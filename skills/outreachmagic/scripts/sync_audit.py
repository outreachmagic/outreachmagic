"""Audit trail for everything that crosses the wire to/from the relay.

Why this exists: when a lead's tags or verification "don't sync", there is
currently no way to answer the only two questions that matter --

    1. what payload did we actually send?
    2. what does the relay actually hold right now?

A push that silently no-ops leaves no trace at all today, which is precisely the
failure mode we are hunting. So every payload is written here *before* the HTTP
call and updated with the response, meaning a push that fails, or never happens,
is still visible.

Backed by three CLI commands (see pipeline_cli.py):
    sync-preview  -- what WOULD we send for this lead, without sending it
    sync-diff     -- local payload vs. the relay's stored snapshot, field by field
    sync-audit    -- timeline of every payload sent/received for this lead
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from db_conn import get_conn

# Keep the audit bounded: it rides in the same file as a 646MB database.
DEFAULT_RETENTION_DAYS = 14

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sync_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    direction     TEXT NOT NULL,            -- 'push' | 'pull'
    action        TEXT,                     -- lead_core_update | lead_workspace_update | ...
    entity_key    TEXT,
    workspace     TEXT,
    payload_json  TEXT NOT NULL,
    content_hash  TEXT,
    relay_id      INTEGER,
    http_status   INTEGER,
    error         TEXT,
    batch_label   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sync_audit_key ON sync_audit(entity_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_audit_created ON sync_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_sync_audit_action ON sync_audit(action, created_at DESC);
"""


def canonical_json(payload: Any) -> str:
    """Byte-identical to the relay's canonicalJson() (relay-db.js).

    Sorted keys, no whitespace. Both sides must agree or the content-hash
    comparison that drives skip-if-unchanged (and, later, the sync_shadow
    anti-echo) silently never matches.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def record_push(
    entries: list[dict],
    *,
    batch_label: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> list[int]:
    """Log entries BEFORE they are sent. Returns audit row ids, in entry order.

    Called pre-flight on purpose: if the request dies, or the relay silently
    drops the entry, the payload we *intended* to send is still on record.
    """
    if not entries:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        ids: list[int] = []
        for entry in entries:
            payload = entry.get("payload", {})
            cur = conn.execute(
                """INSERT INTO sync_audit
                   (direction, action, entity_key, workspace, payload_json, content_hash, batch_label)
                   VALUES ('push', ?, ?, ?, ?, ?, ?)""",
                (
                    entry.get("action"),
                    entry.get("entity_key"),
                    entry.get("workspace"),
                    canonical_json(payload),
                    content_hash(payload),
                    batch_label or None,
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        return ids
    finally:
        if own:
            conn.close()


def record_push_result(
    audit_ids: list[int],
    *,
    http_status: Optional[int] = None,
    error: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Attach the relay's response to rows previously written by record_push()."""
    if not audit_ids:
        return
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executemany(
            "UPDATE sync_audit SET http_status = ?, error = ? WHERE id = ?",
            [(http_status, error, i) for i in audit_ids],
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def record_pull(
    events: list[dict],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Log snapshot/event payloads received from the relay."""
    if not events:
        return
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = []
        for ev in events:
            envelope = ev.get("payload") or {}
            # Two envelope shapes live in production: nested (payload.data) and
            # flat (payload.*). 85% of stored snapshots are flat. Handle both,
            # the same way ingest_agent_entry does.
            if "action" in envelope or "data" in envelope:
                action = envelope.get("action")
                payload = envelope.get("data") or {}
                workspace = envelope.get("workspace")
            else:
                action = ev.get("action")
                payload = envelope
                workspace = ev.get("workspace")
            rows.append((
                action,
                ev.get("entity_key"),
                workspace,
                canonical_json(payload),
                content_hash(payload),
                ev.get("relay_id"),
            ))
        conn.executemany(
            """INSERT INTO sync_audit
               (direction, action, entity_key, workspace, payload_json, content_hash, relay_id)
               VALUES ('pull', ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def prune(days: int = DEFAULT_RETENTION_DAYS, conn: Optional[sqlite3.Connection] = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM sync_audit WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        if own:
            conn.close()


def history_for_entity_keys(
    entity_keys: list[str],
    *,
    limit: int = 50,
    errors_only: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Every audited payload for a set of entity keys, newest first.

    Takes a list because a lead's entity_key is derived from mutable columns
    today -- finding an email moves it from the LinkedIn URL to the address, so
    a lead's history can be split across several keys.
    """
    if not entity_keys:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        placeholders = ",".join("?" for _ in entity_keys)
        where = f"entity_key IN ({placeholders})"
        if errors_only:
            where += " AND error IS NOT NULL"
        rows = conn.execute(
            f"""SELECT id, direction, action, entity_key, workspace, payload_json,
                       content_hash, relay_id, http_status, error, batch_label, created_at
                FROM sync_audit WHERE {where}
                ORDER BY created_at DESC, id DESC LIMIT ?""",
            (*entity_keys, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def summary(conn: Optional[sqlite3.Connection] = None) -> dict:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(direction = 'push') AS pushes,
                      SUM(direction = 'pull') AS pulls,
                      SUM(error IS NOT NULL) AS errors,
                      MIN(created_at) AS oldest,
                      MAX(created_at) AS newest
               FROM sync_audit"""
        ).fetchone()
        return dict(row) if row else {}
    finally:
        if own:
            conn.close()
