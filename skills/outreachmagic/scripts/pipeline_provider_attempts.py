"""
Org-wide per-lead provider attempt tracking (TryKitt, Icypeas, Serper,
MillionVerifier, Scrubby) -- replaces the old `{provider}_attempted`
workspace-tag convention.

A lead is the same person regardless of which workspace they appear in, so
tracking here is keyed on `lead_id` alone (no workspace_id) -- one record
follows the lead everywhere, unlike `workspace_lead_tags` which is scoped
per-workspace and let the same lead get re-attempted in a second workspace.

Syncs org-wide via the existing `lead_core_update` relay snapshot (see
lead_sync.py) -- same mechanism and durability as linkedin_headline/
linkedin_bio/linkedin_sales_nav_id.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from db_conn import get_conn

PROVIDER_DOMAINS: dict[str, str] = {
    "trykitt": "email_finding",
    "icypeas": "email_finding",
    "serper": "research",
    "millionverifier": "email_verification",
    "scrubby": "email_verification",
}

ATTEMPT_STATUSES = frozenset({"found", "not_found", "error", "skipped", "unknown"})


def record_provider_attempt(
    conn: sqlite3.Connection,
    lead_id: int,
    provider: str,
    *,
    status: str,
    domain: Optional[str] = None,
    result_email: Optional[str] = None,
    result_validity: Optional[str] = None,
    batch_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
    attempted_at: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> None:
    """Append one provider-attempt observation (Stage 7: lead_provider_observations,
    origin='attempt'). `lead_provider_attempts` is now a read-only VIEW projecting
    the latest attempt per (lead_id, provider) -- callers reading it see the same
    "one row per lead+provider" shape as before; this just stops discarding the
    history that produced that latest row."""
    from provider_observations import ORIGIN_ATTEMPT, kind_for_provider_domain, record_observation

    provider = (provider or "").strip().lower()
    if not provider or not lead_id:
        return
    status = (status or "unknown").strip().lower()
    if status not in ATTEMPT_STATUSES:
        status = "unknown"
    # PROVIDER_DOMAINS is keyed by provider name to a *category* label
    # ("email_finding", "research", ...), only meaningful as input to
    # kind_for_provider_domain() below -- it must never be written into the
    # actual domain column. That used to happen here (`domain = domain or
    # PROVIDER_DOMAINS.get(provider)`), so every trykitt/icypeas attempt
    # recorded the literal string "email_finding" as its domain, including
    # leads that had a real domain to search against -- see
    # debug-email-finding-domain-bug.md. Leave the real domain (possibly
    # None) untouched; only fall back to the category label for kind.
    kind = kind_for_provider_domain(domain or PROVIDER_DOMAINS.get(provider))
    record_observation(
        conn, lead_id,
        kind=kind,
        origin=ORIGIN_ATTEMPT,
        provider=provider,
        status=status,
        domain=domain,
        result_email=result_email,
        result_validity=result_validity,
        batch_id=batch_id,
        metadata_json=json.dumps(metadata) if metadata else None,
        observed_at=attempted_at,
        completed_at=completed_at,
    )


def record_provider_attempts_bulk(
    lead_ids: list[int],
    provider: str,
    *,
    status: str = "unknown",
) -> dict:
    """Stamp the same provider+status on many leads at once (0-cost skip stamping)."""
    ids = list(dict.fromkeys(int(lid) for lid in lead_ids if lid))
    if not ids or not provider:
        return {"status": "noop", "changed": 0, "leads": 0}
    conn = get_conn()
    try:
        for lid in ids:
            record_provider_attempt(conn, lid, provider, status=status)
        conn.commit()
        return {"status": "recorded", "changed": len(ids), "leads": len(ids), "provider": provider}
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("metadata_json"):
        try:
            d["metadata"] = json.loads(d["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = None
    return d


def list_provider_attempts(lead_id: int) -> list[dict]:
    """Self-contained read for CLI use (opens its own connection)."""
    conn = get_conn()
    try:
        return get_provider_attempts_for_lead(conn, lead_id)
    finally:
        conn.close()


def get_provider_attempts_for_lead(conn: sqlite3.Connection, lead_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT provider, domain, attempted_at, completed_at, status,
                  result_email, result_validity, metadata_json
           FROM lead_provider_attempts WHERE lead_id = ? ORDER BY provider""",
        (lead_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_provider_attempts_map(conn: sqlite3.Connection, lead_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk read for batch_lead_lookup -- {lead_id: [attempt, ...]}."""
    out: dict[int, list[dict]] = {lid: [] for lid in lead_ids}
    if not lead_ids:
        return out
    placeholders = ",".join("?" for _ in lead_ids)
    rows = conn.execute(
        f"""SELECT lead_id, provider, domain, attempted_at, completed_at, status,
                   result_email, result_validity, metadata_json
            FROM lead_provider_attempts WHERE lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall()
    for r in rows:
        out.setdefault(int(r["lead_id"]), []).append(_row_to_dict(r))
    return out


def has_attempted(conn: sqlite3.Connection, lead_id: int, provider: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lead_provider_attempts WHERE lead_id = ? AND provider = ?",
        (lead_id, (provider or "").strip().lower()),
    ).fetchone()
    return row is not None


def apply_provider_attempts_payload(
    conn: sqlite3.Connection, lead_id: int, attempts: list[dict],
) -> None:
    """Apply a pulled `lead_core_update.provider_attempts` array to the local table."""
    if not attempts:
        return
    for a in attempts:
        if not isinstance(a, dict):
            continue
        provider = str(a.get("provider") or "").strip().lower()
        if not provider:
            continue
        record_provider_attempt(
            conn, lead_id, provider,
            status=str(a.get("status") or "unknown"),
            domain=a.get("domain"),
            result_email=a.get("result_email"),
            result_validity=a.get("result_validity"),
            attempted_at=a.get("attempted_at"),
            completed_at=a.get("completed_at"),
            metadata=a.get("metadata"),
        )
