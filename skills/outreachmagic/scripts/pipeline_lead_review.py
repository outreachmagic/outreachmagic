"""Lead list review sheets — export/sync via hosted Google Sheets API."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Callable, Optional

from workspace_routing import resolve_workspace_identity


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", "_", (tag or "").strip().lower())

DETAIL_LEVELS = ("basic", "standard", "full", "custom")

BASIC_COLUMNS = [
    ("lead_id", "lead_id"),
    ("name", "name"),
    ("email", "email"),
    ("company", "company"),
    ("title", "title"),
    ("tags", "tags"),
    ("notes_action", "notes_action"),
]

STANDARD_EXTRA = [
    ("linkedin", "linkedin"),
    ("location_city", "location_city"),
    ("location_state", "location_state"),
    ("location_country", "location_country"),
    ("industry", "industry"),
    ("headcount", "headcount"),
    ("lead_status", "lead_status"),
    ("lead_sentiment", "lead_sentiment"),
    ("workspace_stage", "workspace_stage"),
    ("channel", "channel"),
    ("email_verification_status", "email_verification_status"),
    ("original_source", "original_source"),
    ("original_source_detail", "original_source_detail"),
    ("created_at", "created_at"),
    ("updated_at", "updated_at"),
]

FULL_EXTRA = [
    ("last_contacted_at", "last_contacted_at"),
    ("latest_sender", "latest_sender"),
    ("latest_outbound_subject", "latest_outbound_subject"),
    ("latest_outbound_preview", "latest_outbound_preview"),
    ("latest_inbound_subject", "latest_inbound_subject"),
    ("latest_inbound_preview", "latest_inbound_preview"),
]

SYNCABLE_FIELDS = frozenset({
    "lead_status",
    "workspace_stage",
    "stage",
    "lead_sentiment",
    "tags",
    "notes",
    "notes_action",
    "contact_order",
    "contact_priority",
})

LINKEDIN_STATUS_VALUES = frozenset({
    "connected",
    "pending",
    "none",
    "not_requested",
})

REVIEW_TEMPLATE_ALIASES = {
    "dedup": "dedup-review",
    "lead": "lead-review",
    "leads": "lead-review",
}


def normalize_review_template(template: str) -> str:
    key = (template or "").strip().lower()
    return REVIEW_TEMPLATE_ALIASES.get(key, template)


def _sender_col(sender: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (sender or "").lower()).strip("_")
    return f"linkedin_{slug or 'sender'}"


def resolve_columns(
    detail: str,
    *,
    custom_fields: Optional[list[str]] = None,
    sender_profiles: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Return (header_label, field_key) pairs for the chosen detail level."""
    level = (detail or "standard").strip().lower()
    if level == "custom":
        fields = [f.strip() for f in (custom_fields or []) if f and f.strip()]
        if not fields:
            raise ValueError("custom detail requires --fields")
        return [(f, f) for f in fields]

    cols = list(BASIC_COLUMNS)
    if level in ("standard", "full"):
        cols.extend(STANDARD_EXTRA)
    if level == "full":
        cols.extend(FULL_EXTRA)
        for sender in sender_profiles or []:
            cols.append((f"LinkedIn ({sender})", _sender_col(sender)))
    elif level != "basic" and level != "standard":
        raise ValueError(f"unknown detail level: {detail}")
    return cols


def _never_contacted_sql(alias: str = "wl") -> str:
    a = alias
    return f"""(
        COALESCE({a}.email_sent_count, 0) = 0
        AND COALESCE({a}.linkedin_sent_count, 0) = 0
        AND COALESCE({a}.total_replies_count, 0) = 0
        AND ({a}.last_contacted_at IS NULL OR TRIM({a}.last_contacted_at) = '')
        AND NOT EXISTS (SELECT 1 FROM events e WHERE e.lead_id = l.id)
    )"""


def load_workspace_leads_for_review(
    conn: sqlite3.Connection,
    workspace: str,
    *,
    tag: Optional[str] = None,
    stage: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5000,
    never_contacted: bool = False,
    no_email: bool = False,
    require_domain: bool = False,
    enrich_fn: Callable[..., list[dict]],
) -> list[dict]:
    """Load enriched lead rows for review export."""
    ws_row = resolve_workspace_identity(conn, workspace)
    if not ws_row:
        raise ValueError(f"workspace not found: {workspace}")
    ws_id = ws_row["id"]

    join_tags = ""
    if tag:
        join_tags = (
            " INNER JOIN workspace_lead_tags wlt "
            " ON wlt.workspace_id = wl.workspace_id AND wlt.lead_id = l.id "
            " AND wlt.tag = ? "
        )

    query = f"""
        SELECT l.*,
               COALESCE(co.name, l.company) AS company_display,
               co.domain AS company_domain,
               wl.status AS workspace_stage,
               wl.lead_status AS lead_status,
               wl.lead_sentiment AS lead_sentiment,
               wl.contact_order AS contact_order,
               wl.channel AS channel,
               wl.latest_sender AS workspace_latest_sender,
               COALESCE(wl.last_contacted_at, wl.last_activity_at) AS last_contacted_at,
               wl.email_sent_count,
               wl.linkedin_sent_count,
               wl.total_replies_count
        FROM leads l
        INNER JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
        LEFT JOIN companies co ON l.company_id = co.id
        {join_tags}
        WHERE 1=1
    """
    params: list[Any] = [ws_id]
    if tag:
        params.append(_normalize_tag(tag))
    if stage:
        query += " AND wl.status = ?"
        params.append(stage)
    if since:
        since_date = since.strip()
        if since_date.lower() == "today":
            from datetime import datetime
            since_date = datetime.now().strftime("%Y-%m-%d")
        query += " AND (l.created_at >= ? OR l.updated_at >= ?)"
        params.extend([since_date, since_date])
    if never_contacted:
        query += f" AND {_never_contacted_sql('wl')}"
    if no_email:
        query += " AND (l.email IS NULL OR TRIM(l.email) = '')"
    if require_domain:
        query += " AND co.domain IS NOT NULL AND TRIM(co.domain) != ''"
    query += " ORDER BY l.updated_at DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    enriched = enrich_fn(rows, workspace=workspace)
    return enriched


def _linkedin_cell(lead: dict, sender: str) -> str:
    for entry in lead.get("linkedin_status") or []:
        if str(entry.get("sender_profile") or "").strip().lower() == sender.strip().lower():
            if entry.get("is_connected"):
                return "connected"
            if entry.get("is_request_pending"):
                return "pending"
            return "none"
    return "not_requested"


def _message_previews(conn: sqlite3.Connection, lead_id: int) -> dict[str, str]:
    rows = conn.execute(
        """SELECT direction, subject, body_preview, created_at
           FROM events WHERE lead_id = ?
           ORDER BY created_at DESC LIMIT 40""",
        (lead_id,),
    ).fetchall()
    out = {
        "latest_outbound_subject": "",
        "latest_outbound_preview": "",
        "latest_inbound_subject": "",
        "latest_inbound_preview": "",
    }
    for row in rows:
        direction = (row["direction"] or "").strip().lower()
        if direction == "outbound":
            if not out["latest_outbound_subject"]:
                out["latest_outbound_subject"] = (row["subject"] or "")[:200]
                out["latest_outbound_preview"] = (row["body_preview"] or "")[:300]
        elif direction == "inbound":
            if not out["latest_inbound_subject"]:
                out["latest_inbound_subject"] = (row["subject"] or "")[:200]
                out["latest_inbound_preview"] = (row["body_preview"] or "")[:300]
        if out["latest_outbound_subject"] and out["latest_inbound_subject"]:
            break
    return out


def build_lead_row(
    lead: dict,
    columns: list[tuple[str, str]],
    *,
    conn: Optional[sqlite3.Connection] = None,
    sender_profiles: Optional[list[str]] = None,
) -> list[Any]:
    """Build one sheet row aligned to column keys."""
    company = lead.get("company_display") or lead.get("company") or ""
    tags = lead.get("tags") or []
    if isinstance(tags, list):
        tags_str = ";".join(tags)
    else:
        tags_str = str(tags or "")

    pers = lead.get("personalization") or {}
    if not isinstance(pers, dict):
        pers = {}

    previews = {}
    if conn is not None and any(k in dict(columns) for _, k in columns):
        previews = _message_previews(conn, int(lead["id"]))

    values: dict[str, Any] = {
        "lead_id": lead.get("id") or lead.get("lead_id"),
        "name": lead.get("name") or "",
        "email": lead.get("email") or "",
        "company": company,
        "title": lead.get("title") or "",
        "tags": tags_str,
        "notes_action": lead.get("notes") or "",
        "notes": lead.get("notes") or "",
        "linkedin": lead.get("linkedin") or lead.get("linkedin_url") or "",
        "location_city": lead.get("location_city") or "",
        "location_state": lead.get("location_state") or "",
        "location_country": lead.get("location_country") or "",
        "industry": lead.get("industry") or "",
        "headcount": lead.get("headcount") or "",
        "lead_status": lead.get("lead_status") or "",
        "lead_sentiment": lead.get("lead_sentiment") or "",
        "workspace_stage": lead.get("workspace_stage") or lead.get("stage") or "",
        "stage": lead.get("workspace_stage") or lead.get("stage") or "",
        "channel": lead.get("channel") or "",
        "email_verification_status": lead.get("email_verification_status") or "",
        "original_source": lead.get("original_source") or "",
        "original_source_detail": lead.get("original_source_detail") or "",
        "created_at": lead.get("created_at") or "",
        "updated_at": lead.get("updated_at") or "",
        "last_contacted_at": lead.get("last_contacted_at") or "",
        "latest_sender": lead.get("latest_sender") or lead.get("workspace_latest_sender") or "",
        "contact_order": lead.get("contact_order") if lead.get("contact_order") is not None else "",
        "contact_priority": lead.get("contact_order") if lead.get("contact_order") is not None else "",
        "company_domain": lead.get("company_domain") or "",
        **previews,
    }
    for key, val in pers.items():
        values[f"personalized_{key}"] = val

    for sender in sender_profiles or []:
        values[_sender_col(sender)] = _linkedin_cell(lead, sender)

    row: list[Any] = []
    for _label, key in columns:
        row.append(values.get(key, pers.get(key.replace("personalized_", ""), "")))
    return row


def list_workspace_senders(conn: sqlite3.Connection, workspace_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT sender_profile
           FROM workspace_lead_linkedin_status
           WHERE workspace_id = ?
           ORDER BY sender_profile""",
        (workspace_id,),
    ).fetchall()
    return [str(r["sender_profile"]) for r in rows if r["sender_profile"]]


def build_export_payload(
    conn: sqlite3.Connection,
    *,
    workspace: str,
    detail: str,
    title: str,
    custom_fields: Optional[list[str]] = None,
    tag: Optional[str] = None,
    stage: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5000,
    never_contacted: bool = False,
    no_email: bool = False,
    require_domain: bool = False,
    enrich_fn: Optional[Callable[..., list[dict]]] = None,
) -> dict[str, Any]:
    if enrich_fn is None:
        raise ValueError("enrich_fn is required")
    ws_row = resolve_workspace_identity(conn, workspace)
    if not ws_row:
        raise ValueError(f"workspace not found: {workspace}")
    senders = list_workspace_senders(conn, ws_row["id"]) if detail == "full" else []
    columns = resolve_columns(detail, custom_fields=custom_fields, sender_profiles=senders)
    leads = load_workspace_leads_for_review(
        conn,
        workspace,
        tag=tag,
        stage=stage,
        since=since,
        limit=limit,
        never_contacted=never_contacted,
        no_email=no_email,
        require_domain=require_domain,
        enrich_fn=enrich_fn,
    )
    headers = [label for label, _key in columns]
    rows = [
        build_lead_row(lead, columns, conn=conn, sender_profiles=senders)
        for lead in leads
    ]
    return {
        "template": "lead-review",
        "title": title,
        "detail": detail,
        "workspace": workspace,
        "headers": headers,
        "rows": rows,
        "count": len(rows),
    }


def _parse_tags_cell(raw: Any) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[,;]", text)
    return [p.strip().lower().replace(" ", "_") for p in parts if p.strip()]


def _set_linkedin_status(
    conn: sqlite3.Connection,
    workspace_id: str,
    lead_id: int,
    sender: str,
    status: str,
) -> None:
    from datetime import datetime, timezone
    from workspace_routing import normalize_linkedin

    sender_norm = normalize_linkedin(sender) or sender.strip().lower()
    if not sender_norm:
        return
    now = datetime.now(timezone.utc).isoformat()
    is_connected = 1 if status == "connected" else 0
    is_pending = 1 if status == "pending" else 0
    row_id = f"lis_{workspace_id}_{lead_id}_{sender_norm[:20]}"
    conn.execute(
        """INSERT INTO workspace_lead_linkedin_status
           (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending,
            connected_at, request_sent_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT (workspace_id, lead_id, sender_profile) DO UPDATE SET
             is_connected = excluded.is_connected,
             is_request_pending = excluded.is_request_pending,
             connected_at = CASE WHEN excluded.is_connected = 1 THEN COALESCE(excluded.connected_at, ?) ELSE connected_at END,
             request_sent_at = CASE WHEN excluded.is_request_pending = 1 THEN COALESCE(excluded.request_sent_at, ?) ELSE request_sent_at END,
             updated_at = datetime('now')""",
        (
            row_id,
            workspace_id,
            lead_id,
            sender_norm,
            is_connected,
            is_pending,
            now if is_connected else None,
            now if is_pending else None,
            now,
            now,
        ),
    )


def apply_lead_review_sync(
    conn: sqlite3.Connection,
    workspace_id: str,
    sheet_rows: list[dict[str, Any]],
    *,
    upsert_workspace_lead_fn: Callable,
    org_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply syncable fields from sheet rows back to the database."""
    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "applied",
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "changes": [],
        "errors": [],
    }

    for raw in sheet_rows:
        lead_id_raw = raw.get("lead_id") or raw.get("Lead ID") or raw.get("lead id")
        try:
            lead_id = int(lead_id_raw)
        except (TypeError, ValueError):
            summary["skipped"] += 1
            continue

        exists = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not exists:
            summary["errors"].append({"lead_id": lead_id, "error": "lead not found"})
            continue

        summary["processed"] += 1
        row_changes: dict[str, Any] = {"lead_id": lead_id}

        stage_val = raw.get("workspace_stage") or raw.get("stage") or raw.get("Workspace Stage")
        if stage_val is not None and str(stage_val).strip():
            row_changes["workspace_stage"] = str(stage_val).strip()
        for field in ("lead_status", "lead_sentiment"):
            val = raw.get(field)
            if val is not None and str(val).strip():
                row_changes[field] = str(val).strip()

        notes = raw.get("notes_action") or raw.get("notes")
        if notes is not None and str(notes).strip():
            row_changes["notes"] = str(notes).strip()

        contact = raw.get("contact_order") or raw.get("contact_priority")
        if contact is not None and str(contact).strip():
            try:
                row_changes["contact_order"] = int(contact)
            except ValueError:
                pass

        tags_cell = raw.get("tags")
        if tags_cell is not None:
            row_changes["tags"] = _parse_tags_cell(tags_cell)

        linkedin_updates: list[tuple[str, str]] = []
        for key, val in raw.items():
            if not str(key).startswith("linkedin_") and not str(key).lower().startswith("linkedin ("):
                continue
            status = str(val or "").strip().lower()
            if status not in LINKEDIN_STATUS_VALUES:
                continue
            sender = key
            if key.startswith("linkedin_"):
                sender = key[len("linkedin_"):].replace("_", " ")
            elif key.lower().startswith("linkedin ("):
                sender = key[key.find("(") + 1 : key.rfind(")")].strip()
            linkedin_updates.append((sender, status))

        if not row_changes.get("workspace_stage") and not row_changes.get("lead_status") and not row_changes.get(
            "lead_sentiment"
        ) and "notes" not in row_changes and "tags" not in row_changes and "contact_order" not in row_changes and not linkedin_updates:
            summary["skipped"] += 1
            continue

        if dry_run:
            if linkedin_updates:
                row_changes["linkedin"] = linkedin_updates
            summary["changes"].append(row_changes)
            summary["updated"] += 1
            continue

        ws_sets: list[str] = []
        ws_params: list[Any] = []
        if row_changes.get("workspace_stage"):
            ws_sets.extend(["status = ?", "cloud_pending = 1"])
            ws_params.append(row_changes["workspace_stage"])
        if row_changes.get("lead_status"):
            ws_sets.append("current_status_label = ?")
            ws_params.append(row_changes["lead_status"])
        if row_changes.get("lead_sentiment"):
            ws_sets.append("current_status_sentiment = ?")
            ws_params.append(row_changes["lead_sentiment"])
        if row_changes.get("contact_order") is not None:
            ws_sets.append("contact_priority = ?")
            ws_params.append(row_changes["contact_order"])
        if ws_sets:
            ws_sets.append("updated_at = datetime('now')")
            ws_params.extend([workspace_id, lead_id])
            conn.execute(
                f"UPDATE workspace_leads SET {', '.join(ws_sets)} "
                "WHERE workspace_id = ? AND lead_id = ?",
                ws_params,
            )
            if not conn.execute(
                "SELECT id FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
                (workspace_id, lead_id),
            ).fetchone():
                upsert_workspace_lead_fn(conn, org_id, workspace_id, lead_id)

        if "notes" in row_changes:
            conn.execute(
                "UPDATE leads SET notes = ?, updated_at = datetime('now'), cloud_pending = 1 WHERE id = ?",
                (row_changes["notes"], lead_id),
            )

        if "tags" in row_changes:
            desired = [_normalize_tag(t) for t in row_changes["tags"] if t]
            current_rows = conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
                (workspace_id, lead_id),
            ).fetchall()
            current = {str(r["tag"]) for r in current_rows}
            for tag in desired:
                if tag not in current:
                    tag_id = (
                        f"wlt_{workspace_id}_{lead_id}_"
                        f"{hashlib.md5(tag.encode()).hexdigest()[:8]}"
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO workspace_lead_tags
                           (id, workspace_id, lead_id, tag) VALUES (?, ?, ?, ?)""",
                        (tag_id, workspace_id, lead_id, tag),
                    )
            for tag in current:
                if tag not in desired:
                    conn.execute(
                        "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? AND tag = ?",
                        (workspace_id, lead_id, tag),
                    )

        for sender, status in linkedin_updates:
            _set_linkedin_status(conn, workspace_id, lead_id, sender, status)

        summary["changes"].append(row_changes)
        summary["updated"] += 1

    if not dry_run:
        conn.commit()
    return summary


def email_finder_candidates_from_leads(leads: list[dict]) -> list[dict]:
    """Shape OM leads into batch-find JSON rows (real domains only)."""
    out: list[dict] = []
    for lead in leads:
        domain = (lead.get("company_domain") or "").strip().lower().lstrip("@")
        if not domain or " " in domain or "." not in domain:
            continue
        if domain == (lead.get("company_display") or lead.get("company") or "").strip().lower():
            continue
        row = {
            "lead_id": lead.get("id") or lead.get("lead_id"),
            "name": lead.get("name") or "",
            "company_domain": domain,
            "company": lead.get("company_display") or lead.get("company") or "",
        }
        linkedin = lead.get("linkedin") or lead.get("linkedin_url") or ""
        if linkedin:
            row["linkedin"] = linkedin
        out.append(row)
    return out
