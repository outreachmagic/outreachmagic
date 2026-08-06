#!/usr/bin/env python3
"""Mail-merge personalization for leads and companies."""

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from constants import SHARED_EMAIL_DOMAINS
from db_conn import get_conn
from pipeline_utils import normalize_company_domain

_LEAD_SOURCE_FIELDS = {"first_name": "name"}
_COMPANY_SOURCE_FIELDS = {"company_name": "name"}

_FIELD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

# Real columns on `leads` / `companies`. A personalization field may never
# shadow one: `personalized_original_source` sitting next to the actual
# `leads.original_source` is ambiguous to every reader (exports, CRM mapping,
# the dashboard's custom-field panel) and is how source provenance silently
# ended up in the wrong place on CSV import.
_LEAD_RESERVED_NAMES = frozenset({
    "id", "name", "company_id", "company", "title", "industry", "headcount",
    "headcount_numeric", "email", "email_domain", "linkedin_url",
    "linkedin_sales_nav_id", "location_city", "location_state",
    "location_country", "channel", "stage", "notes",
    "original_source", "original_source_detail", "original_source_platform",
    "original_source_at", "latest_source", "latest_source_detail",
    "latest_source_platform", "latest_source_at",
    "email_verification_status", "email_verified_at", "created_at",
    "updated_at", "last_contact_at", "next_action", "latest_sender",
    "latest_sender_platform", "linkedin_headline", "linkedin_bio", "uid",
    "record_type", "superseded_at",
})
_COMPANY_RESERVED_NAMES = frozenset({
    "id", "name", "domain", "industry", "headcount", "headcount_numeric",
    "hq_city", "hq_state", "hq_country", "created_at", "updated_at", "uid",
})


def looks_company_scoped(field_name: str) -> bool:
    """Heuristic for ROUTING an ambiguously-scoped field during import.

    `company_name`/`company_*` almost certainly describes the account rather
    than the person, so an import row carrying one writes it to the company.
    This is a guess, and it is only ever allowed to pick a destination -- it
    must NOT be used to validate an explicit write. company_personalize_set()
    used to reject anything failing this test, which made perfectly ordinary
    company facts (`phone_google_maps`, `gm_rating`, `hours`) unstorable: the
    caller had already said "company scope" by calling the company function.

    Now the LAST resort, not the first: resolve_scope() consults an explicit
    prefix and then the registry before falling back here.
    """
    return field_name == "company_name" or field_name.startswith("company_")


# Back-compat alias: pipeline.py imports this name for its import-routing loop.
is_company_personalization_field = looks_company_scoped


# ── Scope registry ───────────────────────────────────────────────────────────
#
# A personalization field belongs to exactly ONE scope. Lead-scoped values are
# per person; company-scoped values are shared by every contact at that company.
# Until this registry existed, scope was decided by looks_company_scoped() and
# then never reported, so nothing downstream could answer the only question
# anyone actually has here -- "will this column land on the contact or the
# account?". That single gap is why the export picker was unreadable, why the
# `full` preset silently omitted 53k company values, and why the same company
# could take eleven different `company_name` values from one sheet without a
# word of complaint.
#
# First write to a scope claims the name. After that the registry is
# authoritative and a contradicting write is an error, not a silent reroute.

COMPANY_FIELD_PREFIX = "company_personalized_"
LEAD_FIELD_PREFIX = "personalized_"


def strip_field_prefix(column: str) -> tuple[str, Optional[str]]:
    """Split an import/export column into (field_name, explicit_scope|None).

    `company_personalized_icp_tier` -> ("icp_tier", "company")
    `personalized_first_name`       -> ("first_name", None)   [lead by position,
                                        but not an explicit claim -- a legacy
                                        name like personalized_company_name has
                                        always meant the company's]
    """
    text = str(column or "").strip().lower()
    if text.startswith(COMPANY_FIELD_PREFIX):
        return text[len(COMPANY_FIELD_PREFIX):], "company"
    if text.startswith(LEAD_FIELD_PREFIX):
        return text[len(LEAD_FIELD_PREFIX):], None
    return text, None


def _registry_scope(conn: sqlite3.Connection, field_name: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT scope FROM personalization_fields WHERE field_name = ?",
            (field_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None          # pre-migration database
    return row["scope"] if row else None


def resolve_scope(
    field_name: str,
    *,
    explicit: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[str, str]:
    """Which table this field belongs in, and what decided it.

    Returns (scope, source) where source is one of "explicit", "registry",
    "heuristic". The source matters as much as the scope: an import summary
    reporting "heuristic" is telling the operator that nothing authoritative
    knew, and that a wrong guess is now on disk.
    """
    if explicit in ("lead", "company"):
        return explicit, "explicit"
    own = conn is None
    if own:
        conn = get_conn()
    try:
        registered = _registry_scope(conn, field_name)
    finally:
        if own:
            conn.close()
    if registered:
        return registered, "registry"
    return ("company" if looks_company_scoped(field_name) else "lead"), "heuristic"


def register_field(
    field_name: str,
    scope: str,
    *,
    source: str = "cli",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Claim a field name for a scope. Idempotent; conflicting claims raise."""
    if scope not in ("lead", "company"):
        raise ValueError(f"scope must be 'lead' or 'company', got {scope!r}")
    own = conn is None
    if own:
        conn = get_conn()
    try:
        existing = _registry_scope(conn, field_name)
        if existing and existing != scope:
            raise ValueError(
                f"{field_name!r} is already registered as a {existing.upper()} "
                f"personalization field. A field belongs to one scope. Either "
                f"write it to {existing} scope, or use a different name "
                f"(e.g. {COMPANY_FIELD_PREFIX if scope == 'company' else ''}"
                f"{field_name}_{scope}).")
        if existing:
            return {"status": "already", "field": field_name, "scope": scope}
        conn.execute(
            """INSERT OR IGNORE INTO personalization_fields
                   (field_name, scope, first_seen_at, first_seen_source)
               VALUES (?, ?, ?, ?)""",
            (field_name, scope, datetime.now(timezone.utc).isoformat(timespec="seconds"), source),
        )
        if own:
            conn.commit()
        return {"status": "registered", "field": field_name, "scope": scope}
    except sqlite3.OperationalError:
        return {"status": "skipped", "field": field_name, "scope": scope}
    finally:
        if own:
            conn.close()


def list_registered_fields(
    *,
    scope: Optional[str] = None,
    with_values: bool = False,
    value_limit: int = 12,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """The registry, plus how much each field is actually used.

    `with_values` adds the distinct values in use. That is the check that stops
    the next campaign from either clobbering another campaign's values or
    minting `icp_segment_2` because nobody could see that `icp_segment` was
    already the convention.
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        where, params = "", []
        if scope in ("lead", "company"):
            where, params = " WHERE scope = ?", [scope]
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT field_name, scope, first_seen_at, first_seen_source"
                f" FROM personalization_fields{where} ORDER BY scope, field_name",
                params).fetchall()]
        except sqlite3.OperationalError:
            return []
        for row in rows:
            table = ("company_personalization" if row["scope"] == "company"
                     else "lead_personalization")
            id_col = "company_id" if row["scope"] == "company" else "lead_id"
            stat = conn.execute(
                f"SELECT COUNT(DISTINCT {id_col}) n, COUNT(DISTINCT field_value) v"
                f"  FROM {table} WHERE field_name = ?", (row["field_name"],)).fetchone()
            row["entities"] = stat["n"] if stat else 0
            row["distinct_values"] = stat["v"] if stat else 0
            if with_values:
                row["top_values"] = [
                    [r["field_value"], r["n"]] for r in conn.execute(
                        f"SELECT field_value, COUNT(*) n FROM {table}"
                        f" WHERE field_name = ? AND field_value IS NOT NULL"
                        f" GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
                        (row["field_name"], value_limit)).fetchall()]
        return rows
    finally:
        if own:
            conn.close()


def validate_personalization_field(field_name: str, *, scope: str) -> str:
    """Normalize and validate a personalization field name for `scope`.

    Returns the cleaned name; raises ValueError with the reason otherwise.
    Scope decides which set of real columns the name may not shadow.
    """
    name = str(field_name or "").strip().lower()
    if not name:
        raise ValueError("field name is required")
    if not _FIELD_NAME_RE.match(name):
        raise ValueError(
            f"invalid field name {field_name!r}: use lowercase letters, digits "
            "and underscores (max 64 chars), starting with a letter or digit"
        )
    reserved = _COMPANY_RESERVED_NAMES if scope == "company" else _LEAD_RESERVED_NAMES
    if name in reserved:
        raise ValueError(
            f"{name!r} is a real {scope} column, not a personalization field. "
            f"Set it directly instead of creating a personalized_{name} shadow copy."
        )
    return name


def resolve_company_id(
    conn: sqlite3.Connection,
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[int]:
    from pipeline import ensure_company

    if company_id:
        row = conn.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
        return company_id if row else None
    dom = normalize_company_domain(domain)
    if dom and dom not in SHARED_EMAIL_DOMAINS:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (dom,)).fetchone()
        if row:
            return row["id"]
        return ensure_company(conn, domain=dom)
    if name and str(name).strip():
        return ensure_company(conn, name=str(name).strip())
    return None


def company_entity_key(conn: sqlite3.Connection, company_id: int) -> Optional[str]:
    """The relay wire key: the immutable uid, not domain/name.

    A company keyed by domain-or-name splits in two the moment enrichment
    finds a domain for a name-only company (or vice versa) -- the old snapshot
    orphans under the old key and a new one appears under the new one. The
    same fix already shipped for leads (pipeline_migration.py backfills a uid
    column on both `leads` and `companies` in the same migration, and the
    relay's push/alias-resolution code already branches on kind == "company" —
    this was always meant to land here too). Domain and name become aliases
    (see build_company_sync_payload) instead of the identity itself.
    """
    row = conn.execute("SELECT uid FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row or not row["uid"]:
        return None
    return f"uid:{row['uid']}"


def resolve_company_from_entity_key(conn: sqlite3.Connection, entity_key: str) -> Optional[int]:
    from pipeline import ensure_company

    if entity_key.startswith("uid:"):
        row = conn.execute(
            "SELECT id FROM companies WHERE uid = ?", (entity_key[4:],),
        ).fetchone()
        return int(row["id"]) if row else None
    # Legacy company:domain:/company:name: keys -- still needed to apply the
    # ~61k pre-uid snapshots already stored in D1 on a `pull --full`.
    if not entity_key.startswith("company:"):
        return None
    parts = entity_key.split(":", 2)
    if len(parts) != 3:
        return None
    kind, val = parts[1], parts[2]
    if kind == "domain":
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (val,)).fetchone()
        return row["id"] if row else ensure_company(conn, domain=val)
    if kind == "name":
        return ensure_company(conn, name=val)
    return None


def _lead_source_hash(
    lead_id: int, field_name: str, conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Hash of the leads column a personalization field was derived from.

    Pass `conn` when calling this in a loop. It used to open and close its own
    connection every time, which personalize-status did once per row -- ~110k
    connections on a real database.
    """
    col = _LEAD_SOURCE_FIELDS.get(field_name)
    if not col:
        return None
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        row = conn.execute(f"SELECT {col} FROM leads WHERE id = ?", (lead_id,)).fetchone()
    finally:
        if own_conn:
            conn.close()
    if not row or not row[col]:
        return None
    return hashlib.md5(str(row[col]).encode()).hexdigest()[:8]


def _company_source_hash(
    company_id: int, field_name: str, conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """As _lead_source_hash, for companies. Pass `conn` when looping."""
    col = _COMPANY_SOURCE_FIELDS.get(field_name)
    if not col:
        return None
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        row = conn.execute(f"SELECT {col} FROM companies WHERE id = ?", (company_id,)).fetchone()
    finally:
        if own_conn:
            conn.close()
    if not row or not row[col]:
        return None
    return hashlib.md5(str(row[col]).encode()).hexdigest()[:8]


def _lead_personalization_dict(conn: sqlite3.Connection, lead_id: int) -> dict:
    rows = conn.execute(
        "SELECT field_name, field_value, field_date, processed_at FROM lead_personalization WHERE lead_id = ?",
        (lead_id,),
    ).fetchall()
    return {r["field_name"]: dict(r) for r in rows}


def _company_personalization_dict(conn: sqlite3.Connection, company_id: int) -> dict:
    rows = conn.execute(
        "SELECT field_name, field_value, field_date, processed_at FROM company_personalization WHERE company_id = ?",
        (company_id,),
    ).fetchall()
    return {r["field_name"]: dict(r) for r in rows}


def resolve_personalization(lead_id: int) -> dict:
    """Merged mail-merge values (company fields, then lead overrides)."""
    conn = get_conn()
    row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    merged: dict = {}
    if row and row["company_id"]:
        for fname, rec in _company_personalization_dict(conn, row["company_id"]).items():
            merged[fname] = rec["field_value"]
            if rec.get("field_date"):
                merged[f"{fname}_date"] = rec["field_date"]
    for fname, rec in _lead_personalization_dict(conn, lead_id).items():
        merged[fname] = rec["field_value"]
        if rec.get("field_date"):
            merged[f"{fname}_date"] = rec["field_date"]
        elif f"{fname}_date" in merged:
            del merged[f"{fname}_date"]
    conn.close()
    return merged


def _personalization_sync_payload(rows: dict) -> tuple[dict, dict, Optional[str]]:
    values = {k: v["field_value"] for k, v in rows.items()}
    dates = {k: v["field_date"] for k, v in rows.items() if v.get("field_date")}
    at = max((v["processed_at"] for v in rows.values()), default=None)
    return values, dates, at


def personalize_set(
    lead_id: int,
    field_name: str,
    field_value: str,
    *,
    field_date: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    try:
        field_name = validate_personalization_field(field_name, scope="lead")
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    # The registry, not the name shape, decides where a field belongs. A field
    # already claimed by company scope is refused here rather than written to a
    # second home under the same name; an unclaimed one falls back to the old
    # `company_*` heuristic so nothing that worked before starts failing.
    scope, decided_by = resolve_scope(field_name, conn=conn)
    if scope == "company":
        if own_conn:
            conn.close()
        hint = ("is registered as company-scoped" if decided_by == "registry"
                else "looks company-scoped")
        return {"status": "error",
                "error": f"{field_name} {hint} — use company-personalize-set"}
    register_field(field_name, "lead", source="cli", conn=conn)
    conn.execute("""
        INSERT INTO lead_personalization (lead_id, field_name, field_value, field_date, source_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (lead_id, field_name) DO UPDATE SET
            field_value = excluded.field_value,
            field_date = excluded.field_date,
            source_hash = excluded.source_hash,
            processed_at = datetime('now')
    """, (lead_id, field_name, field_value, field_date, _lead_source_hash(lead_id, field_name)))
    # Bump updated_at so timestamp-based relay sync re-pushes the snapshot
    # with updated personalization data.
    conn.execute("UPDATE leads SET updated_at = datetime('now') WHERE id = ?", (lead_id,))
    if own_conn:
        conn.commit()
        conn.close()
    return {"status": "ok", "lead_id": lead_id, "field": field_name}


def personalize_set_batch(items: list[dict], *, conn: Optional[sqlite3.Connection] = None) -> dict:
    written = 0
    err_list = []
    for item in items:
        lid = item.get("lead_id")
        fname = item.get("field")
        fval = item.get("value")
        if not lid or not fname or fval is None:
            err_list.append({"item": item, "error": "missing lead_id, field, or value"})
            continue
        result = personalize_set(lid, fname, str(fval), field_date=item.get("date"), conn=conn)
        if result.get("status") == "error":
            err_list.append({"item": item, "error": result["error"]})
            continue
        written += 1
    return {"status": "ok", "written": written, "errors": err_list}


def company_personalize_set(
    field_name: str,
    field_value: str,
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
    field_date: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    # No `company_` prefix requirement. Calling this function IS the scope
    # declaration; demanding the name also look company-shaped rejected ordinary
    # company facts like `phone_google_maps`, `gm_rating` or `hours` outright.
    try:
        field_name = validate_personalization_field(field_name, scope="company")
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    cid = resolve_company_id(conn, company_id=company_id, domain=domain, name=name)
    if not cid:
        if own_conn:
            conn.close()
        return {"status": "error", "error": "company not found"}
    scope, decided_by = resolve_scope(field_name, conn=conn)
    if scope == "lead" and decided_by == "registry":
        if own_conn:
            conn.close()
        return {"status": "error",
                "error": f"{field_name} is registered as a LEAD personalization field. "
                         "A field belongs to one scope — use personalize-set, or "
                         "choose a different name for the company value."}
    register_field(field_name, "company", source="cli", conn=conn)
    conn.execute("""
        INSERT INTO company_personalization (company_id, field_name, field_value, field_date, source_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (company_id, field_name) DO UPDATE SET
            field_value = excluded.field_value,
            field_date = excluded.field_date,
            source_hash = excluded.source_hash,
            processed_at = datetime('now')
    """, (cid, field_name, field_value, field_date, _company_source_hash(cid, field_name)))
    # Bump updated_at so timestamp-based relay sync re-pushes the company snapshot.
    conn.execute("UPDATE companies SET updated_at = datetime('now') WHERE id = ?", (cid,))
    if own_conn:
        conn.commit()
        conn.close()
    return {"status": "ok", "company_id": cid, "field": field_name}


def company_personalize_set_batch(items: list[dict], *, conn: Optional[sqlite3.Connection] = None) -> dict:
    written = 0
    errors = []
    for item in items:
        fname = item.get("field")
        fval = item.get("value")
        if not fname or fval is None:
            errors.append({"item": item, "error": "missing field or value"})
            continue
        result = company_personalize_set(
            fname, str(fval),
            company_id=item.get("company_id"),
            domain=item.get("domain"),
            name=item.get("name") or item.get("company"),
            field_date=item.get("date"),
            conn=conn,
        )
        if result.get("status") == "ok":
            written += 1
        else:
            errors.append({"item": item, "error": result.get("error")})
    return {"status": "ok", "written": written, "errors": errors}


def personalize_get(lead_id: int, *, layer: str = "merged") -> dict:
    conn = get_conn()
    if layer == "lead":
        rows = _lead_personalization_dict(conn, lead_id)
    elif layer == "company":
        row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        rows = _company_personalization_dict(conn, row["company_id"]) if row and row["company_id"] else {}
    else:
        conn.close()
        return resolve_personalization(lead_id)
    conn.close()
    out: dict = {}
    for fname, rec in rows.items():
        out[fname] = rec["field_value"]
        if rec.get("field_date"):
            out[f"{fname}_date"] = rec["field_date"]
    return out


def company_personalize_get(
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    conn = get_conn()
    cid = resolve_company_id(conn, company_id=company_id, domain=domain, name=name)
    if not cid:
        conn.close()
        return {}
    rows = _company_personalization_dict(conn, cid)
    conn.close()
    out: dict = {}
    for fname, rec in rows.items():
        out[fname] = rec["field_value"]
        if rec.get("field_date"):
            out[f"{fname}_date"] = rec["field_date"]
    return out


def personalize_pending(fields: list[str], limit: int = 50) -> list[dict]:
    lead_fields = [f for f in fields if not looks_company_scoped(f)]
    if not lead_fields:
        return []
    conn = get_conn()
    conditions = " OR ".join(
        "l.id NOT IN (SELECT lead_id FROM lead_personalization WHERE field_name = ?)"
        for _ in lead_fields
    )
    rows = conn.execute(
        f"SELECT l.id, l.name, l.email, l.company FROM leads l WHERE {conditions} LIMIT ?",
        (*lead_fields, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def company_personalize_pending(fields: list[str], limit: int = 50) -> list[dict]:
    # Every requested field is company-scoped here: the caller asked the company
    # function. Filtering by name shape hid any field that didn't happen to start
    # with `company_`, so `phone_google_maps` never showed as pending.
    company_fields = [f for f in fields if str(f or "").strip()]
    if not company_fields:
        return []
    conn = get_conn()
    conditions = " OR ".join(
        """c.id NOT IN (SELECT company_id FROM company_personalization WHERE field_name = ?)"""
        for _ in company_fields
    )
    rows = conn.execute(
        f"""SELECT c.id AS company_id, c.name, c.domain,
                   (SELECT COUNT(*) FROM leads l WHERE l.company_id = c.id) AS lead_count
            FROM companies c
            WHERE ({conditions})
            ORDER BY lead_count DESC
            LIMIT ?""",
        (*company_fields, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def personalize_status() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    with_lead = conn.execute("SELECT COUNT(DISTINCT lead_id) FROM lead_personalization").fetchone()[0]
    stale = 0
    for row in conn.execute(
        "SELECT lead_id, field_name, source_hash FROM lead_personalization WHERE source_hash IS NOT NULL"
    ).fetchall():
        if _lead_source_hash(row["lead_id"], row["field_name"], conn) != row["source_hash"]:
            stale += 1
    conn.close()
    return {"total_leads": total, "personalized": with_lead, "pending": total - with_lead, "stale": stale}


def company_personalize_status() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    with_co = conn.execute("SELECT COUNT(DISTINCT company_id) FROM company_personalization").fetchone()[0]
    stale = 0
    for row in conn.execute(
        "SELECT company_id, field_name, source_hash FROM company_personalization WHERE source_hash IS NOT NULL"
    ).fetchall():
        if _company_source_hash(row["company_id"], row["field_name"], conn) != row["source_hash"]:
            stale += 1
    conn.close()
    return {"total_companies": total, "personalized": with_co, "pending": total - with_co, "stale": stale}


def personalize_clear_preview(
    lead_id: Optional[int] = None, field: Optional[str] = None, clear_all: bool = False,
) -> dict:
    """Row counts personalize_clear would delete, by scope. Nothing written."""
    conn = get_conn()
    try:
        lead_n = company_n = 0
        if clear_all:
            lead_n = conn.execute("SELECT COUNT(*) n FROM lead_personalization").fetchone()["n"]
            company_n = conn.execute(
                "SELECT COUNT(*) n FROM company_personalization").fetchone()["n"]
        elif lead_id and field:
            lead_n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization WHERE lead_id = ? AND field_name = ?",
                (lead_id, field)).fetchone()["n"]
        elif lead_id:
            lead_n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization WHERE lead_id = ?",
                (lead_id,)).fetchone()["n"]
        elif field:
            lead_n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization WHERE field_name = ?",
                (field,)).fetchone()["n"]
            company_n = conn.execute(
                "SELECT COUNT(*) n FROM company_personalization WHERE field_name = ?",
                (field,)).fetchone()["n"]
        return {"status": "ok", "lead_rows": lead_n, "company_rows": company_n,
                "total": lead_n + company_n}
    finally:
        conn.close()


# Deleting more than this many rows needs an explicit confirm. `--field X` with
# no --lead-id is org-wide across BOTH scopes: it reads like "remove my test
# value" and behaves like "drop this column from every record you have".
_CLEAR_CONFIRM_THRESHOLD = 10


def personalize_clear(
    lead_id: Optional[int] = None,
    field: Optional[str] = None,
    clear_all: bool = False,
    confirm: bool = False,
) -> dict:
    if not confirm:
        preview = personalize_clear_preview(lead_id, field, clear_all)
        if preview["total"] > _CLEAR_CONFIRM_THRESHOLD:
            return {
                **preview,
                "status": "error",
                "error": (
                    f"refusing to delete {preview['total']} personalization rows "
                    f"({preview['lead_rows']} lead, {preview['company_rows']} company) "
                    "without confirmation — re-run with --yes if that is what you want"
                ),
            }
    conn = get_conn()
    count = 0
    if clear_all:
        count += conn.execute("DELETE FROM lead_personalization").rowcount
        count += conn.execute("DELETE FROM company_personalization").rowcount
    elif lead_id and field:
        count = conn.execute(
            "DELETE FROM lead_personalization WHERE lead_id = ? AND field_name = ?", (lead_id, field),
        ).rowcount
    elif lead_id:
        count = conn.execute("DELETE FROM lead_personalization WHERE lead_id = ?", (lead_id,)).rowcount
    elif field:
        # Clear from BOTH scopes. Routing this by name shape meant a company
        # field that doesn't start with `company_` (now legal -- see
        # company_personalize_set) could never be cleared: the delete went to
        # lead_personalization and silently removed nothing.
        count = conn.execute(
            "DELETE FROM lead_personalization WHERE field_name = ?", (field,)).rowcount
        count += conn.execute(
            "DELETE FROM company_personalization WHERE field_name = ?", (field,)).rowcount
    else:
        conn.close()
        return {"status": "error", "error": "Specify --lead-id, --field, or --all"}
    conn.commit()
    conn.close()
    return {"status": "ok", "deleted": count}


def build_company_sync_payload(conn: sqlite3.Connection, company_id: int) -> dict:
    row = conn.execute(
        "SELECT name, domain, industry, headcount FROM companies WHERE id = ?", (company_id,),
    ).fetchone()
    if not row:
        return {}
    payload: dict = {"name": row["name"]}
    if row["domain"]:
        payload["domain"] = row["domain"]
    if row["industry"]:
        payload["industry"] = row["industry"]
    if row["headcount"]:
        payload["headcount"] = row["headcount"]
    # The relay keys the snapshot by uid now; domain and normalized name are
    # aliases so relay_entity_aliases can still map either one back to this
    # company (mirrors leads' aliases -- see _assemble_lead_core_sync_payload).
    aliases: list[str] = []
    if row["domain"]:
        aliases.append(str(row["domain"]).strip().lower())
    if row["name"]:
        aliases.append(str(row["name"]).strip().lower())
    # A company can legitimately own more than one domain (website vs
    # email-sending vs per-branch) -- company_identities tracks those; fold
    # any beyond the primary companies.domain into aliases too, so the relay
    # can map any of them back to this uid.
    identity_rows = conn.execute(
        "SELECT identity_value_normalized, role, label, purpose, verified_mx FROM company_identities "
        "WHERE company_id = ? AND identity_type = 'domain'",
        (company_id,),
    ).fetchall()
    # Only domains that belong to exactly ONE company become aliases. The
    # relay's relay_entity_aliases is PRIMARY KEY (org, entity_type, alias)
    # with last-writer-wins on conflict, so a brand domain shared by 22 Hilton
    # properties would have all 22 fighting over one alias row -- the mapping
    # would flip to whichever pushed most recently and resolve inbound
    # webhooks to an arbitrary property. An alias has to identify one entity
    # to be worth anything; a shared domain doesn't, so it is not sent as one.
    # It still round-trips in full via domain_identities below, which is
    # per-company payload and has no such collision.
    for id_row in identity_rows:
        value = id_row["identity_value_normalized"]
        shared = conn.execute(
            "SELECT 1 FROM company_identities WHERE identity_type = 'domain'"
            "  AND identity_value_normalized = ? AND company_id != ? LIMIT 1",
            (value, company_id),
        ).fetchone()
        if not shared:
            aliases.append(value)
    seen: set[str] = set()
    aliases = [a for a in aliases if a and not (a in seen or seen.add(a))]
    if aliases:
        payload["aliases"] = aliases
    # Stage D8: the relay stores/returns whatever this payload contains
    # verbatim (confirmed against wbhk-worker/relay-db.js -- no server-side
    # allowlist), so a richer, structured field alongside the flat aliases
    # array round-trips role/label/verified_mx too, not just the bare domain
    # string. aliases itself is untouched -- the relay's alias-resolution
    # index needs plain strings and already works correctly.
    if identity_rows:
        payload["domain_identities"] = [
            {
                "domain": r["identity_value_normalized"],
                "role": r["role"],
                "label": r["label"],
                "purpose": r["purpose"],
                "verified_mx": r["verified_mx"],
            }
            for r in identity_rows
        ]
    # Public emails discovered for the brand (domain_discovery.py writes these
    # as identity_type='public_email'). Same reasoning as domain_identities
    # above: without this they would exist only on the machine that found
    # them, and a fresh install pulling the company down would silently lose
    # the one contact address we managed to find.
    public_email_rows = conn.execute(
        "SELECT identity_value_normalized, role, label, purpose, verified_mx FROM company_identities "
        "WHERE company_id = ? AND identity_type = 'public_email'",
        (company_id,),
    ).fetchall()
    if public_email_rows:
        payload["public_emails"] = [
            {
                "email": r["identity_value_normalized"],
                "role": r["role"],
                "label": r["label"],
                "verified_mx": r["verified_mx"],
            }
            for r in public_email_rows
        ]
    pers = _company_personalization_dict(conn, company_id)
    if pers:
        values, dates, at = _personalization_sync_payload(pers)
        payload["personalization"] = values
        if dates:
            payload["personalization_dates"] = dates
        if at:
            payload["personalization_at"] = at
    return payload


def inspect_sync_company(conn: sqlite3.Connection, company_id: int) -> dict:
    """Full company_update payload for one company, for sync auditing/troubleshooting."""
    row = conn.execute(
        "SELECT id, name, domain FROM companies WHERE id = ?", (company_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "company_id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "full_sync_payload": build_company_sync_payload(conn, company_id),
    }


def apply_agent_company_sync_payload(company_id: int, payload: dict, *, conn=None) -> None:
    from pipeline import DEFAULT_ORG_ID, ensure_company

    own_conn = conn is None
    conn = conn or get_conn()

    # Write company data fields authoritatively from company snapshot
    company_name = payload.get("name") or ""
    company_domain = payload.get("domain") or ""
    company_industry = payload.get("industry") or ""
    company_headcount = payload.get("headcount") or ""

    # authoritative_domain_attach=True: this payload was resolved against a
    # SPECIFIC, already-known company_id via its uid/entity_key (see the
    # caller in pipeline_sync.py) -- it is not a name-only guess that might
    # land on an unrelated company, so it's safe to attach the domain
    # directly to a name-matched row with no domain yet, same as
    # resolve_lead()'s own website-vs-email-domain reconciliation. Without
    # this, Stage D3's "never silently attach on an ambiguous name-only
    # match" guard would (correctly, for the ambiguous case it targets, but
    # wrongly here) create a stray second row instead of updating the one
    # this snapshot is actually for.
    resolved_id = ensure_company(
        conn,
        name=company_name,
        domain=company_domain,
        industry=company_industry,
        headcount=company_headcount,
        authoritative=True,
        authoritative_domain_attach=True,
    )

    # Stage D8: reconstruct every domain this company is known to own, not
    # just the primary one -- previously, a company_identities domain row
    # beyond companies.domain only existed on the machine that created it; a
    # fresh install pulling this company down would silently lose it.
    target_id = resolved_id or company_id
    if target_id:
        domain_identities = payload.get("domain_identities")
        if isinstance(domain_identities, list) and domain_identities:
            # Rich, structured form (this plan) -- full-fidelity
            # reconstruction of role/label/purpose/verified_mx, not just the
            # domain. `purpose` is additive: snapshots pushed by an older client
            # simply omit it and COALESCE keeps whatever is already local.
            for entry in domain_identities:
                if not isinstance(entry, dict):
                    continue
                domain_val = normalize_company_domain(entry.get("domain"))
                if not domain_val or domain_val == normalize_company_domain(company_domain):
                    continue
                conn.execute(
                    """INSERT INTO company_identities
                           (org_id, company_id, identity_type, identity_value_normalized, role, label, purpose, verified_mx, source)
                       VALUES (?, ?, 'domain', ?, ?, ?, ?, ?, 'relay_pull')
                       ON CONFLICT (org_id, company_id, identity_type, identity_value_normalized) DO UPDATE SET
                           role = COALESCE(excluded.role, company_identities.role),
                           label = COALESCE(excluded.label, company_identities.label),
                           purpose = COALESCE(excluded.purpose, company_identities.purpose),
                           verified_mx = COALESCE(excluded.verified_mx, company_identities.verified_mx)""",
                    (DEFAULT_ORG_ID, target_id, domain_val, entry.get("role"),
                     entry.get("label"), entry.get("purpose"), entry.get("verified_mx")),
                )
        else:
            # Backward-compat fallback for snapshots pushed before this
            # change (or by an older client): aliases is a flat list mixing
            # the company's own lowercased name with its domain(s) -- pick
            # out anything that's syntactically a valid domain and isn't
            # already the primary one just written above.
            for alias in payload.get("aliases") or []:
                domain_val = normalize_company_domain(alias if isinstance(alias, str) else None)
                if not domain_val or domain_val == normalize_company_domain(company_domain):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO company_identities
                           (org_id, company_id, identity_type, identity_value_normalized, source)
                       VALUES (?, ?, 'domain', ?, 'relay_alias')""",
                    (DEFAULT_ORG_ID, target_id, domain_val),
                )

        # Public emails found for the brand, same reconstruction as above.
        for entry in payload.get("public_emails") or []:
            if not isinstance(entry, dict):
                continue
            email_val = (entry.get("email") or "").strip().lower()
            if "@" not in email_val:
                continue
            conn.execute(
                """INSERT INTO company_identities
                       (org_id, company_id, identity_type, identity_value_normalized, role, label, verified_mx, source)
                   VALUES (?, ?, 'public_email', ?, ?, ?, ?, 'relay_pull')
                   ON CONFLICT (org_id, company_id, identity_type, identity_value_normalized) DO UPDATE SET
                       role = COALESCE(excluded.role, company_identities.role),
                       label = COALESCE(excluded.label, company_identities.label),
                       verified_mx = COALESCE(excluded.verified_mx, company_identities.verified_mx)""",
                (DEFAULT_ORG_ID, target_id, email_val, entry.get("role"),
                 entry.get("label"), entry.get("verified_mx")),
            )

    # Existing personalization logic
    _apply_personalization_payload(
        company_id, payload,
        table="company_personalization", id_col="company_id", entity_id=company_id,
        conn=conn,
    )

    if own_conn:
        conn.commit()
        conn.close()


def _apply_personalization_payload(
    _entity_id_unused: int,
    payload: dict,
    *,
    table: str,
    id_col: str,
    entity_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    pers = payload.get("personalization") or {}
    if not pers:
        return
    dates = payload.get("personalization_dates") or {}
    p_at = payload.get("personalization_at", datetime.now(timezone.utc).isoformat())
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        for fname, fval in pers.items():
            conn.execute(f"""
                INSERT INTO {table} ({id_col}, field_name, field_value, field_date, processed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT ({id_col}, field_name) DO UPDATE SET
                    field_value = excluded.field_value,
                    field_date = excluded.field_date,
                    processed_at = excluded.processed_at
                WHERE excluded.processed_at > {table}.processed_at
            """, (entity_id, fname, fval, dates.get(fname), p_at))
        # Bump the parent record's updated_at so timestamp-based relay sync
        # knows personalization changed and re-pushes the snapshot.
        parent_table = "companies" if table == "company_personalization" else "leads"
        parent_id_col = "id"
        conn.execute(f"UPDATE {parent_table} SET updated_at = datetime('now') WHERE {parent_id_col} = ?", (entity_id,))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def cleanup_campaign_rules(dry_run: bool = False) -> dict:
    conn = get_conn()
    bad_rows = conn.execute("""
        SELECT id, workspace_id, source_platform, created_at
        FROM campaign_workspace_map
        WHERE campaign_platform_id IS NULL AND campaign_name_normalized IS NULL
    """).fetchall()
    count = len(bad_rows)
    if not dry_run and count > 0:
        conn.execute("""
            DELETE FROM campaign_workspace_map
            WHERE campaign_platform_id IS NULL AND campaign_name_normalized IS NULL
        """)
        conn.commit()
    conn.close()
    return {"status": "ok", "removed": count if not dry_run else 0, "found": count, "dry_run": dry_run}
