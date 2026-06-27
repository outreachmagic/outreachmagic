"""Cross-platform lead snapshot build/apply for relay sync."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from activity_sync import (
    apply_activity_sync_payload,
    attach_activity_to_sync_payload,
    compute_lead_activity_from_events,
    _read_workspace_activity_row,
)
from constants import COMPANY_DOMAIN_SQL
from db_conn import get_conn
from workspace_routing import (
    DEFAULT_ORG_ID,
    ensure_organization,
    import_extra_from_entity_key,
    lead_external_id_value,
    normalize_linkedin,
    parse_entity_key,
    upsert_all_identities,
    upsert_workspace_lead,
)

SYNC_PROFILE_FIELDS = (
    "name", "company", "title", "industry", "headcount", "stage", "notes",
    "location_city", "location_state", "location_country",
    "email_verification_status",
)

WORKSPACE_ACTIVITY_SELECT = """
    status, current_status_label, current_status_sentiment, contact_priority,
    COALESCE(last_contacted_at, last_activity_at) AS last_contacted_at,
    last_activity_at, email_sent_count, linkedin_sent_count, total_replies_count
"""


def _personalization_sync_payload(rows: dict) -> tuple[dict, dict, Optional[str]]:
    values = {k: v["field_value"] for k, v in rows.items()}
    dates = {k: v["field_date"] for k, v in rows.items() if v.get("field_date")}
    at = max((v["processed_at"] for v in rows.values()), default=None)
    return values, dates, at


def _resolve_workspace_identity(conn, workspace_slug: str):
    from pipeline import resolve_workspace_identity

    return resolve_workspace_identity(conn, workspace_slug)


def _prefetch_membership(
    prefetch: dict,
    lead_id: int,
    *,
    workspace_id: Optional[str] = None,
    workspace_slug: Optional[str] = None,
) -> Optional[dict]:
    for mem in prefetch.get("memberships", {}).get(lead_id, []):
        if workspace_id and mem["workspace_id"] == workspace_id:
            return mem
        if workspace_slug and mem["slug"] == workspace_slug:
            return mem
    return None


def _resolve_sync_workspace(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_slug: Optional[str],
    prefetch: Optional[dict],
) -> tuple[Optional[str], Optional[sqlite3.Row]]:
    ws_id = None
    if workspace_slug:
        ws_row = _resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if prefetch:
        mem = _prefetch_membership(
            prefetch,
            lead_id,
            workspace_id=ws_id,
            workspace_slug=workspace_slug if not ws_id else None,
        )
        if mem:
            return mem["workspace_id"], mem["wl_row"]
        if ws_id is None:
            mems = prefetch.get("memberships", {}).get(lead_id, [])
            if len(mems) == 1:
                return mems[0]["workspace_id"], mems[0]["wl_row"]
    if ws_id is None:
        wl = conn.execute(
            "SELECT workspace_id FROM workspace_leads WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        if wl:
            ws_id = wl["workspace_id"]
    if ws_id and prefetch:
        mem = _prefetch_membership(prefetch, lead_id, workspace_id=ws_id)
        if mem:
            return ws_id, mem["wl_row"]
    if ws_id:
        wl_row = conn.execute(
            f"""SELECT {WORKSPACE_ACTIVITY_SELECT}
                FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
        return ws_id, wl_row
    return None, None


def _assemble_lead_core_sync_payload(
    row: sqlite3.Row,
    *,
    identity_rows: list,
    external_id: Optional[str],
    personalization_rows: list,
) -> dict:
    """Org-wide lead profile for relay core snapshot."""
    payload: dict = {}
    for field in SYNC_PROFILE_FIELDS:
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if row["email"]:
        payload["email"] = row["email"]
    if row["linkedin_url"]:
        payload["linkedin"] = row["linkedin_url"]
    for id_row in identity_rows:
        payload[id_row["identity_type"]] = id_row["identity_value_normalized"]
    if row["latest_sender"]:
        payload["latest_sender"] = row["latest_sender"]
    if row["latest_sender_platform"]:
        payload["latest_sender_platform"] = row["latest_sender_platform"]
    if row["email_verified_at"]:
        payload["email_verified_at"] = row["email_verified_at"]
    if "email_verification_status" in row.keys() and row["email_verification_status"]:
        payload["email_verification_status"] = row["email_verification_status"]
    if "original_lev_source" in row.keys() and row["original_lev_source"]:
        payload["original_lev_source"] = row["original_lev_source"]
    if "latest_lev_source" in row.keys() and row["latest_lev_source"]:
        payload["lev_source"] = row["latest_lev_source"]
        payload["latest_lev_source"] = row["latest_lev_source"]
    if row["company_domain"]:
        payload["company_domain"] = row["company_domain"]
    for hq in ("hq_city", "hq_state", "hq_country"):
        if row[hq]:
            payload[hq] = row[hq]
    if external_id:
        payload["external_id"] = external_id
    if row["latest_source_detail"]:
        payload["list_source"] = row["latest_source_detail"]
    if row["original_source_detail"] and row["original_source_detail"] != row["latest_source_detail"]:
        payload["import_name"] = row["original_source_detail"]
    for field in (
        "original_source",
        "original_source_detail",
        "original_source_platform",
        "original_source_at",
        "latest_source",
        "latest_source_detail",
        "latest_source_platform",
        "latest_source_at",
    ):
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if personalization_rows:
        pers = {
            r["field_name"]: {
                "field_value": r["field_value"],
                "field_date": r["field_date"],
                "processed_at": r["processed_at"],
            }
            for r in personalization_rows
        }
        values, dates, at = _personalization_sync_payload(pers)
        payload["personalization"] = values
        if dates:
            payload["personalization_dates"] = dates
        if at:
            payload["personalization_at"] = at
    return payload


def _assemble_lead_workspace_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    row: sqlite3.Row,
    *,
    ws_id: str,
    wl_row: sqlite3.Row,
    tags: list[str],
    linkedin_status: list,
) -> dict:
    """Per-workspace pipeline state for relay workspace snapshot."""
    payload: dict = {}
    if wl_row["current_status_label"]:
        payload["lead_status"] = wl_row["current_status_label"]
    if wl_row["current_status_sentiment"]:
        payload["lead_sentiment"] = wl_row["current_status_sentiment"]
    if wl_row["contact_priority"] is not None:
        payload["contact_order"] = wl_row["contact_priority"]
    if wl_row["status"] and wl_row["status"] != row["stage"]:
        payload["workspace_stage"] = wl_row["status"]
    payload["tags"] = list(tags)
    if linkedin_status:
        payload["linkedin_status"] = [
            {
                "sender_profile": r["sender_profile"],
                "is_connected": bool(r["is_connected"]),
                "is_request_pending": bool(r["is_request_pending"]),
            }
            for r in linkedin_status
        ]
    attach_activity_to_sync_payload(
        payload, conn, lead_id, workspace_id=ws_id, wl_row=wl_row,
    )
    return payload


def _load_lead_sync_prefetch(
    conn: sqlite3.Connection,
    org_id: str,
    lead_ids: list[int],
) -> dict:
    """Bulk-load rows used by build_lead_sync_payload for many leads at once."""
    if not lead_ids:
        return {
            "leads": {},
            "identities": {},
            "external_ids": {},
            "memberships": {},
            "personalization": {},
        }

    placeholders = ",".join("?" for _ in lead_ids)
    leads = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT l.*,
                       {COMPANY_DOMAIN_SQL},
                       co.hq_city AS hq_city,
                       co.hq_state AS hq_state,
                       co.hq_country AS hq_country,
                       COALESCE(co.name, l.company) AS company_display
                FROM leads l
                LEFT JOIN companies co ON l.company_id = co.id
                WHERE l.id IN ({placeholders})""",
            lead_ids,
        ).fetchall()
    }

    identities: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, identity_type, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders})
              AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id')""",
        [org_id, *lead_ids],
    ).fetchall():
        identities[r["lead_id"]].append(r)

    external_ids: dict[int, str] = {}
    for r in conn.execute(
        f"""SELECT lead_id, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders}) AND identity_type = 'external_id'""",
        [org_id, *lead_ids],
    ).fetchall():
        external_ids[r["lead_id"]] = r["identity_value_normalized"]

    workspace_slugs: dict[int, str] = {}
    memberships: dict[int, list[dict]] = {lid: [] for lid in lead_ids}
    membership_index: dict[tuple[int, str], dict] = {}
    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, w.slug, wl.status, wl.current_status_label,
                   wl.current_status_sentiment, wl.contact_priority,
                   COALESCE(wl.last_contacted_at, wl.last_activity_at) AS last_contacted_at,
                   wl.last_activity_at, wl.email_sent_count, wl.linkedin_sent_count,
                   wl.total_replies_count
            FROM workspace_leads wl
            JOIN workspaces w ON wl.workspace_id = w.id
            WHERE wl.lead_id IN ({placeholders})
            ORDER BY w.slug, wl.workspace_id""",
        lead_ids,
    ).fetchall():
        workspace_slugs.setdefault(r["lead_id"], r["slug"])
        mem = {
            "workspace_id": r["workspace_id"],
            "slug": r["slug"],
            "wl_row": r,
            "tags": [],
            "linkedin_status": [],
        }
        memberships[r["lead_id"]].append(mem)
        membership_index[(r["lead_id"], r["workspace_id"])] = mem

    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, wlt.tag
            FROM workspace_lead_tags wlt
            JOIN workspace_leads wl ON wl.workspace_id = wlt.workspace_id AND wl.lead_id = wlt.lead_id
            WHERE wl.lead_id IN ({placeholders})
            ORDER BY wlt.created_at""",
        lead_ids,
    ).fetchall():
        mem = membership_index.get((r["lead_id"], r["workspace_id"]))
        if mem:
            mem["tags"].append(r["tag"])

    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, lis.sender_profile, lis.is_connected,
                   lis.is_request_pending
            FROM workspace_lead_linkedin_status lis
            JOIN workspace_leads wl ON wl.workspace_id = lis.workspace_id AND wl.lead_id = lis.lead_id
            WHERE wl.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        mem = membership_index.get((r["lead_id"], r["workspace_id"]))
        if mem:
            mem["linkedin_status"].append(r)

    personalization: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, field_name, field_value, field_date, processed_at
            FROM lead_personalization
            WHERE lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        personalization[r["lead_id"]].append(r)

    return {
        "leads": leads,
        "identities": identities,
        "external_ids": external_ids,
        "workspace_slugs": workspace_slugs,
        "memberships": memberships,
        "personalization": personalization,
    }


def entity_key_from_prefetch(prefetch: dict, lead_id: int) -> str:
    row = prefetch["leads"].get(lead_id)
    if not row:
        return ""
    if row["email"]:
        return str(row["email"]).strip().lower()
    if row["linkedin_url"]:
        return str(row["linkedin_url"]).strip()
    id_rows = prefetch["identities"].get(lead_id) or []
    if id_rows:
        r = id_rows[0]
        return f"{r['identity_type']}:{r['identity_value_normalized']}"
    return ""


def _lead_row_for_sync(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    prefetch: Optional[dict] = None,
) -> Optional[sqlite3.Row]:
    if prefetch is not None:
        return prefetch["leads"].get(lead_id)
    return conn.execute(
        f"""SELECT l.*,
                  {COMPANY_DOMAIN_SQL},
                  co.hq_city AS hq_city,
                  co.hq_state AS hq_state,
                  co.hq_country AS hq_country,
                  COALESCE(co.name, l.company) AS company_display
           FROM leads l
           LEFT JOIN companies co ON l.company_id = co.id
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()


def build_lead_core_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    prefetch: Optional[dict] = None,
) -> dict:
    """Org-wide lead profile for relay lead_core_update."""
    row = _lead_row_for_sync(conn, org_id, lead_id, prefetch=prefetch)
    if not row:
        return {}
    if prefetch is not None:
        identity_rows = prefetch["identities"].get(lead_id) or []
        external_id = prefetch["external_ids"].get(lead_id)
        personalization_rows = prefetch["personalization"].get(lead_id) or []
    else:
        identity_rows = conn.execute(
            """SELECT identity_type, identity_value_normalized FROM lead_identities
               WHERE org_id = ? AND lead_id = ?
                 AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id')""",
            (org_id, lead_id),
        ).fetchall()
        external_id = lead_external_id_value(conn, org_id, lead_id)
        personalization_rows = conn.execute(
            "SELECT field_name, field_value, field_date, processed_at FROM lead_personalization WHERE lead_id = ?",
            (lead_id,),
        ).fetchall()
    row_dict = dict(row)
    original_lev, latest_lev = _lev_sources_for_lead(conn, lead_id)
    if original_lev:
        row_dict["original_lev_source"] = original_lev
    if latest_lev:
        row_dict["latest_lev_source"] = latest_lev
    return _assemble_lead_core_sync_payload(
        row_dict,
        identity_rows=identity_rows,
        external_id=external_id,
        personalization_rows=personalization_rows,
    )


def build_lead_workspace_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: str,
    prefetch: Optional[dict] = None,
) -> dict:
    """Per-workspace pipeline state for relay lead_workspace_update."""
    row = _lead_row_for_sync(conn, org_id, lead_id, prefetch=prefetch)
    if not row:
        return {}
    ws_id, wl_row = _resolve_sync_workspace(conn, lead_id, workspace_slug, prefetch)
    if not ws_id or not wl_row:
        return {}
    if prefetch is not None:
        mem = _prefetch_membership(prefetch, lead_id, workspace_id=ws_id)
        tags = mem["tags"] if mem else []
        linkedin_status = mem["linkedin_status"] if mem else []
    else:
        tags = [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at",
                (ws_id, lead_id),
            ).fetchall()
        ]
        linkedin_status = conn.execute(
            """SELECT sender_profile, is_connected, is_request_pending
               FROM workspace_lead_linkedin_status
               WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchall()
    return _assemble_lead_workspace_sync_payload(
        conn, lead_id, row,
        ws_id=ws_id,
        wl_row=wl_row,
        tags=tags,
        linkedin_status=linkedin_status,
    )


def build_lead_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: Optional[str] = None,
    prefetch: Optional[dict] = None,
) -> dict:
    """Merged core + workspace payload for inspect/export only; relay push uses split snapshots."""
    core = build_lead_core_sync_payload(conn, org_id, lead_id, prefetch=prefetch)
    if not workspace_slug:
        ws_id, _ = _resolve_sync_workspace(conn, lead_id, None, prefetch)
        if prefetch and ws_id:
            mems = prefetch.get("memberships", {}).get(lead_id) or []
            workspace_slug = mems[0]["slug"] if len(mems) == 1 else None
        elif ws_id:
            wl = conn.execute(
                "SELECT w.slug FROM workspaces w JOIN workspace_leads wl ON wl.workspace_id = w.id WHERE wl.lead_id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
            workspace_slug = wl["slug"] if wl else None
    ws_payload = (
        build_lead_workspace_sync_payload(
            conn, org_id, lead_id, workspace_slug=workspace_slug, prefetch=prefetch,
        )
        if workspace_slug
        else {}
    )
    merged = dict(core)
    merged.update(ws_payload)
    return merged


def _attribution_from_sync_payload(payload: dict) -> tuple[str, Optional[str], str]:
    """Map relay lead_core snapshot fields to resolve_lead source attribution."""
    source = (
        (payload.get("original_source") or "").strip()
        or (payload.get("latest_source") or "").strip()
        or "agent_sync"
    )
    source_detail = (
        (payload.get("original_source_detail") or "").strip()
        or (payload.get("latest_source_detail") or "").strip()
        or (payload.get("import_name") or "").strip()
        or (payload.get("list_source") or "").strip()
        or None
    )
    source_platform = (
        (payload.get("original_source_platform") or "").strip()
        or (payload.get("latest_source_platform") or "").strip()
        or "relay"
    )
    return source, source_detail, source_platform


_WEAK_ATTRIBUTION_SOURCES = frozenset({"agent_sync", "relay_sync", ""})
_WEAK_VERIFICATION_SOURCES = frozenset({"agent_sync", "relay_sync", "platform_bounce", ""})


def _is_weak_verification_source(source: Optional[str]) -> bool:
    return (source or "").strip() in _WEAK_VERIFICATION_SOURCES


def _lev_sources_for_lead(conn: sqlite3.Connection, lead_id: int) -> tuple[Optional[str], Optional[str]]:
    """Return (original_lev_source, latest_lev_source) from tool verification rows."""
    rows = conn.execute(
        """SELECT source, verified_at FROM lead_email_verification
           WHERE lead_id = ? AND source != 'platform_bounce'
           ORDER BY verified_at ASC""",
        (lead_id,),
    ).fetchall()
    tool_rows = [r for r in rows if not _is_weak_verification_source(r["source"])]
    if not tool_rows:
        return None, None
    original = (tool_rows[0]["source"] or "").strip() or None
    latest = (tool_rows[-1]["source"] or "").strip() or None
    return original, latest


def apply_attribution_from_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    payload: dict,
) -> None:
    """Restore source attribution from relay lead_core snapshot."""
    row = conn.execute(
        "SELECT original_source, original_source_detail FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    current_source = (row["original_source"] or "").strip() if row else ""
    payload_source = (payload.get("original_source") or "").strip()
    upgrade_original = bool(payload_source) and current_source in _WEAK_ATTRIBUTION_SOURCES

    sets: list[str] = []
    params: list = []
    for col in (
        "original_source",
        "original_source_detail",
        "original_source_platform",
        "original_source_at",
    ):
        val = payload.get(col)
        if val is not None and str(val).strip():
            if upgrade_original:
                sets.append(f"{col} = ?")
            else:
                sets.append(f"{col} = COALESCE({col}, ?)")
            params.append(val)
    for col in (
        "latest_source",
        "latest_source_detail",
        "latest_source_platform",
        "latest_source_at",
    ):
        val = payload.get(col)
        if val is not None and str(val).strip():
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(lead_id)
    conn.execute(
        f"UPDATE leads SET {', '.join(sets)} WHERE id = ?",
        params,
    )


def resolve_lead_from_agent_sync(
    entity_key: str,
    payload: dict,
    *,
    stage: str = "prospecting",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Create or match a lead from a relay agent entry (uses entity_key + full payload)."""
    from pipeline import resolve_lead

    extra = dict(import_extra_from_entity_key(entity_key))
    if payload.get("external_id"):
        extra["external_id"] = str(payload["external_id"])
    if payload.get("list_source"):
        extra["list_source"] = str(payload["list_source"])
    if payload.get("import_name"):
        extra["import_name"] = str(payload["import_name"])
    if payload.get("company_domain"):
        extra["company_domain"] = str(payload["company_domain"])
    profile = {
        k: payload[k]
        for k in ("email", "name", "company", "title", "industry", "headcount")
        if payload.get(k)
    }
    if payload.get("linkedin"):
        profile["linkedin"] = payload["linkedin"]
    source, source_detail, source_platform = _attribution_from_sync_payload(payload)
    return resolve_lead(
        email=payload.get("email"),
        linkedin_url=payload.get("linkedin"),
        name=payload.get("name", "Unknown"),
        company=payload.get("company"),
        title=payload.get("title"),
        industry=payload.get("industry"),
        headcount=payload.get("headcount"),
        stage=payload.get("stage") or payload.get("workspace_stage") or stage,
        notes=payload.get("notes"),
        company_domain=payload.get("company_domain"),
        location_city=payload.get("location_city"),
        location_state=payload.get("location_state"),
        location_country=payload.get("location_country"),
        hq_city=payload.get("hq_city"),
        hq_state=payload.get("hq_state"),
        hq_country=payload.get("hq_country"),
        import_extra=extra,
        import_batch=payload.get("import_batch_id"),
        source=source,
        source_detail=source_detail,
        source_platform=source_platform,
        overwrite=True,
        mark_cloud_pending=False,
        conn=conn,
    )


def apply_agent_lead_core_payload(
    lead_id: int,
    payload: dict,
    *,
    org_id: str = DEFAULT_ORG_ID,
    entity_key: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Apply org-wide lead profile from relay lead_core_update."""
    from bounces import verify_email
    from pipeline import (
        enrich_lead,
        ensure_company,
        link_lead_company,
        normalize_company_domain,
        _apply_personalization_payload,
    )

    own_conn = conn is None
    if own_conn:
        conn = get_conn()

    update_fields = {
        k: v for k, v in payload.items()
        if k in ("name", "title", "industry", "company", "headcount") and v is not None
    }
    if update_fields:
        enrich_lead(
            lead_id, overwrite=True, mark_cloud_pending=False, conn=conn, **update_fields,
        )

    loc_sets, loc_params = [], []
    for col in ("location_city", "location_state", "location_country"):
        if payload.get(col):
            loc_sets.append(f"{col} = ?")
            loc_params.append(payload[col])
    if loc_sets:
        loc_params.append(lead_id)
        conn.execute(
            f"UPDATE leads SET {', '.join(loc_sets)}, updated_at = datetime('now') WHERE id = ?",
            loc_params,
        )

    if payload.get("company_domain") or any(payload.get(k) for k in ("hq_city", "hq_state", "hq_country")):
        domain = normalize_company_domain(payload.get("company_domain"))
        ensure_company(
            conn,
            name=payload.get("company"),
            domain=domain,
            industry=payload.get("industry"),
            headcount=payload.get("headcount"),
            hq_city=payload.get("hq_city"),
            hq_state=payload.get("hq_state"),
            hq_country=payload.get("hq_country"),
        )
        link_lead_company(
            conn, lead_id,
            company=payload.get("company"),
            email=payload.get("email"),
            industry=payload.get("industry"),
            headcount=payload.get("headcount"),
        )

    identities: list[tuple[str, str]] = []
    if payload.get("external_id"):
        identities.append(("external_id", str(payload["external_id"])))
    itype, val = parse_entity_key(entity_key or "")
    if itype and val and itype != "email":
        if not any(t == itype and v == val for t, v in identities):
            identities.append((itype, val))
    if identities:
        upsert_all_identities(conn, org_id, lead_id, identities, source="agent_sync")

    personalization = payload.get("personalization")
    if personalization:
        _apply_personalization_payload(
            lead_id, payload, table="lead_personalization", id_col="lead_id", entity_id=lead_id,
            conn=conn,
        )

    if payload.get("notes"):
        conn.execute(
            "UPDATE leads SET notes = ?, updated_at = datetime('now') WHERE id = ?",
            (payload["notes"], lead_id),
        )

    apply_attribution_from_sync_payload(conn, lead_id, payload)

    if own_conn:
        conn.commit()
        conn.close()

    lev_source = (
        (payload.get("latest_lev_source") or payload.get("lev_source") or "").strip()
    )
    if payload.get("email_verification_status") and lev_source and not _is_weak_verification_source(lev_source):
        verify_email(
            lead_id,
            str(payload["email_verification_status"]),
            lev_source,
            verified_at=payload.get("email_verified_at"),
            conn=None if own_conn else conn,
        )


def apply_agent_lead_workspace_payload(
    lead_id: int,
    payload: dict,
    *,
    org_id: str = DEFAULT_ORG_ID,
    workspace_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Apply per-workspace pipeline state from relay lead_workspace_update."""
    from pipeline import parse_tags_value

    status_label = (payload.get("lead_status") or "").strip().lower().replace("_", " ") or None
    status_sentiment = (payload.get("lead_sentiment") or "").strip().lower() or None
    if status_sentiment == "neutral":
        print(f"[warn] lead_sentiment 'neutral' is no longer supported (use 'autoreply'). Skipping for lead_id={lead_id}.", file=sys.stderr)
        status_sentiment = None
    contact_pri = None
    if payload.get("contact_order") is not None:
        try:
            contact_pri = int(payload["contact_order"])
        except (ValueError, TypeError):
            pass
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    ensure_organization(conn)
    upsert_workspace_lead(
        conn, org_id, workspace_id, lead_id,
        status=payload.get("workspace_stage") or payload.get("stage", "prospecting"),
        current_status_label=status_label,
        current_status_sentiment=status_sentiment,
        contact_priority=contact_pri,
        mark_cloud_pending=False,
    )
    if "tags" in payload:
        conn.execute(
            "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
            (workspace_id, lead_id),
        )
        for tag in parse_tags_value(payload.get("tags")):
            tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (tag_id, workspace_id, lead_id, tag),
            )
    for li in payload.get("linkedin_status") or []:
        sender = normalize_linkedin(li.get("sender_profile"))
        if not sender:
            continue
        is_connected = bool(li.get("is_connected"))
        is_pending = bool(li.get("is_request_pending"))
        if not is_connected and not is_pending:
            continue
        now_ts = datetime.now(timezone.utc).isoformat()
        li_id = f"lis_{workspace_id}_{lead_id}_{sender[:20]}"
        conn.execute(
            """INSERT INTO workspace_lead_linkedin_status
               (id, workspace_id, lead_id, sender_profile, is_connected,
                is_request_pending, connected_at, request_sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (workspace_id, lead_id, sender_profile) DO UPDATE SET
                   is_connected = excluded.is_connected,
                   is_request_pending = excluded.is_request_pending,
                   updated_at = datetime('now')""",
            (li_id, workspace_id, lead_id, sender,
             int(is_connected), int(is_pending),
             now_ts if is_connected else None,
             now_ts if is_pending else None),
        )
    activity = payload.get("activity")
    if activity:
        apply_activity_sync_payload(
            conn, lead_id, workspace_id, activity, merge=True,
        )
    if own_conn:
        conn.commit()
        conn.close()


def inspect_sync_lead(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: Optional[str] = None,
) -> dict:
    """Compare stored, event-derived, and sync-payload activity for one lead."""
    ws_id = None
    if workspace_slug:
        ws_row = _resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if ws_id is None:
        wl = conn.execute(
            "SELECT workspace_id FROM workspace_leads WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        ws_id = wl["workspace_id"] if wl else None

    stored = _read_workspace_activity_row(conn, ws_id, lead_id) if ws_id else {}
    computed = compute_lead_activity_from_events(conn, lead_id)
    payload = build_lead_sync_payload(conn, org_id, lead_id, workspace_slug=workspace_slug)
    lead_row = conn.execute(
        "SELECT email, name, cloud_pending, last_contact_at FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    wl_row = None
    if ws_id:
        wl_row = conn.execute(
            """SELECT current_status_label, current_status_sentiment, status
               FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
    return {
        "lead_id": lead_id,
        "email": lead_row["email"] if lead_row else None,
        "name": lead_row["name"] if lead_row else None,
        "cloud_pending": bool(lead_row["cloud_pending"]) if lead_row else None,
        "workspace_slug": workspace_slug,
        "workspace_id": ws_id,
        "lead_status": wl_row["current_status_label"] if wl_row else None,
        "lead_sentiment": wl_row["current_status_sentiment"] if wl_row else None,
        "workspace_stage": wl_row["status"] if wl_row else None,
        "activity_stored": stored,
        "activity_computed_from_events": computed,
        "activity_sync_payload": payload.get("activity", {}),
        "full_sync_payload_keys": sorted(payload.keys()),
    }


def build_crm_entity_map_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Build relay payloads for pending crm_entity_map rows.

    Returns rows where ``cloud_pending = 1``, each with a ``kind`` field
    set to ``"crm_entity_map"``.
    """
    rows = conn.execute(
        """SELECT workspace_id, lead_id, platform,
                  crm_contact_id, crm_deal_id, crm_company_id,
                  crm_owner_id, last_synced_at, last_event_id_synced,
                  last_sync_status, sync_error, sync_hash,
                  cloud_pending, created_at, updated_at
           FROM crm_entity_map
           WHERE cloud_pending = 1"""
    ).fetchall()
    payloads = []
    for row in rows:
        d = dict(row)
        d["kind"] = "crm_entity_map"
        payloads.append(d)
    return payloads
