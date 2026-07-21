#!/usr/bin/env python3
"""Mail-merge personalization for leads and companies."""

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from constants import SHARED_EMAIL_DOMAINS
from db_conn import get_conn
from pipeline_utils import normalize_company_domain

_LEAD_SOURCE_FIELDS = {"first_name": "name"}
_COMPANY_SOURCE_FIELDS = {"company_name": "name"}


def is_company_personalization_field(field_name: str) -> bool:
    return field_name == "company_name" or field_name.startswith("company_")


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
    if is_company_personalization_field(field_name):
        return {"status": "error", "error": f"{field_name} is company-scoped — use company-personalize-set"}
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
        if is_company_personalization_field(fname):
            err_list.append({"item": item, "error": f"{fname} is company-scoped"})
            continue
        personalize_set(lid, fname, str(fval), field_date=item.get("date"), conn=conn)
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
    if not is_company_personalization_field(field_name):
        return {"status": "error", "error": f"{field_name} is not a company personalization field"}
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    cid = resolve_company_id(conn, company_id=company_id, domain=domain, name=name)
    if not cid:
        if own_conn:
            conn.close()
        return {"status": "error", "error": "company not found"}
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
    lead_fields = [f for f in fields if not is_company_personalization_field(f)]
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
    company_fields = [f for f in fields if is_company_personalization_field(f)]
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


def personalize_clear(lead_id: Optional[int] = None, field: Optional[str] = None, clear_all: bool = False) -> dict:
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
        if is_company_personalization_field(field):
            count = conn.execute("DELETE FROM company_personalization WHERE field_name = ?", (field,)).rowcount
        else:
            count = conn.execute("DELETE FROM lead_personalization WHERE field_name = ?", (field,)).rowcount
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
        "SELECT identity_value_normalized, role, label, verified_mx FROM company_identities "
        "WHERE company_id = ? AND identity_type = 'domain'",
        (company_id,),
    ).fetchall()
    for id_row in identity_rows:
        aliases.append(id_row["identity_value_normalized"])
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
        "SELECT identity_value_normalized, role, label, verified_mx FROM company_identities "
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
            # reconstruction of role/label/verified_mx, not just the domain.
            for entry in domain_identities:
                if not isinstance(entry, dict):
                    continue
                domain_val = normalize_company_domain(entry.get("domain"))
                if not domain_val or domain_val == normalize_company_domain(company_domain):
                    continue
                conn.execute(
                    """INSERT INTO company_identities
                           (org_id, company_id, identity_type, identity_value_normalized, role, label, verified_mx, source)
                       VALUES (?, ?, 'domain', ?, ?, ?, ?, 'relay_pull')
                       ON CONFLICT (org_id, identity_type, identity_value_normalized) DO UPDATE SET
                           role = COALESCE(excluded.role, company_identities.role),
                           label = COALESCE(excluded.label, company_identities.label),
                           verified_mx = COALESCE(excluded.verified_mx, company_identities.verified_mx)""",
                    (DEFAULT_ORG_ID, target_id, domain_val, entry.get("role"), entry.get("label"), entry.get("verified_mx")),
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
                   ON CONFLICT (org_id, identity_type, identity_value_normalized) DO UPDATE SET
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
