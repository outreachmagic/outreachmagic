"""Draining the outbox: selection, anti-echo, and ack bookkeeping.

The push loop used to *derive* what to send from `leads.updated_at`. It now
*reads* what to send from the outbox, which the triggers in pipeline_migration
maintain at write time. See sync_contract for why.

Two things here are load-bearing and easy to get wrong.

**The timestamp we send.** The old push sent `leads.updated_at` as the entry
timestamp. Migration 0014 made the relay guard every row on
`source_updated_at_ms` and reject stale writes -- and 40.7% of local
`updated_at` values are *older than their own created_at*. Sending them now
means the relay correctly rejects the write as stale and the change never
lands. The outbox's `dirty_at` is when we actually observed the change: honest,
monotonic, and not corrupt. That is what goes on the wire.

**Anti-echo by content, not by provenance.** Applying a pulled snapshot writes
to local tables, which fires the triggers, which enqueues an outbox row -- so a
pull would otherwise cause a push of exactly what we just pulled. The fix is not
to suppress triggers during a pull (a temp-table suppression flag latches on a
crash and silently un-tracks every subsequent local write -- the same failure
class this whole stage exists to kill). Instead we record what we believe the
relay holds in `sync_shadow`, and at drain time we rebuild the payload, hash it,
and drop it if the hash already matches. An echo is defined by its content, so a
crash mid-pull costs at most a redundant push, never a lost write.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from sync_audit import content_hash


def select_dirty(
    conn: sqlite3.Connection,
    entity_type: str,
    *,
    op: str = "upsert",
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Oldest-dirty-first, so a backlog drains in the order it was created.

    `dirty_at <= now` is what makes record_failure's backoff real: a row that
    just failed has its dirty_at pushed into the future and is skipped until it
    comes due. Without this gate the ORDER BY merely reshuffles, and a
    permanently-failing row is retried on every single pass.
    """
    sql = (
        "SELECT entity_id, entity_key, workspace_slug, dirty_at, attempts "
        "FROM outbox WHERE entity_type = ? AND op = ? "
        "AND dirty_at <= datetime('now') ORDER BY dirty_at ASC"
    )
    params: tuple = (entity_type, op)
    if limit:
        sql += " LIMIT ?"
        params += (limit,)
    return conn.execute(sql, params).fetchall()


def count_dirty(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT entity_type, op, COUNT(*) AS n FROM outbox GROUP BY entity_type, op"
    ).fetchall()
    return {f"{r['entity_type']}:{r['op']}": r["n"] for r in rows}


def load_shadow(
    conn: sqlite3.Connection, entity_type: str
) -> dict[tuple[str, str], str]:
    """(entity_key, workspace_slug) -> content_hash we believe the relay holds."""
    rows = conn.execute(
        "SELECT entity_key, workspace_slug, content_hash FROM sync_shadow "
        "WHERE entity_type = ?",
        (entity_type,),
    ).fetchall()
    return {(r["entity_key"], r["workspace_slug"] or ""): r["content_hash"] for r in rows}


def is_echo(
    shadow: dict[tuple[str, str], str],
    entity_key: str,
    workspace_slug: Optional[str],
    payload: object,
) -> bool:
    """True when the relay already holds byte-identical content."""
    known = shadow.get((entity_key, workspace_slug or ""))
    return known is not None and known == content_hash(payload)


def record_synced(
    conn: sqlite3.Connection,
    entity_type: str,
    records: Iterable[tuple[str, str, Optional[str], object]],
) -> None:
    """After a successful push: remember the content, clear the outbox row.

    records: (entity_id, entity_key, workspace_slug, payload)
    """
    for entity_id, entity_key, ws_slug, payload in records:
        conn.execute(
            "INSERT INTO sync_shadow (entity_type, entity_key, workspace_slug, content_hash, synced_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT (entity_type, entity_key, workspace_slug) DO UPDATE SET "
            "content_hash = excluded.content_hash, synced_at = excluded.synced_at",
            (entity_type, entity_key, ws_slug or "", content_hash(payload)),
        )
        conn.execute(
            "DELETE FROM outbox WHERE entity_type = ? AND entity_id = ? AND op = 'upsert'",
            (entity_type, entity_id),
        )


def drop_clean(
    conn: sqlite3.Connection, entity_type: str, entity_ids: Iterable[str]
) -> None:
    """Echoes and unbuildable rows: clear the outbox row without pushing."""
    for entity_id in entity_ids:
        conn.execute(
            "DELETE FROM outbox WHERE entity_type = ? AND entity_id = ? AND op = 'upsert'",
            (entity_type, entity_id),
        )


LEGACY_SHADOW_ENTITY_TYPES = ("lead_core", "lead_workspace", "company")


def legacy_shadow_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Legacy natural-key sync_shadow rows for entity types the relay keys by
    uid only. Pull seeds shadow under whatever entity_key the relay event
    carries, which can still be a pre-migration natural key; push always
    writes uid: keys. Nothing prunes the legacy side, so these rows just
    accumulate as orphaned local metadata that inflates sync_shadow totals
    without ever reflecting what the relay actually holds.
    """
    rows = conn.execute(
        "SELECT entity_type, COUNT(*) AS n FROM sync_shadow "
        "WHERE entity_type IN (?, ?, ?) AND entity_key NOT LIKE 'uid:%' "
        "GROUP BY entity_type",
        LEGACY_SHADOW_ENTITY_TYPES,
    ).fetchall()
    return {r["entity_type"]: r["n"] for r in rows}


def prune_legacy_shadow(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict:
    """Preview or delete legacy-key sync_shadow rows for uid-migrated entity
    types. D1 is 100% uid-keyed for leads/companies, so a legacy-key shadow
    row never matches a real relay row -- deleting it can't make a synced
    entity look unsynced.
    """
    counts = legacy_shadow_counts(conn)
    total = sum(counts.values())
    if not dry_run and total:
        conn.execute(
            "DELETE FROM sync_shadow WHERE entity_type IN (?, ?, ?) "
            "AND entity_key NOT LIKE 'uid:%'",
            LEGACY_SHADOW_ENTITY_TYPES,
        )
        conn.commit()
    return {"dry_run": dry_run, "by_type": counts, "total": total}


def record_failure(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_ids: Iterable[str],
    error: str,
    *,
    op: str = "upsert",
) -> None:
    """Backoff: the row stays dirty, but stops being retried on every pass."""
    for entity_id in entity_ids:
        conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ?, "
            "dirty_at = datetime('now', '+' || MIN(POWER(2, attempts + 1), 60) || ' minutes') "
            "WHERE entity_type = ? AND entity_id = ? AND op = ?",
            (error[:500], entity_type, entity_id, op),
        )


def record_deleted(
    conn: sqlite3.Connection,
    entity_type: str,
    records: Iterable[tuple[str, str, Optional[str]]],
) -> None:
    """After a relay delete lands: drop the outbox tombstone, and any shadow
    row for the same key -- a deleted entity has no content for a future
    push to compare against, so a lingering shadow row is now stale.

    records: (entity_id, entity_key, workspace_slug)
    """
    for entity_id, entity_key, ws_slug in records:
        conn.execute(
            "DELETE FROM outbox WHERE entity_type = ? AND entity_id = ? AND op = 'delete'",
            (entity_type, entity_id),
        )
        conn.execute(
            "DELETE FROM sync_shadow WHERE entity_type = ? AND entity_key = ? AND workspace_slug = ?",
            (entity_type, entity_key, ws_slug or ""),
        )
