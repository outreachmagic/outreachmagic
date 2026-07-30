"""Storing Serper candidates, and recording what a human decided about them.

`serper_candidates.py` extracts and scores; this module persists, queues, and
applies. The split matters: extraction is a pure function over search results,
while everything here touches the database and has to survive a relay round
trip.

Three rules the surfaces above this one must not break:

  * **Nothing is pre-selected.** The API returns candidates in score order and
    no `chosen` field. A pre-ticked radio is a recommendation in disguise, and
    an operator triaging 200 leads will click straight through it.
  * **"None of these" is an answer.** It is recorded like any other decision, so
    the lead leaves the review queue and a re-run does not ask again. The
    absence of a decision means "not looked at yet", which is a different thing.
  * **Rejections are kept.** Knowing which candidates were offered and refused
    is the only way to find out whether the score is worth anything.

Applied values ride the ordinary lead write path (`update_lead_identity`), so
the outbox triggers fire and the change pushes to relay like any manual edit.
`title` and `linkedin_url` are both in sync_contract.SYNCED_COLUMNS, and the
snapshot apply side is truthy-guarded, so a later pull cannot blank them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import pipeline_personalize as pp
from db_conn import get_conn

# The personalization field holding the extracted candidate sets + decisions.
CANDIDATES_FIELD = "serper_candidates"

# The lead fields a Serper candidate can resolve. Each maps to how it is written.
REVIEWABLE_FIELDS = ("linkedin", "title", "company_domain")


class SerperReviewError(ValueError):
    """User-facing failure (unknown field, unknown lead, malformed decision)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── storage ──────────────────────────────────────────────────────────────────

def load_candidates(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any]:
    """The stored candidate blob for a lead, or an empty skeleton."""
    row = conn.execute(
        "SELECT field_value FROM lead_personalization WHERE lead_id = ? AND field_name = ?",
        (lead_id, CANDIDATES_FIELD),
    ).fetchone()
    if not row or not row["field_value"]:
        return {"linkedin": [], "company": [], "emails": [], "decisions": {}}
    try:
        blob = json.loads(row["field_value"])
    except (TypeError, ValueError):
        # A corrupt blob must not wedge the queue; treat it as "no candidates"
        # and let the next research run overwrite it.
        return {"linkedin": [], "company": [], "emails": [], "decisions": {}}
    blob.setdefault("decisions", {})
    for key in ("linkedin", "company", "emails"):
        blob.setdefault(key, [])
    return blob


def store_candidates(
    conn: sqlite3.Connection, lead_id: int, extracted: dict[str, Any],
) -> None:
    """Persist a freshly extracted candidate set, keeping existing decisions.

    A re-run finds new results; it does not un-make a judgement someone already
    made. Decisions survive re-extraction by construction.
    """
    previous = load_candidates(conn, lead_id)
    blob = {
        "extracted_at": _now(),
        "linkedin": extracted.get("linkedin", []),
        "company": extracted.get("company", []),
        "emails": extracted.get("emails", []),
        "decisions": previous.get("decisions", {}),
    }
    pp.personalize_set(
        lead_id, CANDIDATES_FIELD, json.dumps(blob, separators=(",", ":")), conn=conn)


# ── decisions ────────────────────────────────────────────────────────────────

def _record_decision(
    conn: sqlite3.Connection, lead_id: int, field: str, *,
    value: Optional[str], dismissed: bool, rejected: list,
) -> None:
    blob = load_candidates(conn, lead_id)
    blob["decisions"][field] = {
        "at": _now(),
        "dismissed": bool(dismissed),
        **({"value": value} if value else {}),
        # What was on offer and not taken. Without this there is no way to tell
        # a good ordering from a lucky one.
        **({"rejected": rejected} if rejected else {}),
    }
    pp.personalize_set(
        lead_id, CANDIDATES_FIELD, json.dumps(blob, separators=(",", ":")), conn=conn)


def _rejected_for(blob: dict, field: str, chosen: Optional[str]) -> list:
    """The candidate identifiers offered for `field` other than the chosen one."""
    key = {"linkedin": ("linkedin", "url"), "title": ("linkedin", "suggested_title"),
           "company_domain": ("company", "domain")}[field]
    bucket, ident = key
    return [
        c.get(ident) for c in blob.get(bucket, [])
        if c.get(ident) and c.get(ident) != chosen
    ]


def apply_decision(
    lead_id: int, field: str, *, value: Optional[str] = None,
    dismissed: bool = False, dry_run: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Record one decision, and write the value through if there is one.

    `dismissed=True` ("none of these") records the judgement and writes nothing.
    """
    if field not in REVIEWABLE_FIELDS:
        raise SerperReviewError(
            f"unknown reviewable field {field!r} (expected one of {', '.join(REVIEWABLE_FIELDS)})")
    if not dismissed and not (value or "").strip():
        raise SerperReviewError(
            f"{field}: pass a value, or dismissed=true for 'none of these'")

    own_conn = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            raise SerperReviewError(f"lead not found: {lead_id}")

        blob = load_candidates(conn, lead_id)
        chosen = (value or "").strip() or None
        plan = {
            "lead_id": lead_id, "field": field,
            "value": chosen, "dismissed": bool(dismissed),
            "rejected": _rejected_for(blob, field, chosen),
        }
        if dry_run:
            return {"status": "dry_run", **plan}

        if not dismissed:
            _write_field(conn, lead_id, field, chosen)
        _record_decision(
            conn, lead_id, field, value=chosen, dismissed=dismissed,
            rejected=plan["rejected"])
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {"status": "dismissed" if dismissed else "applied", **plan}


def _write_field(conn: sqlite3.Connection, lead_id: int, field: str, value: str) -> None:
    """Write one resolved field through the ordinary lead edit path.

    Reuses update_lead_identity for title/linkedin rather than issuing its own
    UPDATE: that function already routes a LinkedIn value to the right column
    and keeps lead_identities consistent, and a second implementation would
    drift from it.

    It is handed *this* connection, not left to open its own. Two connections
    writing the same rows inside one transaction is a lock against yourself, and
    an outer ROLLBACK cannot undo what the second one already committed.
    """
    import dashboard_actions

    if field == "linkedin":
        dashboard_actions.update_lead_identity(lead_id, linkedin=value, conn=conn)
    elif field == "title":
        dashboard_actions.update_lead_identity(lead_id, title=value, conn=conn)
    elif field == "company_domain":
        _link_company_domain(conn, lead_id, value)


def _link_company_domain(conn: sqlite3.Connection, lead_id: int, domain: str) -> None:
    """Point the lead at the company for `domain`, creating it if needed."""
    from pipeline import ensure_company

    # No ad-hoc strip().lower() here -- ensure_company() normalizes, and a
    # second half-normalization at the call site is how the two drift apart.
    company_id = ensure_company(conn, domain=domain)
    if company_id:
        conn.execute(
            "UPDATE leads SET company_id = ?, updated_at = datetime('now') WHERE id = ?",
            (company_id, lead_id))


def apply_batch(decisions: list[dict], *, dry_run: bool = False) -> dict:
    """Apply many decisions in one transaction.

    All-or-nothing: a triage session posts twenty-five judgements at once, and
    half-applying them leaves the operator with no way to know which half.
    """
    conn = get_conn()
    applied, errors = [], []
    try:
        conn.execute("BEGIN")
        for item in decisions or []:
            try:
                applied.append(apply_decision(
                    int(item["lead_id"]), str(item["field"]),
                    value=item.get("value"), dismissed=bool(item.get("dismissed")),
                    dry_run=dry_run, conn=conn,
                ))
            except (SerperReviewError, KeyError, TypeError, ValueError) as exc:
                err = {"item": item, "error": str(exc)}
                # An identity conflict is resolvable — two records for one
                # person — so the pair has to survive the batch rather than be
                # flattened to a message the caller can only display.
                if hasattr(exc, "as_payload"):
                    err["conflict"] = exc.as_payload()
                errors.append(err)
        if errors:
            conn.execute("ROLLBACK")
            return {"status": "error", "applied": 0, "errors": errors}
        conn.execute("COMMIT") if not dry_run else conn.execute("ROLLBACK")
    finally:
        conn.close()
    return {"status": "dry_run" if dry_run else "ok",
            "applied": len(applied), "decisions": applied, "errors": []}


# ── the review queue ─────────────────────────────────────────────────────────

def lead_candidates(conn: sqlite3.Connection, lead_id: int) -> dict:
    """One lead's candidate sets and any decisions already recorded.

    Decisions come back so the pane can show "you already said none of these"
    rather than presenting the same nine people again as if nobody had looked.
    """
    blob = load_candidates(conn, lead_id)
    return {
        "lead_id": lead_id,
        "extracted_at": blob.get("extracted_at"),
        "linkedin": blob.get("linkedin", []),
        "company": blob.get("company", []),
        "emails": blob.get("emails", []),
        "decisions": blob.get("decisions", {}),
        "fields": list(REVIEWABLE_FIELDS),
    }


def review_queue(
    conn: sqlite3.Connection, workspace_id: Optional[str] = None, *,
    field: str = "linkedin", limit: int = 25, offset: int = 0,
) -> dict:
    """Leads with stored candidates and no decision yet for `field`.

    Ordered newest-research-first, so a triage session works through what was
    just gathered rather than restarting at the oldest backlog every time.
    """
    if field not in REVIEWABLE_FIELDS:
        raise SerperReviewError(f"unknown reviewable field {field!r}")

    ws_join, params = "", []
    if workspace_id:
        ws_join = "JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?"
        params.append(workspace_id)
    params += [CANDIDATES_FIELD]

    rows = conn.execute(
        f"""SELECT l.id AS lead_id, l.name, l.title, l.email, l.company,
                   l.linkedin_url, l.company_id, p.field_value AS blob
              FROM leads l
              {ws_join}
              JOIN lead_personalization p
                ON p.lead_id = l.id AND p.field_name = ?
             ORDER BY p.processed_at DESC, l.id DESC""",
        params,
    ).fetchall()

    bucket = "company" if field == "company_domain" else "linkedin"
    pending = []
    for r in rows:
        try:
            blob = json.loads(r["blob"] or "{}")
        except (TypeError, ValueError):
            continue
        if field in (blob.get("decisions") or {}):
            continue                       # already judged, including "none of these"
        candidates = blob.get(bucket) or []
        if not candidates:
            continue                       # nothing to choose between
        pending.append({
            "lead_id": r["lead_id"], "name": r["name"], "title": r["title"],
            "email": r["email"], "company": r["company"],
            "current_linkedin_url": r["linkedin_url"],
            # Score order only. No `chosen`, no default -- the picker starts on
            # "none of these" and the operator moves it.
            "candidates": candidates,
        })
    return {"field": field, "limit": limit, "offset": offset,
            "total": len(pending), "leads": pending[offset:offset + limit]}
