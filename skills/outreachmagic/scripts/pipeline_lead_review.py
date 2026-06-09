"""Lead list review sheets — export/sync via hosted Google Sheets API."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Callable, Optional

from constants import COMPANY_DOMAIN_SQL, require_professional_domain_clause
from workspace_routing import resolve_workspace_identity


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", "_", (tag or "").strip().lower())

DETAIL_LEVELS = ("basic", "standard", "full", "custom")

KEY_BG, KEY_TEXT = "#FFF3CD", "#856404"
EDIT_BG, EDIT_TEXT = "#D4EDDA", "#155724"
READ_BG, READ_TEXT = "#CCE5FF", "#004085"

COLUMN_GROUPS: dict[str, list[str]] = {
    "lead_info": ["name", "title", "email", "linkedin", "company", "industry", "headcount"],
    "location": ["location_city", "location_state", "location_country"],
    "workspace_state": ["workspace_stage", "lead_status", "lead_sentiment", "contact_priority"],
    "activity": [
        "email_sent_count",
        "linkedin_sent_count",
        "total_replies_count",
        "last_contacted_at",
    ],
    "personalization": ["personalized_first_name", "personalized_company_name"],
    "attribution": ["original_source", "original_source_detail", "latest_sender"],
    "timestamps": ["created_at", "updated_at"],
    "messaging": [
        "latest_outbound_subject",
        "latest_outbound_preview",
        "latest_inbound_subject",
        "latest_inbound_preview",
    ],
}

FIELD_DEFS: dict[str, dict[str, Any]] = {
    "lead_id": {"type": "key", "editable": False, "presets": ("basic", "standard", "full")},
    "name": {"type": "string", "editable": True, "scope": "lead", "presets": ("basic", "standard", "full")},
    "email": {"type": "string", "editable": True, "scope": "lead", "presets": ("basic", "standard", "full")},
    "company": {"type": "string", "editable": True, "scope": "lead", "presets": ("basic", "standard", "full")},
    "title": {"type": "string", "editable": True, "scope": "lead", "presets": ("basic", "standard", "full")},
    "tags": {"type": "tags", "editable": True, "scope": "tags", "presets": ("basic", "standard", "full")},
    "notes": {"type": "string", "editable": True, "scope": "notes", "presets": ("basic", "standard", "full")},
    "linkedin": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "location_city": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "location_state": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "location_country": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "industry": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "headcount": {"type": "string", "editable": True, "scope": "lead", "presets": ("standard", "full")},
    "workspace_stage": {"type": "string", "editable": True, "scope": "workspace", "presets": ("standard", "full")},
    "lead_status": {"type": "string", "editable": True, "scope": "workspace", "presets": ("standard", "full")},
    "lead_sentiment": {"type": "string", "editable": True, "scope": "workspace", "presets": ("standard", "full")},
    "contact_priority": {"type": "integer", "editable": True, "scope": "workspace", "presets": ("standard", "full")},
    "email_verification_status": {"type": "string", "editable": False, "presets": ("standard", "full")},
    "original_source": {"type": "string", "editable": False, "presets": ("standard", "full")},
    "original_source_detail": {"type": "string", "editable": False, "presets": ("standard", "full")},
    "created_at": {"type": "timestamp", "editable": False, "presets": ("standard", "full")},
    "updated_at": {"type": "timestamp", "editable": False, "presets": ("standard", "full")},
    "last_contacted_at": {"type": "timestamp", "editable": False, "presets": ("full",)},
    "email_sent_count": {"type": "integer", "editable": False, "presets": ("full",)},
    "linkedin_sent_count": {"type": "integer", "editable": False, "presets": ("full",)},
    "total_replies_count": {"type": "integer", "editable": False, "presets": ("full",)},
    "latest_sender": {"type": "string", "editable": False, "presets": ("full",)},
    "personalized_first_name": {
        "type": "string", "editable": True, "scope": "personalization_lead", "presets": ("full",),
    },
    "personalized_company_name": {
        "type": "string", "editable": True, "scope": "personalization_company", "presets": ("full",),
    },
    "latest_outbound_subject": {"type": "string", "editable": False, "presets": ("full",)},
    "latest_outbound_preview": {"type": "string", "editable": False, "presets": ("full",)},
    "latest_inbound_subject": {"type": "string", "editable": False, "presets": ("full",)},
    "latest_inbound_preview": {"type": "string", "editable": False, "presets": ("full",)},
}

PRESET_KEYS: dict[str, list[str]] = {
    "basic": ["lead_id", "name", "email", "company", "title", "tags", "notes"],
    "standard": [
        "lead_id", "name", "email", "company", "title", "tags", "notes",
        "linkedin", "location_city", "location_state", "location_country", "industry", "headcount",
        "workspace_stage", "lead_status", "lead_sentiment", "contact_priority",
        "email_verification_status", "original_source", "original_source_detail", "created_at", "updated_at",
    ],
    "full": [
        "lead_id", "name", "email", "company", "title", "tags", "notes",
        "linkedin", "location_city", "location_state", "location_country", "industry", "headcount",
        "workspace_stage", "lead_status", "lead_sentiment", "contact_priority",
        "email_verification_status", "original_source", "original_source_detail", "created_at", "updated_at",
        "last_contacted_at", "email_sent_count", "linkedin_sent_count", "total_replies_count", "latest_sender",
        "personalized_first_name", "personalized_company_name",
        "latest_outbound_subject", "latest_outbound_preview", "latest_inbound_subject", "latest_inbound_preview",
    ],
}

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


def review_export_filter_kwargs(args: Any) -> dict[str, Any]:
    """Extract optional lead-review export filters from an argparse namespace."""
    out: dict[str, Any] = {}
    for key in (
        "original_source",
        "original_source_detail",
        "latest_source",
        "latest_source_detail",
        "industry",
        "location_city",
        "location_state",
        "email_domain",
        "email_verification_status",
    ):
        val = getattr(args, key, None)
        if val:
            out[key] = val
    for key in ("headcount_min", "headcount_max"):
        val = getattr(args, key, None)
        if val is not None:
            out[key] = val
    return out


def normalize_review_template(template: str) -> str:
    key = (template or "").strip().lower()
    return REVIEW_TEMPLATE_ALIASES.get(key, template)


def _sender_col(sender: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (sender or "").lower()).strip("_")
    return f"linkedin_{slug or 'sender'}"


def _humanize_key(key: str) -> str:
    text = key.replace("personalized_", "").replace("_", " ")
    return " ".join(part.capitalize() for part in text.split())


def _field_def(key: str) -> dict[str, Any]:
    return FIELD_DEFS.get(key, {"type": "string", "editable": True, "scope": "lead", "presets": ()})


def _default_label(key: str) -> str:
    defn = _field_def(key)
    if defn.get("type") == "key":
        return key
    title = _humanize_key(key)
    if not defn.get("editable"):
        return f"🔒 {title}"
    return f"✏️ {title}"


def _default_note(key: str) -> str:
    defn = _field_def(key)
    if defn.get("type") == "key":
        return "Stable row key — do not edit or rename this column"
    if not defn.get("editable"):
        return "Read-only — computed from event data. Cannot be edited from the sheet."
    scope = defn.get("scope")
    if scope == "company":
        return "Edits sync to the shared companies table for all leads linked to this company."
    if scope == "workspace":
        return "Edits sync back to workspace_leads for this lead in this workspace."
    if scope in ("personalization_lead", "personalization_company"):
        return "Edits sync back to personalization fields for this lead."
    if scope == "tags":
        return "Edits sync back as workspace tags for this lead."
    return "Edits sync back to your OutreachMagic database for this lead. Do not rename this header."


def _default_format(key: str) -> dict[str, Any]:
    defn = _field_def(key)
    if defn.get("type") == "key":
        return {"backgroundColor": KEY_BG, "textColor": KEY_TEXT, "bold": False}
    if defn.get("editable"):
        return {"backgroundColor": EDIT_BG, "textColor": EDIT_TEXT, "bold": False}
    return {"backgroundColor": READ_BG, "textColor": READ_TEXT, "bold": False}


def expand_field_groups(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for raw in tokens:
        token = (raw or "").strip()
        if not token:
            continue
        group = COLUMN_GROUPS.get(token.lower())
        if group:
            out.extend(group)
        else:
            out.append(token)
    seen: set[str] = set()
    ordered: list[str] = []
    for key in ["lead_id", *out]:
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered or ["lead_id"]


def build_column_metadata(field_keys: list[str]) -> list[dict[str, Any]]:
    cols: list[dict[str, Any]] = []
    for key in field_keys:
        defn = _field_def(key)
        editable = bool(defn.get("editable"))
        label = key if defn.get("type") == "key" else (
            f"🔒 {_humanize_key(key)}" if not editable else f"✏️ {_humanize_key(key)}"
        )
        meta: dict[str, Any] = {
            "key": key,
            "label": label,
            "type": defn.get("type", "string"),
            "editable": editable,
            "note": _default_note(key),
            "format": _default_format(key),
        }
        if defn.get("scope"):
            meta["scope"] = defn["scope"]
        cols.append(meta)
    return cols


def field_keys_for_sender(sender: str) -> str:
    return _sender_col(sender)


def build_sender_column_metadata(sender: str) -> dict[str, Any]:
    key = _sender_col(sender)
    return {
        "key": key,
        "label": f"🔒 LinkedIn ({sender})",
        "type": "enum",
        "editable": False,
        "note": "Read-only per-sender LinkedIn connection status from workspace data.",
        "format": _default_format("latest_sender"),
    }


def resolve_field_keys(
    detail: str,
    *,
    custom_fields: Optional[list[str]] = None,
    sender_profiles: Optional[list[str]] = None,
) -> list[str]:
    level = (detail or "standard").strip().lower()
    if level == "custom":
        fields = expand_field_groups([f.strip() for f in (custom_fields or []) if f and f.strip()])
        if len(fields) <= 1 and fields == ["lead_id"]:
            raise ValueError("custom detail requires --fields")
        return fields
    if level not in PRESET_KEYS:
        raise ValueError(f"unknown detail level: {detail}")
    keys = list(PRESET_KEYS[level])
    if level == "full":
        for sender in sender_profiles or []:
            keys.append(_sender_col(sender))
    return keys


def resolve_columns(
    detail: str,
    *,
    custom_fields: Optional[list[str]] = None,
    sender_profiles: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Return (header_label, field_key) pairs for the chosen detail level."""
    keys = resolve_field_keys(detail, custom_fields=custom_fields, sender_profiles=sender_profiles)
    meta = build_column_metadata([k for k in keys if not k.startswith("linkedin_") or k in FIELD_DEFS])
    meta_by_key = {m["key"]: m["label"] for m in meta}
    cols: list[tuple[str, str]] = []
    for key in keys:
        if key.startswith("linkedin_") and key not in FIELD_DEFS:
            sender = key[len("linkedin_"):].replace("_", " ")
            cols.append((f"🔒 LinkedIn ({sender})", key))
        else:
            cols.append((meta_by_key.get(key, _default_label(key)), key))
    return cols


def list_presets(template: str = "lead-review") -> dict[str, Any]:
    if template != "lead-review":
        raise ValueError(f"unknown template: {template}")
    all_fields = [
        {
            "key": key,
            "type": defn.get("type", "string"),
            "editable": bool(defn.get("editable")),
            "scope": defn.get("scope"),
            "in_presets": list(defn.get("presets", ())),
        }
        for key, defn in FIELD_DEFS.items()
    ]
    return {
        "template": template,
        "presets": list(PRESET_KEYS.keys()),
        "column_groups": COLUMN_GROUPS,
        "all_fields": all_fields,
    }


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
    original_source: Optional[str] = None,
    original_source_detail: Optional[str] = None,
    latest_source: Optional[str] = None,
    latest_source_detail: Optional[str] = None,
    industry: Optional[str] = None,
    headcount_min: Optional[int] = None,
    headcount_max: Optional[int] = None,
    location_city: Optional[str] = None,
    location_state: Optional[str] = None,
    email_domain: Optional[str] = None,
    email_verification_status: Optional[str] = None,
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
               {COMPANY_DOMAIN_SQL},
               wl.status AS workspace_stage,
               wl.current_status_label AS lead_status,
               wl.current_status_sentiment AS lead_sentiment,
               wl.contact_priority AS contact_order,
               l.channel AS channel,
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
        domain_clause, domain_params = require_professional_domain_clause()
        query += f" {domain_clause}"
        params.extend(domain_params)
    if original_source:
        query += " AND l.original_source = ?"
        params.append(original_source.strip())
    if original_source_detail:
        query += " AND (l.original_source_detail = ? OR l.latest_source_detail = ?)"
        params.extend([original_source_detail.strip(), original_source_detail.strip()])
    if latest_source:
        query += " AND l.latest_source = ?"
        params.append(latest_source.strip())
    if latest_source_detail:
        query += " AND l.latest_source_detail = ?"
        params.append(latest_source_detail.strip())
    if industry:
        query += " AND LOWER(l.industry) = LOWER(?)"
        params.append(industry.strip())
    if headcount_min is not None:
        query += " AND l.headcount_numeric >= ?"
        params.append(headcount_min)
    if headcount_max is not None:
        query += " AND l.headcount_numeric <= ?"
        params.append(headcount_max)
    if location_city:
        query += " AND LOWER(l.location_city) = LOWER(?)"
        params.append(location_city.strip())
    if location_state:
        query += " AND LOWER(l.location_state) = LOWER(?)"
        params.append(location_state.strip())
    if email_domain:
        query += " AND LOWER(l.email_domain) = LOWER(?)"
        params.append(email_domain.strip().lstrip("@"))
    if email_verification_status:
        query += " AND LOWER(l.email_verification_status) = LOWER(?)"
        params.append(email_verification_status.strip())
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
        "email_sent_count": lead.get("email_sent_count") if lead.get("email_sent_count") is not None else "",
        "linkedin_sent_count": lead.get("linkedin_sent_count") if lead.get("linkedin_sent_count") is not None else "",
        "total_replies_count": lead.get("total_replies_count") if lead.get("total_replies_count") is not None else "",
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
    original_source: Optional[str] = None,
    original_source_detail: Optional[str] = None,
    latest_source: Optional[str] = None,
    latest_source_detail: Optional[str] = None,
    industry: Optional[str] = None,
    headcount_min: Optional[int] = None,
    headcount_max: Optional[int] = None,
    location_city: Optional[str] = None,
    location_state: Optional[str] = None,
    email_domain: Optional[str] = None,
    email_verification_status: Optional[str] = None,
    enrich_fn: Optional[Callable[..., list[dict]]] = None,
) -> dict[str, Any]:
    if enrich_fn is None:
        raise ValueError("enrich_fn is required")
    ws_row = resolve_workspace_identity(conn, workspace)
    if not ws_row:
        raise ValueError(f"workspace not found: {workspace}")
    senders = list_workspace_senders(conn, ws_row["id"]) if detail == "full" else []
    columns = resolve_columns(detail, custom_fields=custom_fields, sender_profiles=senders)
    columns_meta: list[dict[str, Any]] = []
    for label, key in columns:
        if key.startswith("linkedin_") and key not in FIELD_DEFS:
            sender = key[len("linkedin_"):].replace("_", " ")
            meta = build_sender_column_metadata(sender)
            meta["label"] = label
            columns_meta.append(meta)
        else:
            meta = build_column_metadata([key])[0]
            meta["label"] = label
            columns_meta.append(meta)
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
        original_source=original_source,
        original_source_detail=original_source_detail,
        latest_source=latest_source,
        latest_source_detail=latest_source_detail,
        industry=industry,
        headcount_min=headcount_min,
        headcount_max=headcount_max,
        location_city=location_city,
        location_state=location_state,
        email_domain=email_domain,
        email_verification_status=email_verification_status,
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
        "columns": columns_meta,
        "field_keys": {label: key for label, key in columns},
        "freeze_header": True,
        "count": len(rows),
    }


def _normalize_header_key(header: str) -> str:
    """Strip emoji/markers, whitespace, lowercase — for sheet header matching."""
    text = str(header or "").strip()
    text = re.sub(r"^[^\w]+", "", text)
    return re.sub(r"\s+", "_", text.lower())


def _resolve_personalization_key(norm: str) -> Optional[str]:
    if norm.startswith("personalized_"):
        return norm if norm in FIELD_DEFS else None
    pers_key = f"personalized_{norm}"
    if pers_key in FIELD_DEFS:
        return pers_key
    return None


def _linkedin_status_map(
    conn: sqlite3.Connection,
    workspace_id: str,
    lead_id: int,
) -> dict[str, str]:
    rows = conn.execute(
        """SELECT sender_profile, is_connected, is_request_pending
           FROM workspace_lead_linkedin_status
           WHERE workspace_id = ? AND lead_id = ?""",
        (workspace_id, lead_id),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        sender = str(row["sender_profile"] or "").strip().lower()
        if not sender:
            continue
        if row["is_connected"]:
            out[sender] = "connected"
        elif row["is_request_pending"]:
            out[sender] = "pending"
        else:
            out[sender] = "none"
    return out


def _find_in_row(row: dict[str, Any], *keys: str) -> Any:
    """Resolve a cell by machine key or human/emoji header label."""
    by_norm = {_normalize_header_key(k): v for k, v in row.items()}
    for key in keys:
        norm = _normalize_header_key(key)
        if norm in by_norm:
            val = by_norm[norm]
            if val is not None and str(val).strip() != "":
                return val
    return None


def _parse_tags_cell(raw: Any) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[,;]", text)
    return [p.strip().lower().replace(" ", "_") for p in parts if p.strip()]


def _sheet_value_equal(field: str, current: Any, new: Any) -> bool:
    if field == "tags":
        cur = current if isinstance(current, list) else _parse_tags_cell(current)
        new_tags = new if isinstance(new, list) else _parse_tags_cell(new)
        return set(cur) == set(new_tags)
    if field == "contact_order":
        try:
            return int(current) == int(new)
        except (TypeError, ValueError):
            return str(current or "").strip() == str(new or "").strip()
    return str(current or "").strip().casefold() == str(new or "").strip().casefold()


def _current_row_state(
    conn: sqlite3.Connection,
    workspace_id: str,
    lead_id: int,
) -> dict[str, Any]:
    lead = conn.execute(
        """SELECT l.name, l.email, l.company, l.title, l.notes, l.linkedin_url,
                  l.company_id, COALESCE(co.name, l.company) AS company_display
           FROM leads l
           LEFT JOIN companies co ON l.company_id = co.id
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    wl = conn.execute(
        """SELECT status, current_status_label, current_status_sentiment, contact_priority
           FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
        (workspace_id, lead_id),
    ).fetchone()
    tags = [
        str(r["tag"])
        for r in conn.execute(
            "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
            (workspace_id, lead_id),
        ).fetchall()
    ]
    pers = {
        str(r["field_name"]): r["field_value"]
        for r in conn.execute(
            "SELECT field_name, field_value FROM lead_personalization WHERE lead_id = ?",
            (lead_id,),
        ).fetchall()
    }
    company_pers: dict[str, Any] = {}
    if lead and lead["company_id"]:
        company_pers = {
            str(r["field_name"]): r["field_value"]
            for r in conn.execute(
                "SELECT field_name, field_value FROM company_personalization WHERE company_id = ?",
                (lead["company_id"],),
            ).fetchall()
        }
    state: dict[str, Any] = {
        "name": (lead["name"] if lead else "") or "",
        "email": (lead["email"] if lead else "") or "",
        "company": (lead["company"] if lead else "") or "",
        "company_display": (lead["company_display"] if lead else "") or "",
        "company_id": lead["company_id"] if lead else None,
        "title": (lead["title"] if lead else "") or "",
        "linkedin": (lead["linkedin_url"] if lead else "") or "",
        "notes": (lead["notes"] if lead else "") or "",
        "workspace_stage": (wl["status"] if wl else "") or "",
        "lead_status": (wl["current_status_label"] if wl else "") or "",
        "lead_sentiment": (wl["current_status_sentiment"] if wl else "") or "",
        "contact_order": wl["contact_priority"] if wl and wl["contact_priority"] is not None else "",
        "tags": tags,
    }
    for key, val in pers.items():
        state[f"personalized_{key}"] = val or ""
    for key, val in company_pers.items():
        state[f"personalized_{key}"] = val or ""
    state["linkedin_status"] = _linkedin_status_map(conn, workspace_id, lead_id)
    return state


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
        lead_id_raw = _find_in_row(raw, "lead_id")
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
        current = _current_row_state(conn, workspace_id, lead_id)
        row_changes: dict[str, Any] = {"lead_id": lead_id}

        stage_val = _find_in_row(raw, "workspace_stage", "stage")
        if stage_val is not None and str(stage_val).strip():
            val = str(stage_val).strip()
            if not _sheet_value_equal("workspace_stage", current.get("workspace_stage"), val):
                row_changes["workspace_stage"] = val

        for field in ("lead_status", "lead_sentiment"):
            val = _find_in_row(raw, field)
            if val is not None and str(val).strip():
                val = str(val).strip()
                if not _sheet_value_equal(field, current.get(field), val):
                    row_changes[field] = val

        for field in ("name", "email", "company", "title", "linkedin"):
            val = _find_in_row(raw, field)
            if val is not None and str(val).strip():
                val = str(val).strip()
                compare_key = "company_display" if field == "company" and current.get("company_id") else field
                if not _sheet_value_equal(field, current.get(compare_key), val):
                    row_changes[field] = val
                    if field == "company" and current.get("company_id"):
                        row_changes["company_scope"] = True

        notes = _find_in_row(raw, "notes")
        if notes is not None and str(notes).strip():
            val = str(notes).strip()
            if not _sheet_value_equal("notes", current.get("notes"), val):
                row_changes["notes"] = val

        contact = _find_in_row(raw, "contact_order", "contact_priority")
        if contact is not None and str(contact).strip():
            try:
                val = int(contact)
                if not _sheet_value_equal("contact_order", current.get("contact_order"), val):
                    row_changes["contact_order"] = val
            except ValueError:
                pass

        tags_cell = _find_in_row(raw, "tags")
        if tags_cell is not None:
            parsed = _parse_tags_cell(tags_cell)
            if not _sheet_value_equal("tags", current.get("tags"), parsed):
                row_changes["tags"] = parsed

        for key, val in raw.items():
            norm = _normalize_header_key(key)
            pers_key = _resolve_personalization_key(norm)
            if not pers_key:
                continue
            if val is None or not str(val).strip():
                continue
            val = str(val).strip()
            if not _sheet_value_equal(pers_key, current.get(pers_key), val):
                row_changes[pers_key] = val

        current_li = current.get("linkedin_status") or {}
        linkedin_updates: list[tuple[str, str]] = []
        for key, val in raw.items():
            norm = _normalize_header_key(key)
            if not norm.startswith("linkedin_") and not norm.startswith("linkedin("):
                continue
            status = str(val or "").strip().lower()
            if status not in LINKEDIN_STATUS_VALUES:
                continue
            sender = key
            if norm.startswith("linkedin_"):
                sender = norm[len("linkedin_"):].replace("_", " ")
            elif "(" in key and ")" in key:
                sender = key[key.find("(") + 1 : key.rfind(")")].strip()
            sender_key = sender.strip().lower()
            db_status = current_li.get(sender_key, "not_requested")
            if status != db_status:
                linkedin_updates.append((sender, status))

        syncable = {
            k: v
            for k, v in row_changes.items()
            if k != "lead_id"
        }
        if not syncable and not linkedin_updates:
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

        lead_sets: list[str] = []
        lead_params: list[Any] = []
        for field, col in (
            ("name", "name"),
            ("email", "email"),
            ("title", "title"),
            ("linkedin", "linkedin_url"),
            ("notes", "notes"),
        ):
            if field in row_changes:
                lead_sets.append(f"{col} = ?")
                lead_params.append(row_changes[field])
        if "company" in row_changes and not row_changes.get("company_scope"):
            lead_sets.append("company = ?")
            lead_params.append(row_changes["company"])
        if lead_sets:
            lead_sets.extend(["updated_at = datetime('now')", "cloud_pending = 1"])
            lead_params.append(lead_id)
            conn.execute(
                f"UPDATE leads SET {', '.join(lead_sets)} WHERE id = ?",
                lead_params,
            )

        if row_changes.get("company") and row_changes.get("company_scope") and current.get("company_id"):
            conn.execute(
                "UPDATE companies SET name = ?, updated_at = datetime('now'), cloud_pending = 1 WHERE id = ?",
                (row_changes["company"], current["company_id"]),
            )
            conn.execute(
                "UPDATE leads SET company = ?, updated_at = datetime('now'), cloud_pending = 1 WHERE company_id = ?",
                (row_changes["company"], current["company_id"]),
            )
        elif row_changes.get("company"):
            conn.execute(
                "UPDATE leads SET company = ?, updated_at = datetime('now'), cloud_pending = 1 WHERE id = ?",
                (row_changes["company"], lead_id),
            )

        for field_key, field_val in row_changes.items():
            if not field_key.startswith("personalized_"):
                continue
            pers_name = field_key[len("personalized_"):]
            scope = _field_def(field_key).get("scope")
            if scope == "personalization_company" and current.get("company_id"):
                conn.execute(
                    """INSERT INTO company_personalization
                       (company_id, field_name, field_value, cloud_pending)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT (company_id, field_name) DO UPDATE SET
                         field_value = excluded.field_value,
                         processed_at = datetime('now'),
                         cloud_pending = 1""",
                    (current["company_id"], pers_name, field_val),
                )
            else:
                conn.execute(
                    """INSERT INTO lead_personalization
                       (lead_id, field_name, field_value, cloud_pending)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT (lead_id, field_name) DO UPDATE SET
                         field_value = excluded.field_value,
                         processed_at = datetime('now'),
                         cloud_pending = 1""",
                    (lead_id, pers_name, field_val),
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
