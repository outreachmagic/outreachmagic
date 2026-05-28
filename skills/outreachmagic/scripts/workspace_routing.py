"""
Org-wide leads + workspace-scoped status/events + campaign routing.

Campaign routing priority:
  campaign_id exact > campaign_name exact >
  rule_contains / rule_prefix / rule_regex > quarantine
Rules with source_platform='*' match any incoming platform.

Identity resolution (additive aliases):
  external_id > email > linkedin_url > linkedin_sales_nav_id >
  linkedin_member_id > phone > name_company_domain > name_company >
  import_key > provider_id
"""

from __future__ import annotations

import hashlib
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
    "external_id",
    "email",
    "linkedin_url",
    "linkedin_sales_nav_id",
    "linkedin_member_id",
    "phone",
    "name_company_domain",
    "name_company_domain_title",
    "name_company",
    "import_key",
    "provider_id",
)

ENTITY_KEY_IDENTITY_TYPES = (
    "external_id",
    "name_company_domain",
    "name_company_domain_title",
    "name_company",
    "import_key",
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
    """Public profile slug only: linkedin.com/in/handle (no scheme/www)."""
    raw = (url or "").strip()
    if not raw:
        return None
    norm = raw.lower()
    for prefix in ("https://", "http://"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if norm.startswith("www."):
        norm = norm[4:]
    match = re.match(r"(linkedin\.com/in/[^/?#]+)", norm)
    if match:
        return match.group(1)
    return norm.rstrip("/") or None


def normalize_linkedin_sales_nav_id(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    m = re.match(r"^ACwAA[\w-]+$", raw)
    if m:
        return m.group(0)
    m = re.search(r"urn:li:fs_salesProfile:\((ACwAA[^,]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def normalize_linkedin_member_id(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    m = re.search(r"urn:li:member:(\d+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.match(r"^\d{5,12}$", raw):
        return raw
    return None


def parse_linkedin_value(raw: str) -> list[tuple[str, str]]:
    """Classify one string into 0..n (identity_type, normalized_value) pairs."""
    text = (raw or "").strip()
    if not text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    lower = text.lower()

    def add(itype: str, norm: Optional[str]):
        if not norm:
            return
        key = (itype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    if "urn:li:member:" in lower:
        add("linkedin_member_id", normalize_linkedin_member_id(text))
    if "fs_salesprofile" in lower or text.startswith("ACwAA"):
        add("linkedin_sales_nav_id", normalize_linkedin_sales_nav_id(text))
    public = normalize_linkedin(text)
    if public and "linkedin.com/in/" in public:
        add("linkedin_url", public)

    order = {t: i for i, t in enumerate(IDENTITY_PRECEDENCE)}
    out.sort(key=lambda x: order.get(x[0], 99))
    return out


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


def slugify_identity_source(raw: Optional[str]) -> str:
    """Stable slug for namespacing external_id values (list_source, import_name, etc.)."""
    text = (raw or "").strip().lower()
    if not text:
        return "csv"
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:64] or "csv"


def normalize_person_name(name: Optional[str]) -> Optional[str]:
    if not name or not str(name).strip():
        return None
    text = re.sub(r"[^\w\s\-']", "", str(name).strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_company_name_key(company: Optional[str]) -> Optional[str]:
    if not company or not str(company).strip():
        return None
    text = str(company).strip().lower()
    text = re.sub(r"\s+", " ", text)
    for suffix in (
        r",?\s+inc\.?$",
        r",?\s+incorporated$",
        r",?\s+llc\.?$",
        r",?\s+l\.?l\.?c\.?$",
        r",?\s+corp\.?$",
        r",?\s+corporation$",
    ):
        text = re.sub(suffix, "", text, flags=re.IGNORECASE)
    return text.strip() or None


def pick_external_id_from_raw(raw: Optional[dict]) -> Optional[str]:
    """First non-empty CRM/list id from a payload row (column aliases only; stored as external_id)."""
    if not raw:
        return None
    for key in ("external_id", "unified_lead_id", "source_id"):
        val = raw.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def normalize_external_id(value: str, source_slug: str) -> Optional[str]:
    raw = (value or "").strip().lower()
    if not raw or len(raw) > 128:
        return None
    if ":" in raw:
        return raw
    slug = slugify_identity_source(source_slug)
    return f"{slug}:{raw}"


def build_import_key_fingerprint(
    *,
    name: str,
    company: Optional[str] = None,
    company_domain: Optional[str] = None,
    import_batch: Optional[str] = None,
) -> str:
    parts = [
        normalize_person_name(name) or "",
        normalize_company_name_key(company) or "",
        (company_domain or "").strip().lower(),
        slugify_identity_source(import_batch),
    ]
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]
    return f"om:{digest}"


def match_confidence_for_type(identity_type: str) -> str:
    if identity_type in (
        "external_id", "email", "linkedin_url",
        "linkedin_sales_nav_id", "linkedin_member_id",
    ):
        return "high"
    if identity_type in ("phone", "name_company_domain", "name_company_domain_title"):
        return "medium"
    return "low"


def build_import_identities(
    profile: dict[str, str],
    extra: dict[str, str],
    *,
    import_batch: Optional[str] = None,
    company_domain: Optional[str] = None,
) -> list[tuple[str, str]]:
    """Build (identity_type, normalized_value) list for import / resolve_lead."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(itype: str, val: Optional[str]):
        if not val:
            return
        norm = normalize_identity_value(itype, val)
        if not norm:
            return
        key = (itype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    source_slug = (
        extra.get("list_source")
        or extra.get("import_name")
        or import_batch
        or "csv"
    )
    ext_raw = extra.get("external_id")
    if ext_raw:
        namespaced = normalize_external_id(str(ext_raw), source_slug)
        if namespaced:
            add("external_id", namespaced)

    add("email", profile.get("email"))
    li = profile.get("linkedin")
    if li:
        for itype, val in parse_linkedin_value(li):
            add(itype, val)
    add("phone", profile.get("phone") or extra.get("phone"))

    norm_name = normalize_person_name(profile.get("name"))
    domain = (company_domain or extra.get("company_domain") or "").strip().lower()
    if domain:
        domain = re.sub(r"^www\.", "", domain.split("/")[0].split("?")[0])
    company = profile.get("company")
    title = (profile.get("title") or "").strip().lower()

    if norm_name and domain:
        if title:
            add("name_company_domain_title", f"{norm_name}|{domain}|{title}")
        else:
            add("name_company_domain", f"{norm_name}|{domain}")
    elif norm_name and company:
        ckey = normalize_company_name_key(company)
        if ckey:
            add("name_company", f"{norm_name}|{ckey}")
    elif norm_name:
        batch = import_batch or extra.get("import_name") or extra.get("list_source")
        add("import_key", build_import_key_fingerprint(
            name=profile.get("name") or "",
            company=company,
            company_domain=domain or None,
            import_batch=batch,
        ))

    order = {t: i for i, t in enumerate(IDENTITY_PRECEDENCE)}
    out.sort(key=lambda x: order.get(x[0], 99))
    return out


def find_match_method_for_lead(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    identities: list[tuple[str, str]],
) -> Optional[str]:
    """Which identity type linked to this lead_id (first in precedence order)."""
    for itype, val in identities:
        found = find_lead_by_identity(conn, org_id, itype, val)
        if found == lead_id:
            return itype
    return None


def upsert_all_identities(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    identities: list[tuple[str, str]],
    *,
    source: Optional[str] = None,
) -> list[dict]:
    """Register all identities; return conflict records (does not raise)."""
    conflicts: list[dict] = []
    for itype, val in identities:
        try:
            upsert_identity_alias(conn, org_id, lead_id, itype, val, source=source)
        except ValueError:
            existing = conn.execute(
                """SELECT lead_id FROM lead_identities
                   WHERE org_id = ? AND identity_type = ? AND identity_value_normalized = ?""",
                (org_id, itype, val),
            ).fetchone()
            conflicts.append({
                "identity_type": itype,
                "value": val,
                "existing_lead_id": int(existing["lead_id"]) if existing else None,
            })
    return conflicts


def normalize_identity_value(identity_type: str, value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    if identity_type == "email":
        return normalize_email(value)
    if identity_type == "linkedin_url":
        return normalize_linkedin(value)
    if identity_type == "linkedin_sales_nav_id":
        return normalize_linkedin_sales_nav_id(value)
    if identity_type == "linkedin_member_id":
        return normalize_linkedin_member_id(value)
    if identity_type == "phone":
        return normalize_phone(value)
    if identity_type == "external_id":
        raw = value.strip().lower()
        return raw[:128] if raw else None
    if identity_type == "provider_id":
        return f"{value.strip()}"
    if identity_type in (
        "name_company_domain",
        "name_company_domain_title",
        "name_company",
        "import_key",
    ):
        return value.strip().lower()
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
        f"     pipeline.py campaign-map add --workspace WORKSPACE_SLUG --campaign-id ID",
        f'     pipeline.py campaign-map add --workspace WORKSPACE_SLUG --campaign-name "{label}"',
        "   Or use a contains/prefix/regex rule:",
        f'     pipeline.py campaign-map add --workspace WORKSPACE_SLUG --match-strategy rule_contains --campaign-name "substring"',
        "3. Or assign one quarantined event manually:",
        "     pipeline.py quarantine list",
        "     pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG",
    ]
    if ctx.campaign_id and ctx.campaign_name_raw and ctx.campaign_id != ctx.campaign_name_raw:
        lines[6] = (
            f"     pipeline.py campaign-map add --workspace WORKSPACE_SLUG --campaign-id {ctx.campaign_id}"
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
               WHERE org_id = ? AND source_platform IN (?, '*') AND is_active = 1
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
               WHERE org_id = ? AND source_platform IN (?, '*') AND is_active = 1
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
               WHERE org_id = ? AND source_platform IN (?, '*') AND is_active = 1
                 AND match_strategy IN ('rule_contains', 'rule_prefix', 'rule_regex')
               ORDER BY priority ASC""",
            (org_id, platform),
        ).fetchall()
        for rule in rules:
            pattern = rule["campaign_name_normalized"] or ""
            if not pattern:
                continue
            if rule["match_strategy"] == "rule_contains" and pattern in name_for_rules:
                return RoutingResult(
                    workspace_id=rule["workspace_id"],
                    match_strategy=rule["match_strategy"],
                    map_id=rule["id"],
                )
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

    ext = pick_external_id_from_raw(raw)
    if ext:
        add("external_id", ext)
    add("email", identity.get("email"))
    li = identity.get("linkedin_url")
    if li:
        for itype, val in parse_linkedin_value(li):
            add(itype, val)
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
            "SELECT id FROM leads WHERE linkedin_url = ?", (value_normalized,)
        ).fetchone()
        return int(row["id"]) if row else None
    return None


def lead_entity_key(conn: sqlite3.Connection, org_id: str, lead_id: int) -> str:
    """Stable relay push/replay key: email > linkedin > prefixed identity alias."""
    row = conn.execute(
        "SELECT email, linkedin_url FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    if row and row["email"]:
        return str(row["email"]).strip().lower()
    if row and row["linkedin_url"]:
        return str(row["linkedin_url"]).strip()
    placeholders = ",".join("?" for _ in ENTITY_KEY_IDENTITY_TYPES)
    id_row = conn.execute(
        f"""SELECT identity_type, identity_value_normalized FROM lead_identities
            WHERE org_id = ? AND lead_id = ?
            AND identity_type IN ({placeholders})
            ORDER BY identity_type LIMIT 1""",
        (org_id, lead_id, *ENTITY_KEY_IDENTITY_TYPES),
    ).fetchone()
    if id_row:
        return f"{id_row['identity_type']}:{id_row['identity_value_normalized']}"
    return ""


def parse_entity_key(entity_key: str) -> tuple[Optional[str], Optional[str]]:
    """Parse 'type:value' entity keys for agent replay."""
    if not entity_key or "@" in entity_key:
        return None, None
    if entity_key.startswith("http") or "linkedin.com" in entity_key.lower():
        return None, None
    if ":" not in entity_key:
        return None, None
    itype, _, val = entity_key.partition(":")
    val = val.strip()
    if not val:
        return None, None
    return itype, val


def import_extra_from_entity_key(entity_key: str) -> dict[str, str]:
    """Map a prefixed entity_key into import extra fields (external_id only)."""
    itype, val = parse_entity_key(entity_key)
    if itype == "external_id" and val:
        return {"external_id": val}
    return {}


def lead_external_id_value(
    conn: sqlite3.Connection, org_id: str, lead_id: int,
) -> Optional[str]:
    row = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ? AND identity_type = 'external_id' LIMIT 1""",
        (org_id, lead_id),
    ).fetchone()
    return row["identity_value_normalized"] if row else None


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


def enqueue_identity_conflict_merge(
    conn: sqlite3.Connection,
    org_id: str,
    new_lead_id: int,
    identity_type: str,
    value_normalized: str,
    *,
    source: Optional[str] = None,
) -> None:
    """Queue merge of new_lead_id into the lead that already owns this identity."""
    owner_id = find_lead_by_identity(conn, org_id, identity_type, value_normalized)
    if not owner_id or owner_id == new_lead_id:
        return
    keep_id = owner_id
    merge_id = new_lead_id
    job_id = "merge_" + hashlib.sha256(
        f"{org_id}:{keep_id}:{merge_id}:{identity_type}:{value_normalized}".encode()
    ).hexdigest()[:24]
    conn.execute(
        """INSERT OR IGNORE INTO lead_merge_jobs (
               id, org_id, keep_lead_id, merge_lead_id, status, reason, audit_json
           ) VALUES (?, ?, ?, ?, 'pending', 'identity_conflict', ?)""",
        (
            job_id,
            org_id,
            keep_id,
            merge_id,
            json.dumps(
                {
                    "identity_type": identity_type,
                    "value": value_normalized,
                    "source": source,
                }
            ),
        ),
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
    current_status_label: Optional[str] = None,
    current_status_sentiment: Optional[str] = None,
    contact_priority: Optional[int] = None,
) -> str:
    row = conn.execute(
        "SELECT id, status FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
        (workspace_id, lead_id),
    ).fetchone()
    if row:
        extra_sets = []
        extra_params = []
        if current_status_label is not None:
            extra_sets.append("current_status_label = ?")
            extra_params.append(current_status_label)
        if current_status_sentiment is not None:
            extra_sets.append("current_status_sentiment = ?")
            extra_params.append(current_status_sentiment)
        if contact_priority is not None:
            extra_sets.append("contact_priority = ?")
            extra_params.append(contact_priority)
        sets = "last_activity_at = datetime('now'), updated_at = datetime('now')"
        if extra_sets:
            sets += ", " + ", ".join(extra_sets)
        conn.execute(
            f"UPDATE workspace_leads SET {sets} WHERE id = ?",
            (*extra_params, row["id"]),
        )
        return row["id"]

    ws_lead_id = f"wl_{workspace_id}_{lead_id}"
    conn.execute(
        """INSERT INTO workspace_leads (
               id, org_id, workspace_id, lead_id, status, owner_user_id,
               current_status_label, current_status_sentiment, contact_priority,
               stage_entered_at, last_activity_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                     datetime('now'), datetime('now'), datetime('now'), datetime('now'))""",
        (ws_lead_id, org_id, workspace_id, lead_id, status, owner_user_id,
         current_status_label, current_status_sentiment, contact_priority),
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


def upsert_linkedin_status(
    conn: sqlite3.Connection,
    workspace_id: str,
    lead_id: int,
    sender_profile_normalized: str,
    event_type: str,
    event_at: str,
) -> None:
    """Update LinkedIn connection status with timestamp guards to prevent stale writes."""
    row = conn.execute(
        """SELECT id, is_connected, connected_at, request_sent_at
           FROM workspace_lead_linkedin_status
           WHERE workspace_id = ? AND lead_id = ? AND sender_profile = ?""",
        (workspace_id, lead_id, sender_profile_normalized),
    ).fetchone()

    if event_type == "linkedin_connect":
        if row and row["is_connected"] and row["connected_at"] and row["connected_at"] <= event_at:
            return
        if not row:
            row_id = f"lis_{workspace_id}_{lead_id}_{sender_profile_normalized[:20]}"
            conn.execute(
                """INSERT INTO workspace_lead_linkedin_status
                   (id, workspace_id, lead_id, sender_profile, is_request_pending, request_sent_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (row_id, workspace_id, lead_id, sender_profile_normalized, event_at),
            )
        else:
            conn.execute(
                """UPDATE workspace_lead_linkedin_status
                   SET is_request_pending = 1, request_sent_at = ?, updated_at = datetime('now')
                   WHERE id = ? AND is_connected = 0""",
                (event_at, row["id"]),
            )

    elif event_type == "linkedin_connection_accepted":
        if not row:
            row_id = f"lis_{workspace_id}_{lead_id}_{sender_profile_normalized[:20]}"
            conn.execute(
                """INSERT INTO workspace_lead_linkedin_status
                   (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending,
                    connected_at)
                   VALUES (?, ?, ?, ?, 1, 0, ?)""",
                (row_id, workspace_id, lead_id, sender_profile_normalized, event_at),
            )
        else:
            conn.execute(
                """UPDATE workspace_lead_linkedin_status
                   SET is_connected = 1, is_request_pending = 0, connected_at = ?,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (event_at, row["id"]),
            )


def assign_campaign_map(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    source_platform: str = "*",
    workspace_id: str,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: str = "id_exact",
    priority: int = 100,
) -> str:
    if not campaign_id and not campaign_name:
        raise ValueError("At least one of campaign_id or campaign_name is required for a mapping rule")
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
    if row["campaign_id"] or row["campaign_name_raw"]:
        assign_campaign_map(
            conn,
            org_id,
            source_platform="*",
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
