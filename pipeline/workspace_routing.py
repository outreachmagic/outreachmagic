"""
Org-wide leads + workspace-scoped status/events + campaign routing.

Campaign routing priority:
  platform + campaign_id exact > platform + campaign_name exact >
  rule_prefix / rule_regex > quarantine

Identity resolution (additive aliases):
  unified_id > email > linkedin_url > phone > provider_id
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_ORG_ID = "default"
DEFAULT_WORKSPACE_SLUG = "default"

WORKSPACE_ROUTING_SINGLE = "single"
WORKSPACE_ROUTING_MULTI = "multi"
VALID_WORKSPACE_ROUTING_MODES = (WORKSPACE_ROUTING_SINGLE, WORKSPACE_ROUTING_MULTI)

IDENTITY_PRECEDENCE = (
    "unified_id",
    "email",
    "linkedin_url",
    "phone",
    "provider_id",
)


@dataclass
class CampaignContext:
    source_platform: str
    campaign_id: Optional[str]
    campaign_name_raw: Optional[str]
    campaign_name_normalized: Optional[str]


@dataclass
class RoutingResult:
    workspace_id: str
    match_strategy: str
    map_id: Optional[str] = None


@dataclass
class OrgRoutingConfig:
    mode: str
    default_workspace_id: Optional[str] = None


def normalize_campaign_name(name: Optional[str]) -> Optional[str]:
    if not name or not str(name).strip():
        return None
    text = str(name).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text or None


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def normalize_linkedin(url: Optional[str]) -> Optional[str]:
    if not url or not str(url).strip():
        return None
    norm = str(url).strip().lower()
    for prefix in ("https://", "http://"):
        if norm.startswith(prefix):
            norm = norm[len(prefix) :]
    if norm.startswith("www."):
        norm = norm[4:]
    return norm.rstrip("/") or None


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) < 7:
        return None
    if not digits.startswith("+"):
        if len(digits) == 10:
            digits = "1" + digits
        return f"+{digits}"
    return f"+{digits}"


def normalize_identity_value(identity_type: str, value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    if identity_type == "email":
        return normalize_email(value)
    if identity_type == "linkedin_url":
        return normalize_linkedin(value)
    if identity_type == "phone":
        return normalize_phone(value)
    if identity_type == "unified_id":
        return value.strip().lower()
    if identity_type == "provider_id":
        return f"{value.strip()}"
    return value.strip().lower()


def extract_campaign_context(
    platform: str,
    event_fields: dict[str, str],
    raw: dict | None,
) -> CampaignContext:
    """Parse campaign id/name from extractor output and raw payload."""
    raw = raw or {}
    campaign_field = (event_fields.get("campaign") or "").strip()
    campaign_id = (event_fields.get("campaign_id") or "").strip() or None
    campaign_name = (event_fields.get("campaign_name") or "").strip() or None

    if campaign_field and not campaign_id and not campaign_name:
        if campaign_field.isdigit() or re.match(r"^[a-f0-9-]{8,}$", campaign_field, re.I):
            campaign_id = campaign_field
        else:
            campaign_name = campaign_field

    if not campaign_id:
        for path in (
            "campaign_id",
            "data.campaign_id",
            "campaign.id",
            "lead.campaign_id",
        ):
            val = _get_path(raw, path) if "." in path else raw.get(path)
            if val is not None and str(val).strip():
                campaign_id = str(val).strip()
                break

    if not campaign_name:
        for key in ("campaign_name", "campaign", "data.campaign_name"):
            val = _get_path(raw, key) if "." in key else raw.get(key)
            if val is not None and str(val).strip():
                text = str(val).strip()
                if text != (campaign_id or ""):
                    campaign_name = text
                    break

    return CampaignContext(
        source_platform=platform,
        campaign_id=campaign_id,
        campaign_name_raw=campaign_name or campaign_field or None,
        campaign_name_normalized=normalize_campaign_name(campaign_name or campaign_field),
    )


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def ensure_organization(conn: sqlite3.Connection, org_id: str = DEFAULT_ORG_ID) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO organizations (id, name, created_at)
           VALUES (?, 'Default Organization', datetime('now'))""",
        (org_id,),
    )


def ensure_default_org_workspace(conn: sqlite3.Connection) -> str:
    """Create default org + default workspace (single-workspace mode only)."""
    ensure_organization(conn)
    row = conn.execute(
        "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
        (DEFAULT_ORG_ID, DEFAULT_WORKSPACE_SLUG),
    ).fetchone()
    if row:
        ws_id = row["id"]
    else:
        ws_id = f"ws_{DEFAULT_WORKSPACE_SLUG}"
        conn.execute(
            """INSERT INTO workspaces (id, org_id, name, slug, created_at, updated_at)
               VALUES (?, ?, 'Default Workspace', ?, datetime('now'), datetime('now'))""",
            (ws_id, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_SLUG),
        )
    conn.execute(
        """UPDATE organizations SET default_workspace_id = ?
           WHERE id = ? AND (default_workspace_id IS NULL OR default_workspace_id = '')""",
        (ws_id, DEFAULT_ORG_ID),
    )
    return ws_id


def get_org_routing_config(conn: sqlite3.Connection, org_id: str) -> OrgRoutingConfig:
    ensure_organization(conn, org_id)
    row = conn.execute(
        """SELECT workspace_routing_mode, default_workspace_id
           FROM organizations WHERE id = ?""",
        (org_id,),
    ).fetchone()
    mode = WORKSPACE_ROUTING_SINGLE
    ws_id: Optional[str] = None
    if row:
        raw_mode = (row["workspace_routing_mode"] or "").strip().lower()
        if raw_mode in VALID_WORKSPACE_ROUTING_MODES:
            mode = raw_mode
        ws_id = (row["default_workspace_id"] or "").strip() or None
    if mode == WORKSPACE_ROUTING_MULTI:
        return OrgRoutingConfig(mode=mode, default_workspace_id=None)
    if not ws_id:
        ws_id = ensure_default_org_workspace(conn)
    return OrgRoutingConfig(mode=mode, default_workspace_id=ws_id)


def campaign_display_label(ctx: CampaignContext) -> str:
    if ctx.campaign_name_raw:
        return ctx.campaign_name_raw
    if ctx.campaign_id:
        return ctx.campaign_id
    return "unknown"


MULTI_WORKSPACE_HOLD_MESSAGE = (
    "Multi-workspace mode: events are held unprocessed until each campaign is "
    "mapped to a workspace. Create workspaces and campaign maps, then replay "
    "quarantined events."
)


def format_unmapped_campaign_message(ctx: CampaignContext) -> str:
    """User-facing instructions when multi-workspace routing cannot resolve a campaign."""
    label = campaign_display_label(ctx)
    platform = ctx.source_platform
    lines = [
        f"Campaign '{label}' ({platform}) is not mapped to a workspace.",
        "This event was not processed and is waiting in the quarantine queue.",
        "",
        "To fix this:",
        '1. Create a workspace:  pipeline.py workspace create --name "Your Team"',
        "2. Map this campaign to that workspace:",
        f"     pipeline.py campaign-map add --platform {platform} "
        f"--workspace WORKSPACE_SLUG --campaign-id ID",
        f"     pipeline.py campaign-map add --platform {platform} "
        f"--workspace WORKSPACE_SLUG --campaign-name \"{label}\"",
        "   Or use a prefix/regex rule:",
        f"     pipeline.py campaign-map add --platform {platform} "
        f"--workspace WORKSPACE_SLUG --match-strategy rule_prefix --campaign-name \"prefix\"",
        "3. Or assign one quarantined event manually:",
        "     pipeline.py quarantine list",
        "     pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG",
    ]
    if ctx.campaign_id and ctx.campaign_name_raw and ctx.campaign_id != ctx.campaign_name_raw:
        lines[5] = (
            f"     pipeline.py campaign-map add --platform {platform} "
            f"--workspace WORKSPACE_SLUG --campaign-id {ctx.campaign_id}"
        )
    return "\n".join(lines)


def resolve_workspace(
    conn: sqlite3.Connection,
    org_id: str,
    ctx: CampaignContext,
) -> Optional[RoutingResult]:
    """ID-first campaign routing with name and rule fallbacks."""
    platform = ctx.source_platform

    if ctx.campaign_id:
        row = conn.execute(
            """SELECT id, workspace_id, match_strategy FROM campaign_workspace_map
               WHERE org_id = ? AND source_platform = ? AND is_active = 1
                 AND match_strategy = 'id_exact' AND campaign_id = ?
               ORDER BY priority ASC LIMIT 1""",
            (org_id, platform, ctx.campaign_id),
        ).fetchone()
        if row:
            return RoutingResult(
                workspace_id=row["workspace_id"],
                match_strategy=row["match_strategy"],
                map_id=row["id"],
            )

    if ctx.campaign_name_normalized:
        row = conn.execute(
            """SELECT id, workspace_id, match_strategy FROM campaign_workspace_map
               WHERE org_id = ? AND source_platform = ? AND is_active = 1
                 AND match_strategy = 'name_exact'
                 AND campaign_name_normalized = ?
               ORDER BY priority ASC LIMIT 1""",
            (org_id, platform, ctx.campaign_name_normalized),
        ).fetchone()
        if row:
            return RoutingResult(
                workspace_id=row["workspace_id"],
                match_strategy=row["match_strategy"],
                map_id=row["id"],
            )

    name_for_rules = ctx.campaign_name_normalized or ""
    if name_for_rules:
        rules = conn.execute(
            """SELECT id, workspace_id, match_strategy, campaign_name_normalized
               FROM campaign_workspace_map
               WHERE org_id = ? AND source_platform = ? AND is_active = 1
                 AND match_strategy IN ('rule_prefix', 'rule_regex')
               ORDER BY priority ASC""",
            (org_id, platform),
        ).fetchall()
        for rule in rules:
            pattern = rule["campaign_name_normalized"] or ""
            if rule["match_strategy"] == "rule_prefix" and name_for_rules.startswith(pattern):
                return RoutingResult(
                    workspace_id=rule["workspace_id"],
                    match_strategy=rule["match_strategy"],
                    map_id=rule["id"],
                )
            if rule["match_strategy"] == "rule_regex":
                try:
                    if re.search(pattern, name_for_rules):
                        return RoutingResult(
                            workspace_id=rule["workspace_id"],
                            match_strategy=rule["match_strategy"],
                            map_id=rule["id"],
                        )
                except re.error:
                    continue

    return None


def resolve_workspace_for_ingest(
    conn: sqlite3.Connection,
    org_id: str,
    ctx: CampaignContext,
) -> Optional[RoutingResult]:
    """
    Resolve workspace using org routing mode:
      single — all events go to default_workspace_id
      multi  — campaign maps required; None if unmapped
    """
    config = get_org_routing_config(conn, org_id)
    if config.mode == WORKSPACE_ROUTING_SINGLE:
        if not config.default_workspace_id:
            return None
        return RoutingResult(
            workspace_id=config.default_workspace_id,
            match_strategy="single_workspace",
        )
    return resolve_workspace(conn, org_id, ctx)


def quarantine_event(
    conn: sqlite3.Connection,
    org_id: str,
    ctx: CampaignContext,
    *,
    reason: str,
    payload: dict,
    external_event_id: Optional[str] = None,
) -> str:
    qid = f"q_{datetime.now(timezone.utc).timestamp()}".replace(".", "")
    conn.execute(
        """INSERT INTO unmapped_campaign_queue (
               id, org_id, source_platform, campaign_id, campaign_name_raw,
               campaign_name_normalized, external_event_id, reason, status,
               payload_json, received_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))""",
        (
            qid,
            org_id,
            ctx.source_platform,
            ctx.campaign_id,
            ctx.campaign_name_raw,
            ctx.campaign_name_normalized,
            external_event_id,
            reason,
            json.dumps(payload),
        ),
    )
    return qid


def collect_identities_from_event(
    identity: dict[str, str],
    raw: dict | None,
    platform: str,
) -> list[tuple[str, str]]:
    """Return list of (identity_type, normalized_value) in precedence order."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(itype: str, val: Optional[str]):
        norm = normalize_identity_value(itype, val) if val else None
        if not norm:
            return
        key = (itype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    unified = (raw or {}).get("unified_lead_id") or (raw or {}).get("lead_id")
    if unified:
        add("unified_id", str(unified))
    add("email", identity.get("email"))
    add("linkedin_url", identity.get("linkedin_url"))
    add("phone", identity.get("phone"))
    provider_lead = (raw or {}).get("lead_id") or (raw or {}).get("sl_lead_email")
    if provider_lead and platform:
        add("provider_id", f"{platform}:{provider_lead}")

    # Sort by precedence for resolution attempts
    order = {t: i for i, t in enumerate(IDENTITY_PRECEDENCE)}
    out.sort(key=lambda x: order.get(x[0], 99))
    return out


def find_lead_by_identity(
    conn: sqlite3.Connection,
    org_id: str,
    identity_type: str,
    value_normalized: str,
) -> Optional[int]:
    row = conn.execute(
        """SELECT lead_id FROM lead_identities
           WHERE org_id = ? AND identity_type = ? AND identity_value_normalized = ?""",
        (org_id, identity_type, value_normalized),
    ).fetchone()
    if row:
        return int(row["lead_id"])
    if identity_type == "email":
        row = conn.execute(
            "SELECT id FROM leads WHERE email = ?", (value_normalized,)
        ).fetchone()
        return int(row["id"]) if row else None
    if identity_type == "linkedin_url":
        row = conn.execute(
            "SELECT id FROM leads WHERE linkedin_normalized = ?", (value_normalized,)
        ).fetchone()
        return int(row["id"]) if row else None
    return None


def upsert_identity_alias(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    identity_type: str,
    value_normalized: str,
    source: Optional[str] = None,
) -> None:
    existing = conn.execute(
        """SELECT lead_id FROM lead_identities
           WHERE org_id = ? AND identity_type = ? AND identity_value_normalized = ?""",
        (org_id, identity_type, value_normalized),
    ).fetchone()
    if existing and int(existing["lead_id"]) != lead_id:
        raise ValueError(
            f"identity conflict: {identity_type}={value_normalized} belongs to lead "
            f"{existing['lead_id']}, not {lead_id}"
        )
    conn.execute(
        """INSERT OR IGNORE INTO lead_identities (
               id, org_id, lead_id, identity_type, identity_value_normalized,
               source, is_verified, created_at
           ) VALUES (
               lower(hex(randomblob(16))), ?, ?, ?, ?, ?, 0, datetime('now')
           )""",
        (org_id, lead_id, identity_type, value_normalized, source),
    )


def resolve_org_lead_id(
    conn: sqlite3.Connection,
    org_id: str,
    identities: list[tuple[str, str]],
    *,
    create_lead_fn,
) -> tuple[int, bool]:
    """
    Resolve org lead by identity precedence. create_lead_fn() -> lead_id for new leads.
    Returns (lead_id, created).
    """
    for identity_type, value in identities:
        lead_id = find_lead_by_identity(conn, org_id, identity_type, value)
        if lead_id:
            for itype, val in identities:
                upsert_identity_alias(conn, org_id, lead_id, itype, val)
            return lead_id, False

    lead_id = create_lead_fn()
    for itype, val in identities:
        upsert_identity_alias(conn, org_id, lead_id, itype, val)
    return lead_id, True


def upsert_workspace_lead(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_id: str,
    lead_id: int,
    *,
    status: str = "prospecting",
    owner_user_id: Optional[str] = None,
) -> str:
    row = conn.execute(
        "SELECT id, status FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
        (workspace_id, lead_id),
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE workspace_leads SET last_activity_at = datetime('now'),
               updated_at = datetime('now') WHERE id = ?""",
            (row["id"],),
        )
        return row["id"]

    ws_lead_id = f"wl_{workspace_id}_{lead_id}"
    conn.execute(
        """INSERT INTO workspace_leads (
               id, org_id, workspace_id, lead_id, status, owner_user_id,
               stage_entered_at, last_activity_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'),
                     datetime('now'), datetime('now'))""",
        (ws_lead_id, org_id, workspace_id, lead_id, status, owner_user_id),
    )
    return ws_lead_id


def append_workspace_event(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_id: str,
    lead_id: int,
    workspace_lead_id: str,
    *,
    event_type: str,
    event_at: str,
    source_platform: str,
    idempotency_key: str,
    payload: dict,
    external_event_id: Optional[str] = None,
) -> Optional[str]:
    existing = conn.execute(
        "SELECT id FROM workspace_lead_events WHERE org_id = ? AND idempotency_key = ?",
        (org_id, idempotency_key),
    ).fetchone()
    if existing:
        return None

    event_id = f"wse_{idempotency_key[:32]}"
    conn.execute(
        """INSERT INTO workspace_lead_events (
               id, org_id, workspace_id, lead_id, workspace_lead_id,
               event_type, event_at, source_platform, external_event_id,
               idempotency_key, payload_json, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            event_id,
            org_id,
            workspace_id,
            lead_id,
            workspace_lead_id,
            event_type,
            event_at,
            source_platform,
            external_event_id,
            idempotency_key,
            json.dumps(payload),
        ),
    )
    return event_id


def assign_campaign_map(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    source_platform: str,
    workspace_id: str,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: str = "id_exact",
    priority: int = 100,
) -> str:
    map_id = f"map_{source_platform}_{campaign_id or campaign_name or 'rule'}"
    conn.execute(
        """INSERT OR REPLACE INTO campaign_workspace_map (
               id, org_id, source_platform, campaign_id, campaign_name_normalized,
               workspace_id, match_strategy, priority, is_active, created_at, updated_at
           ) VALUES (
               ?, ?, ?, ?, ?, ?, ?, ?, 1,
               COALESCE((SELECT created_at FROM campaign_workspace_map WHERE id = ?),
                        datetime('now')),
               datetime('now')
           )""",
        (
            map_id,
            org_id,
            source_platform,
            campaign_id,
            normalize_campaign_name(campaign_name),
            workspace_id,
            match_strategy,
            priority,
            map_id,
        ),
    )
    return map_id


def replay_quarantine_item(conn: sqlite3.Connection, queue_id: str, workspace_id: str) -> dict:
    """Assign workspace to quarantined payload and mark for reprocessing."""
    row = conn.execute(
        "SELECT * FROM unmapped_campaign_queue WHERE id = ? AND status = 'pending'",
        (queue_id,),
    ).fetchone()
    if not row:
        return {"status": "error", "error": "queue item not found or not pending"}

    org_id = row["org_id"]
    assign_campaign_map(
        conn,
        org_id,
        source_platform=row["source_platform"],
        workspace_id=workspace_id,
        campaign_id=row["campaign_id"],
        campaign_name=row["campaign_name_raw"],
        match_strategy="id_exact" if row["campaign_id"] else "name_exact",
    )
    conn.execute(
        """UPDATE unmapped_campaign_queue
           SET status = 'assigned', resolved_at = datetime('now') WHERE id = ?""",
        (queue_id,),
    )
    return {"status": "assigned", "queue_id": queue_id, "workspace_id": workspace_id}
