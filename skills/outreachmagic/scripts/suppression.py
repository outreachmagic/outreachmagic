"""Suppression lists: contacts that must never leave a workspace in an export.

Called "suppression" rather than "block list" because that is the term every
comparable tool ships (Salesforce, HubSpot, Marketo, SendGrid, Instantly,
Smartlead), and because `blocklist` already means something else here -- the
ICP title-exclusion list in contact_review.py.

The design decision everything else follows from: **suppress by identifier
VALUE, resolve to leads at match time.** A suppression stored as a tag or a
lead column dies the moment a re-import overwrites the row, or the lead is
deleted and re-created with a new id. Stored against the value, suppressing
`acme.com` covers the Acme contacts on file today and the ones imported next
month, with no second action.

Two tables (see schema.py):

- `suppression_entries` -- the authored rules. Durable, synced, soft-deleted.
- `workspace_lead_suppressions` -- materialized matches. Derived, never synced,
  rebuildable by `reconcile()`.

Enforcement lives in exactly one place: `dashboard_queries.lead_filter_clause()`
gained a `suppressed` key defaulting to "exclude", so the contacts list, its
counts, the CSV export and every bulk action all inherit it from one WHERE.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Optional

from db_conn import get_conn
from workspace_routing import DEFAULT_ORG_ID

# Entry types, and how a raw value becomes a match key. Adding one means adding
# both a normalizer here and a matching arm in _match_sql().
ENTRY_TYPES = (
    "email",
    "email_domain",
    "linkedin_url",
    "company_domain",
    "company_id",
    "lead_id",
    "name_company",
)

# A closed enum on purpose. Free text here is unqueryable within a month, and
# "how much of this list is competitors vs. existing customers" is the first
# question anyone asks of a suppression list.
REASONS = (
    "unsubscribed",
    "bounced",
    "complained",
    "competitor",
    "existing_customer",
    "partner",
    "legal_request",
    "do_not_contact",
    "manual",
)

DEFAULT_REASON = "manual"


class SuppressionError(ValueError):
    """User-facing failure (unknown type, unknown reason, unusable value)."""


# ── Normalization ────────────────────────────────────────────────────────────

def normalize_value(entry_type: str, value: Any) -> str:
    """The match key for a raw value. Raises when the value can't be one.

    Each type reuses the normalizer the rest of the codebase already applies to
    that field, so a suppression matches what is actually stored rather than
    what the operator happened to type.
    """
    raw = str(value or "").strip()
    if not raw:
        raise SuppressionError("value is required")

    if entry_type == "email":
        from normalize import canonicalize_email

        address, _repairs = canonicalize_email(raw)
        if not address:
            raise SuppressionError(f"not a valid email address: {value!r}")
        return address

    if entry_type in ("email_domain", "company_domain"):
        from pipeline_utils import company_registrable_domain

        domain = raw.lower().lstrip("@").strip("/")
        if domain.startswith(("http://", "https://")):
            domain = domain.split("//", 1)[1]
        domain = domain.split("/", 1)[0]
        registrable = company_registrable_domain(domain) or domain
        if "." not in registrable:
            raise SuppressionError(f"not a valid domain: {value!r}")
        return registrable

    if entry_type == "linkedin_url":
        from workspace_routing import linkedin_in_slug, normalize_linkedin

        slug = linkedin_in_slug(raw)
        if slug:
            return f"linkedin.com/in/{slug.strip('/').lower()}"
        normalized = normalize_linkedin(raw)
        if not normalized:
            raise SuppressionError(f"not a usable LinkedIn URL: {value!r}")
        return str(normalized).lower().rstrip("/")

    if entry_type in ("company_id", "lead_id"):
        if not raw.isdigit():
            raise SuppressionError(f"{entry_type} must be an integer, got {value!r}")
        return raw

    if entry_type == "name_company":
        # "Jane Doe|Acme Inc" -- the same composite the weak-identity import
        # path uses, so a suppression can name someone with no email and no
        # LinkedIn URL.
        from pipeline_utils import normalize_company_name
        from workspace_routing import normalize_person_name

        name, _, company = raw.partition("|")
        person = normalize_person_name(name) or name.strip().lower()
        if not person:
            raise SuppressionError("name_company needs a person name")
        return f"{person}|{normalize_company_name(company)}"

    raise SuppressionError(
        f"unknown entry type {entry_type!r}. Valid: {', '.join(ENTRY_TYPES)}")


# ── Matching ─────────────────────────────────────────────────────────────────

def _match_sql(entry_type: str) -> str:
    """A SQL predicate matching leads against `?` (one normalized value).

    Aliases `l` (leads) and `wl` (workspace_leads) are in scope. Each arm is
    the read side of the corresponding normalize_value() arm; they have to stay
    in step or a suppression is stored and never matches anything, which is the
    worst possible failure for this feature -- it looks like it worked.
    """
    if entry_type == "email":
        return ("(LOWER(TRIM(l.email)) = ?"
                " OR EXISTS (SELECT 1 FROM lead_emails le"
                "            WHERE le.lead_id = l.id AND LOWER(TRIM(le.email)) = ?))")
    if entry_type == "email_domain":
        return "LOWER(TRIM(l.email_domain)) = ?"
    if entry_type == "linkedin_url":
        # Stored URLs vary in scheme and www, so compare on the /in/ slug the
        # way normalize_value() produced it.
        return ("REPLACE(REPLACE(REPLACE(LOWER(TRIM(COALESCE(l.linkedin_url, ''))),"
                " 'https://', ''), 'http://', ''), 'www.', '') LIKE ? || '%'")
    if entry_type == "company_domain":
        return ("EXISTS (SELECT 1 FROM companies c2"
                "        WHERE c2.id = l.company_id AND LOWER(TRIM(c2.domain)) = ?)"
                " OR LOWER(TRIM(l.email_domain)) = ?")
    if entry_type == "company_id":
        return "l.company_id = CAST(? AS INTEGER)"
    if entry_type == "lead_id":
        return "l.id = CAST(? AS INTEGER)"
    if entry_type == "name_company":
        return ("LOWER(TRIM(COALESCE(l.name, ''))) || '|'"
                " || LOWER(TRIM(COALESCE(l.company, ''))) = ?")
    raise SuppressionError(f"unknown entry type: {entry_type}")


def _match_params(entry_type: str, value: str) -> list:
    """Bind values for _match_sql (some arms use `?` more than once)."""
    if entry_type in ("email", "company_domain"):
        return [value, value]
    return [value]


def _materialize_entry(conn: sqlite3.Connection, entry: dict) -> int:
    """Insert (workspace, lead) rows for one entry. Returns rows written."""
    predicate = _match_sql(entry["entry_type"])
    params = _match_params(entry["entry_type"], entry["value_normalized"])
    scope_sql, scope_params = "", []
    if entry["workspace_id"]:
        scope_sql = " AND wl.workspace_id = ?"
        scope_params = [entry["workspace_id"]]
    cur = conn.execute(
        f"""INSERT OR IGNORE INTO workspace_lead_suppressions
                (workspace_id, lead_id, entry_id, matched_on)
            SELECT wl.workspace_id, l.id, ?, ?
              FROM workspace_leads wl
              JOIN leads l ON l.id = wl.lead_id
             WHERE ({predicate}){scope_sql}""",
        [entry["id"], entry["entry_type"], *params, *scope_params],
    )
    return cur.rowcount or 0


# ── Public API ───────────────────────────────────────────────────────────────

def add_entry(
    conn: sqlite3.Connection,
    *,
    entry_type: str,
    value: Any,
    workspace_id: Optional[str] = None,
    reason: str = DEFAULT_REASON,
    note: Optional[str] = None,
    source: str = "agent",
    created_by: Optional[str] = None,
    expires_at: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Author a suppression rule and materialize its matches.

    Re-adding a revoked entry un-revokes it rather than minting a second row,
    so the id (and anything referencing it) stays stable across a
    suppress / un-suppress / re-suppress cycle.
    """
    if entry_type not in ENTRY_TYPES:
        raise SuppressionError(
            f"unknown entry type {entry_type!r}. Valid: {', '.join(ENTRY_TYPES)}")
    if reason not in REASONS:
        raise SuppressionError(
            f"unknown reason {reason!r}. Valid: {', '.join(REASONS)}")
    normalized = normalize_value(entry_type, value)
    scope = "workspace" if workspace_id else "org"

    existing = conn.execute(
        """SELECT * FROM suppression_entries
            WHERE org_id = ? AND entry_type = ? AND value_normalized = ?
              AND workspace_id IS ?""",
        (org_id, entry_type, normalized, workspace_id),
    ).fetchone()

    if existing:
        entry_id = existing["id"]
        conn.execute(
            """UPDATE suppression_entries
                  SET revoked_at = NULL, reason = ?, note = COALESCE(?, note),
                      expires_at = ?
                WHERE id = ?""",
            (reason, note, expires_at, entry_id),
        )
        created = False
    else:
        entry_id = f"sup_{uuid.uuid4().hex[:20]}"
        conn.execute(
            """INSERT INTO suppression_entries
                   (id, org_id, workspace_id, scope, entry_type, value_raw,
                    value_normalized, reason, note, source, created_by, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, org_id, workspace_id, scope, entry_type, str(value),
             normalized, reason, note, source, created_by, expires_at),
        )
        created = True

    entry = dict(conn.execute(
        "SELECT * FROM suppression_entries WHERE id = ?", (entry_id,)).fetchone())
    matched = _materialize_entry(conn, entry)
    conn.commit()
    return {
        "status": "created" if created else "updated",
        "entry_id": entry_id,
        "entry_type": entry_type,
        "value": normalized,
        "scope": scope,
        "reason": reason,
        "contacts_suppressed": matched,
    }


def revoke_entry(
    conn: sqlite3.Connection,
    *,
    entry_type: str,
    value: Any,
    workspace_id: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Soft-delete a rule and drop its materialized matches."""
    normalized = normalize_value(entry_type, value)
    row = conn.execute(
        """SELECT id FROM suppression_entries
            WHERE org_id = ? AND entry_type = ? AND value_normalized = ?
              AND workspace_id IS ? AND revoked_at IS NULL""",
        (org_id, entry_type, normalized, workspace_id),
    ).fetchone()
    if not row:
        return {"status": "not_found", "entry_type": entry_type, "value": normalized}
    removed = conn.execute(
        "DELETE FROM workspace_lead_suppressions WHERE entry_id = ?", (row["id"],),
    ).rowcount or 0
    conn.execute(
        "UPDATE suppression_entries SET revoked_at = datetime('now') WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    return {"status": "revoked", "entry_id": row["id"], "entry_type": entry_type,
            "value": normalized, "contacts_released": removed}


def list_entries(
    conn: sqlite3.Connection,
    *,
    workspace_id: Optional[str] = None,
    entry_type: Optional[str] = None,
    reason: Optional[str] = None,
    include_revoked: bool = False,
    org_id: str = DEFAULT_ORG_ID,
) -> list[dict]:
    """Authored rules with their current match counts.

    Org-wide entries are included for a workspace query: they apply to it, so
    hiding them would make the list a misleading account of what is suppressed.
    """
    where = ["e.org_id = ?"]
    params: list = [org_id]
    if workspace_id:
        where.append("(e.workspace_id = ? OR e.workspace_id IS NULL)")
        params.append(workspace_id)
    if entry_type:
        where.append("e.entry_type = ?")
        params.append(entry_type)
    if reason:
        where.append("e.reason = ?")
        params.append(reason)
    if not include_revoked:
        where.append("e.revoked_at IS NULL")
    rows = conn.execute(
        f"""SELECT e.*,
                   (SELECT COUNT(*) FROM workspace_lead_suppressions s
                     WHERE s.entry_id = e.id) AS matched_contacts
              FROM suppression_entries e
             WHERE {" AND ".join(where)}
             ORDER BY e.created_at DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def check(
    conn: sqlite3.Connection,
    value: Any,
    *,
    workspace_id: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """"Why is this contact missing from my export?" -- answered definitively.

    This is the support question the feature generates, so it gets a first
    class command rather than leaving people to reason about which of six
    identifier types might have matched.
    """
    raw = str(value or "").strip()
    matches: list[dict] = []
    for entry_type in ENTRY_TYPES:
        try:
            normalized = normalize_value(entry_type, raw)
        except SuppressionError:
            continue
        where = ["entry_type = ?", "value_normalized = ?", "org_id = ?",
                 "revoked_at IS NULL"]
        params: list = [entry_type, normalized, org_id]
        if workspace_id:
            where.append("(workspace_id = ? OR workspace_id IS NULL)")
            params.append(workspace_id)
        for row in conn.execute(
            f"SELECT * FROM suppression_entries WHERE {' AND '.join(where)}", params,
        ).fetchall():
            matches.append(dict(row))

    # Also answer for a lead that is suppressed transitively -- blocked by its
    # company's domain rather than by anything on the lead itself, which is the
    # case people find most confusing.
    lead_rows = []
    if workspace_id:
        lead_rows = [dict(r) for r in conn.execute(
            """SELECT s.lead_id, s.matched_on, e.entry_type, e.value_normalized,
                      e.reason, l.name, l.email
                 FROM workspace_lead_suppressions s
                 JOIN suppression_entries e ON e.id = s.entry_id
                 JOIN leads l ON l.id = s.lead_id
                WHERE s.workspace_id = ?
                  AND (LOWER(TRIM(COALESCE(l.email, ''))) = LOWER(?)
                       OR LOWER(TRIM(COALESCE(l.name, ''))) = LOWER(?))""",
            (workspace_id, raw, raw),
        ).fetchall()]

    return {
        "value": raw,
        "suppressed": bool(matches or lead_rows),
        "matching_entries": matches,
        "suppressed_leads": lead_rows,
    }


def reconcile(
    conn: sqlite3.Connection,
    *,
    workspace_id: Optional[str] = None,
    lead_ids: Optional[list] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Rebuild materialized matches.

    Three call sites: after an entry is added or revoked (handled inline), after
    an import or any write touching leads.email / linkedin_url / company_id, and
    on demand for a full rebuild.

    `lead_ids` narrows the rebuild to the rows an import just touched, which is
    what keeps import-time reconciliation proportional to the batch rather than
    to the workspace.
    """
    where = ["e.org_id = ?", "e.revoked_at IS NULL",
             "(e.expires_at IS NULL OR e.expires_at > datetime('now'))"]
    params: list = [org_id]
    if workspace_id:
        where.append("(e.workspace_id = ? OR e.workspace_id IS NULL)")
        params.append(workspace_id)
    entries = [dict(r) for r in conn.execute(
        f"SELECT e.* FROM suppression_entries e WHERE {' AND '.join(where)}", params,
    ).fetchall()]

    if lead_ids:
        ids = [int(x) for x in lead_ids]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM workspace_lead_suppressions WHERE lead_id IN ({placeholders})",
            ids,
        )
    elif workspace_id:
        conn.execute(
            "DELETE FROM workspace_lead_suppressions WHERE workspace_id = ?",
            (workspace_id,),
        )
    else:
        conn.execute("DELETE FROM workspace_lead_suppressions")

    written = 0
    for entry in entries:
        written += _materialize_entry(conn, entry)

    # Expired and revoked entries leave their matches behind otherwise.
    conn.execute(
        """DELETE FROM workspace_lead_suppressions
            WHERE entry_id NOT IN (SELECT id FROM suppression_entries
                                    WHERE revoked_at IS NULL
                                      AND (expires_at IS NULL OR expires_at > datetime('now')))""")
    conn.commit()
    return {"status": "ok", "entries_applied": len(entries),
            "suppressed_contacts": written,
            "scope": workspace_id or "org-wide",
            "narrowed_to_leads": len(lead_ids) if lead_ids else None}


def stats(
    conn: sqlite3.Connection, workspace_id: str, *, org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Counts for the suppression card: entries by type and reason, and how
    many contacts each is currently keeping out of exports."""
    by_reason = conn.execute(
        """SELECT e.reason, COUNT(DISTINCT e.id) AS entries,
                  COUNT(s.lead_id) AS contacts
             FROM suppression_entries e
             LEFT JOIN workspace_lead_suppressions s
               ON s.entry_id = e.id AND s.workspace_id = ?
            WHERE e.org_id = ? AND e.revoked_at IS NULL
              AND (e.workspace_id = ? OR e.workspace_id IS NULL)
            GROUP BY e.reason ORDER BY contacts DESC""",
        (workspace_id, org_id, workspace_id),
    ).fetchall()
    by_type = conn.execute(
        """SELECT e.entry_type, COUNT(DISTINCT e.id) AS entries,
                  COUNT(s.lead_id) AS contacts
             FROM suppression_entries e
             LEFT JOIN workspace_lead_suppressions s
               ON s.entry_id = e.id AND s.workspace_id = ?
            WHERE e.org_id = ? AND e.revoked_at IS NULL
              AND (e.workspace_id = ? OR e.workspace_id IS NULL)
            GROUP BY e.entry_type ORDER BY contacts DESC""",
        (workspace_id, org_id, workspace_id),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(DISTINCT lead_id) AS n FROM workspace_lead_suppressions WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()["n"]
    return {
        "suppressed_contacts": total,
        "by_reason": [dict(r) for r in by_reason],
        "by_type": [dict(r) for r in by_type],
    }


def summarize_for_leads(
    conn: sqlite3.Connection, workspace_id: str, lead_ids: list,
) -> dict:
    """How many of these leads are suppressed, and why.

    Used by the import summary: the ask is that suppressed contacts are still
    stored, just flagged and excluded from exports, and that the import says
    how many that was.
    """
    if not lead_ids:
        return {"total": 0, "by_reason": {}, "by_entry_type": {}}
    ids = [int(x) for x in lead_ids]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT s.lead_id, e.reason, e.entry_type
              FROM workspace_lead_suppressions s
              JOIN suppression_entries e ON e.id = s.entry_id
             WHERE s.workspace_id = ? AND s.lead_id IN ({placeholders})""",
        [workspace_id, *ids],
    ).fetchall()
    by_reason: dict[str, int] = {}
    by_type: dict[str, int] = {}
    seen: set[int] = set()
    for row in rows:
        if row["lead_id"] in seen:
            continue
        seen.add(row["lead_id"])
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
        by_type[row["entry_type"]] = by_type.get(row["entry_type"], 0) + 1
    return {"total": len(seen), "by_reason": by_reason, "by_entry_type": by_type}


def add_entries_bulk(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    workspace_id: Optional[str] = None,
    default_reason: str = DEFAULT_REASON,
    source: str = "import",
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Add many rules. Each row: {type, value, [reason], [note]}.

    One bad row reports itself and the rest still land -- a 400-line
    suppression CSV that refuses entirely because line 212 has a typo is worse
    than one that tells you about line 212.
    """
    added, updated, failed = 0, 0, []
    total_matched = 0
    for i, row in enumerate(rows):
        try:
            result = add_entry(
                conn,
                entry_type=str(row.get("type") or row.get("entry_type") or "").strip(),
                value=row.get("value"),
                workspace_id=workspace_id,
                reason=str(row.get("reason") or default_reason).strip(),
                note=row.get("note"),
                source=source,
                org_id=org_id,
            )
        except SuppressionError as exc:
            failed.append({"row": i + 1, "value": row.get("value"), "error": str(exc)})
            continue
        total_matched += result["contacts_suppressed"]
        if result["status"] == "created":
            added += 1
        else:
            updated += 1
    return {"status": "ok", "added": added, "updated": updated,
            "failed": len(failed), "errors": failed[:20],
            "contacts_suppressed": total_matched}


def reconcile_after_write(workspace_id: Optional[str], lead_ids: list) -> dict:
    """Convenience wrapper for callers that don't hold a connection open."""
    if not lead_ids:
        return {"status": "ok", "suppressed_contacts": 0}
    conn = get_conn()
    try:
        return reconcile(conn, workspace_id=workspace_id, lead_ids=lead_ids)
    finally:
        conn.close()
