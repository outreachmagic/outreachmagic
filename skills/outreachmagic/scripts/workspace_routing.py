"""
Org-wide leads + workspace-scoped status/events + campaign routing.

Campaign routing priority:
  campaign_platform_id exact > campaign_name exact >
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


def resolve_workspace_identity(
    conn: sqlite3.Connection,
    workspace: Optional[str],
    *,
    org_id: str = DEFAULT_ORG_ID,
) -> Optional[dict]:
    """Resolve workspace slug or display name to {id, name, slug}."""
    token = (workspace or "").strip()
    if not token:
        return None
    row = conn.execute(
        """SELECT id, name, slug
           FROM workspaces
           WHERE org_id = ?
             AND (lower(slug) = lower(?) OR lower(name) = lower(?))
           ORDER BY CASE WHEN lower(slug) = lower(?) THEN 0 ELSE 1 END
           LIMIT 1""",
        (org_id, token, token, token),
    ).fetchone()
    return dict(row) if row else None

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

# Fuzzy composite types built for the "is there *some* identity" check and
# entity-key fallback -- never used for actual lead matching (see
# STRONG_IDENTITY_TYPES in pipeline.py's resolve_lead), and >96% redundant
# with a strong identity already on the same lead. Not persisted to
# lead_identities; lead_entity_key() recomputes the same fingerprint on the
# fly from the leads row instead of reading a stored row.
NON_PERSISTED_IDENTITY_TYPES = frozenset({
    "name_company_domain", "name_company_domain_title", "name_company", "import_key",
})

# Identity types safe enough to trigger an AUTOMATIC merge queue on conflict.
# Narrower than STRONG_IDENTITY_TYPES (pipeline.py) on purpose: external_id's
# safety depends on the source provider's own guarantees (not verifiable
# here), phone numbers are frequently shared company lines, and PlusVibe's
# provider_id is a conversation/thread id shared by both parties on a
# forwarded reply thread -- none of those should trigger an automatic merge,
# only email and LinkedIn's own unique identifiers are solid enough for that.
AUTO_MERGE_SAFE_IDENTITY_TYPES = frozenset({
    "email", "linkedin_url", "linkedin_sales_nav_id", "linkedin_member_id",
})


@dataclass
class CampaignContext:
    source_platform: str
    campaign_platform_id: Optional[str]
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


_CALENDLY_EVENT_TYPE_ID_RE = re.compile(r"event_types/([a-f0-9-]+)", re.I)


def parse_calendly_event_type_id(value: Any) -> Optional[str]:
    """UUID from Calendly event_types URI or bare id string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.match(r"^[a-f0-9-]{36}$", text, re.I):
        return text
    match = _CALENDLY_EVENT_TYPE_ID_RE.search(text)
    return match.group(1) if match else None


def enrich_calendly_campaign_fields(
    raw: dict,
    campaign_platform_id: Optional[str],
    campaign_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Calendly: event type UUID + display name for workspace routing."""
    payload = raw.get("payload") or {}
    scheduled = payload.get("scheduled_event") or {}
    if not campaign_platform_id:
        campaign_platform_id = parse_calendly_event_type_id(scheduled.get("event_type"))
        if not campaign_platform_id:
            campaign_platform_id = parse_calendly_event_type_id(raw.get("campaign_id"))
    if not campaign_name:
        scheduled_name = str(scheduled.get("name") or "").strip()
        if scheduled_name:
            campaign_name = scheduled_name
    return campaign_platform_id, campaign_name


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def is_sales_nav_hash_slug(slug: str) -> bool:
    """True when a linkedin.com/in/ slug is a Sales Navigator member token, not a public handle."""
    return bool(re.match(r"^ac(?:w|o)aa[\w-]{20,}$", (slug or "").strip(), re.IGNORECASE))


def linkedin_in_slug(url_or_slug: str) -> Optional[str]:
    """Extract linkedin.com/in/<slug> segment from a URL or path."""
    raw = (url_or_slug or "").strip().lower()
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    if raw.startswith("www."):
        raw = raw[4:]
    m = re.search(r"linkedin\.com/in/([^/?#]+)", raw, re.IGNORECASE)
    return m.group(1) if m else None


def normalize_linkedin_sales_nav_id(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    # ACwAA... is the fs_salesProfile URN encoding; ACoAA... is the token
    # tools like Prosp surface for the same Sales Navigator lookup (confirmed
    # working against Sales Navigator directly) -- both are valid here.
    m = re.match(r"^AC(?:w|o)AA[\w-]+$", raw, re.IGNORECASE)
    if m:
        return m.group(0)
    m = re.search(r"urn:li:fs_salesProfile:\((AC(?:w|o)AA[^,]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def extract_sales_nav_id_from_linkedin_url(value: str) -> Optional[str]:
    """Pull Sales Navigator member token from a linkedin.com/in/<slug>,
    linkedin.com/sales/lead/<token>,... or linkedin.com/sales/people/<token>,... URL."""
    raw = (value or "").strip()
    if not raw:
        return None
    m = re.search(r"linkedin\.com/(?:in|sales/(?:lead|people))/([^/?#,]+)", raw, re.IGNORECASE)
    if not m:
        return None
    return normalize_linkedin_sales_nav_id(m.group(1))


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


def normalize_linkedin(url: Optional[str]) -> Optional[str]:
    """Public profile slug only: linkedin.com/in/handle (no scheme/www)."""
    raw = (url or "").strip()
    if not raw:
        return None
    if normalize_linkedin_sales_nav_id(raw):
        return None
    norm = raw.lower()
    for prefix in ("https://", "http://"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if norm.startswith("www."):
        norm = norm[4:]
    match = re.match(r"(linkedin\.com/in/([^/?#]+))", norm)
    if match:
        slug = match.group(2)
        if is_sales_nav_hash_slug(slug):
            return None
        return match.group(1)
    if re.match(r"^[a-z0-9][a-z0-9\-_%]*$", norm):
        if is_sales_nav_hash_slug(norm):
            return None
        return f"linkedin.com/in/{norm}"
    return norm.rstrip("/") or None


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
    sales_nav = None
    if "fs_salesprofile" in lower or re.match(r"^AC(?:w|o)AA", text, re.IGNORECASE):
        sales_nav = normalize_linkedin_sales_nav_id(text)
        add("linkedin_sales_nav_id", sales_nav)
    if "linkedin.com/in/" in lower or "linkedin.com/sales/lead/" in lower or "linkedin.com/sales/people/" in lower:
        sales_nav = sales_nav or extract_sales_nav_id_from_linkedin_url(text)
        add("linkedin_sales_nav_id", sales_nav)
    public = normalize_linkedin(text)
    if public and "linkedin.com/in/" in public:
        slug = linkedin_in_slug(public) or ""
        if not is_sales_nav_hash_slug(slug):
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
    for extra_key in (
        "member linkedin sales nav id",
        "linkedin_sales_nav_id",
        "sales_nav_id",
    ):
        sn_val = extra.get(extra_key)
        if sn_val:
            for itype, val in parse_linkedin_value(str(sn_val)):
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


def linkedin_url_is_hash(url: Optional[str]) -> bool:
    slug = linkedin_in_slug(url or "")
    return bool(slug and is_sales_nav_hash_slug(slug))


def should_replace_linkedin_url(current: Optional[str], new_public: Optional[str]) -> bool:
    """Replace when current is empty or a Sales Nav hash slug and new is a valid public URL."""
    if not new_public or linkedin_url_is_hash(new_public):
        return False
    if not current or not str(current).strip():
        return True
    return linkedin_url_is_hash(current)


def linkedin_url_field_conflict(
    conn: sqlite3.Connection,
    lead_id: int,
    url: str,
) -> Optional[dict]:
    """Return conflict metadata when another lead already owns this linkedin_url."""
    if not url or linkedin_url_is_hash(url):
        return None
    row = conn.execute(
        "SELECT id FROM leads WHERE linkedin_url = ? AND id != ?",
        (url, lead_id),
    ).fetchone()
    if not row:
        return None
    owner_id = int(row["id"])
    return {
        "type": "linkedin_url_conflict",
        "linkedin_url": url,
        "existing_lead_id": owner_id,
        "message": (
            f"linkedin_url {url} is already set on lead {owner_id}; "
            "field left unchanged — consider dedup merge"
        ),
    }


def promote_linkedin_url_from_identities(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
) -> Optional[dict]:
    """Promote best public linkedin_url identity to leads.linkedin_url.

    Returns conflict metadata when the URL is already owned by another lead.
    """
    row = conn.execute("SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,)).fetchone()
    current = (row["linkedin_url"] if row else None) or ""
    if current and not linkedin_url_is_hash(current):
        return None
    candidates = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ? AND identity_type = 'linkedin_url'
           ORDER BY created_at DESC""",
        (org_id, lead_id),
    ).fetchall()
    for c in candidates:
        url = c["identity_value_normalized"]
        if linkedin_url_is_hash(url):
            continue
        conflict = linkedin_url_field_conflict(conn, lead_id, url)
        if conflict:
            return conflict
        conn.execute(
            "UPDATE leads SET linkedin_url = ?, updated_at = datetime('now') WHERE id = ?",
            (url, lead_id),
        )
        return None
    return None


def linkedin_sales_nav_id_field_conflict(
    conn: sqlite3.Connection,
    lead_id: int,
    sales_nav_id: str,
) -> Optional[dict]:
    """Return conflict metadata when another lead already owns this sales-nav id."""
    if not sales_nav_id:
        return None
    row = conn.execute(
        "SELECT id FROM leads WHERE linkedin_sales_nav_id = ? AND id != ?",
        (sales_nav_id, lead_id),
    ).fetchone()
    if not row:
        return None
    owner_id = int(row["id"])
    return {
        "type": "linkedin_sales_nav_id_conflict",
        "linkedin_sales_nav_id": sales_nav_id,
        "existing_lead_id": owner_id,
        "message": (
            f"linkedin_sales_nav_id {sales_nav_id} is already set on lead {owner_id}; "
            "field left unchanged — consider dedup merge"
        ),
    }


def promote_linkedin_sales_nav_id_from_identities(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
) -> Optional[dict]:
    """Promote the sales-nav-id identity to leads.linkedin_sales_nav_id.

    Returns conflict metadata when the id is already owned by another lead.
    """
    row = conn.execute(
        "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    if row and (row["linkedin_sales_nav_id"] or "").strip():
        return None
    candidate = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ? AND identity_type = 'linkedin_sales_nav_id'
           ORDER BY created_at DESC LIMIT 1""",
        (org_id, lead_id),
    ).fetchone()
    if not candidate:
        return None
    sales_nav_id = candidate["identity_value_normalized"]
    conflict = linkedin_sales_nav_id_field_conflict(conn, lead_id, sales_nav_id)
    if conflict:
        return conflict
    conn.execute(
        "UPDATE leads SET linkedin_sales_nav_id = ?, updated_at = datetime('now') WHERE id = ?",
        (sales_nav_id, lead_id),
    )
    return None


def upsert_all_identities(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    identities: list[tuple[str, str]],
    *,
    source: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Register all identities; return (identity conflicts, linkedin_url conflicts)."""
    conflicts: list[dict] = []
    linkedin_conflicts: list[dict] = []
    rows = [(t, v) for t, v in identities if t not in NON_PERSISTED_IDENTITY_TYPES]
    if not rows:
        return conflicts, linkedin_conflicts

    # The UNIQUE (org_id, identity_type, identity_value_normalized) constraint
    # already decides this, so a per-identity pre-SELECT is redundant: a row
    # that exists is ignored whether it belongs to this lead or another. Insert
    # the whole set in one batch, and only pay for ownership lookups when the
    # insert count comes up short (i.e. something was already there).
    cur = conn.executemany(
        """INSERT OR IGNORE INTO lead_identities (
               org_id, lead_id, identity_type, identity_value_normalized,
               source, is_verified, created_at
           ) VALUES (
               ?, ?, ?, ?, ?, 0, datetime('now')
           )""",
        [(org_id, lead_id, t, v, source) for t, v in rows],
    )
    inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if inserted < len(rows):
        pairs = ", ".join("(?, ?)" for _ in rows)
        params: list = [org_id]
        for t, v in rows:
            params.extend((t, v))
        owners = {
            (r["identity_type"], r["identity_value_normalized"]): int(r["lead_id"])
            for r in conn.execute(
                f"""SELECT identity_type, identity_value_normalized, lead_id
                    FROM lead_identities
                    WHERE org_id = ?
                      AND (identity_type, identity_value_normalized) IN (VALUES {pairs})""",
                params,
            )
        }
        for t, v in rows:
            owner = owners.get((t, v))
            if owner is not None and owner != lead_id:
                conflicts.append({
                    "identity_type": t,
                    "value": v,
                    "existing_lead_id": owner,
                })

    # Both promotes are pure no-ops unless this batch actually carries the
    # identity type they read -- any promotion from a pre-existing identity row
    # already happened on the call that inserted it. Skipping them here drops
    # 4-6 SELECTs per event for the (common) payload with no LinkedIn data.
    itypes = {t for t, _ in rows}
    if "linkedin_url" in itypes:
        prom = promote_linkedin_url_from_identities(conn, org_id, lead_id)
        if prom:
            linkedin_conflicts.append(prom)
    if "linkedin_sales_nav_id" in itypes:
        sn_prom = promote_linkedin_sales_nav_id_from_identities(conn, org_id, lead_id)
        if sn_prom:
            linkedin_conflicts.append(sn_prom)
    return conflicts, linkedin_conflicts


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
    campaign_platform_id = (event_fields.get("campaign_id") or "").strip() or None
    campaign_name = (event_fields.get("campaign_name") or "").strip() or None

    if campaign_field and not campaign_platform_id and not campaign_name:
        if campaign_field.isdigit() or re.match(r"^[a-f0-9-]{8,}$", campaign_field, re.I):
            campaign_platform_id = campaign_field
        else:
            campaign_name = campaign_field

    if not campaign_platform_id:
        for path in (
            "campaign_id",
            "data.campaign_id",
            "campaign.id",
            "data.campaign.id",
            "lead.campaign_id",
        ):
            val = _get_path(raw, path) if "." in path else raw.get(path)
            if val is not None and str(val).strip():
                campaign_platform_id = str(val).strip()
                break

    if not campaign_name:
        for key in ("campaign_name", "campaign", "data.campaign_name", "data.campaign.name"):
            val = _get_path(raw, key) if "." in key else raw.get(key)
            if val is not None and str(val).strip():
                text = str(val).strip()
                if text != (campaign_platform_id or ""):
                    campaign_name = text
                    break

    if (platform or "").lower() == "calendly":
        campaign_platform_id, campaign_name = enrich_calendly_campaign_fields(
            raw, campaign_platform_id, campaign_name
        )
        if campaign_platform_id:
            parsed_id = parse_calendly_event_type_id(campaign_platform_id)
            if parsed_id:
                campaign_platform_id = parsed_id

    return CampaignContext(
        source_platform=platform,
        campaign_platform_id=campaign_platform_id,
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
    if ctx.campaign_platform_id:
        return ctx.campaign_platform_id
    return "unknown"


MULTI_WORKSPACE_HOLD_MESSAGE = (
    "Multi-workspace mode: events are held unprocessed until each campaign is "
    "mapped to a workspace. Create workspaces and campaign maps, then replay "
    "quarantined events."
)


def format_no_campaign_event_message(ctx: CampaignContext) -> str:
    """User-facing instructions when a relay event has no campaign metadata."""
    from user_messages import no_campaign_event_message

    return no_campaign_event_message(platform=ctx.source_platform)


def format_unmapped_campaign_message(ctx: CampaignContext) -> str:
    """User-facing instructions when multi-workspace routing cannot resolve a campaign."""
    from user_messages import unmapped_campaign_message

    return unmapped_campaign_message(
        label=campaign_display_label(ctx),
        platform=ctx.source_platform,
    )


@dataclass
class CampaignRoutingCache:
    """In-memory campaign maps for one pull session (avoids per-event SQL)."""

    config: OrgRoutingConfig
    _id_exact: dict[tuple[str, str], RoutingResult]
    _name_exact: dict[tuple[str, str], RoutingResult]
    _rules: list[tuple[str, str, str, RoutingResult]]

    @classmethod
    def load(
        cls,
        conn: sqlite3.Connection,
        org_id: str,
        config: OrgRoutingConfig,
    ) -> CampaignRoutingCache:
        id_exact: dict[tuple[str, str], RoutingResult] = {}
        name_exact: dict[tuple[str, str], RoutingResult] = {}
        rules: list[tuple[str, str, str, RoutingResult]] = []
        if config.mode != WORKSPACE_ROUTING_MULTI:
            return cls(config=config, _id_exact=id_exact, _name_exact=name_exact, _rules=rules)

        rows = conn.execute(
            """SELECT id, workspace_id, match_strategy, source_platform, campaign_platform_id,
                      campaign_name_normalized, priority
               FROM campaign_workspace_map
               WHERE org_id = ? AND is_active = 1
               ORDER BY priority ASC""",
            (org_id,),
        ).fetchall()
        for row in rows:
            sp = str(row["source_platform"] or "*")
            result = RoutingResult(
                workspace_id=row["workspace_id"],
                match_strategy=row["match_strategy"],
                map_id=row["id"],
            )
            strat = row["match_strategy"]
            if strat == "id_exact" and row["campaign_platform_id"]:
                key = (sp, str(row["campaign_platform_id"]))
                if key not in id_exact:
                    id_exact[key] = result
            elif strat == "name_exact" and row["campaign_name_normalized"]:
                key = (sp, str(row["campaign_name_normalized"]))
                if key not in name_exact:
                    name_exact[key] = result
            elif strat in ("rule_contains", "rule_prefix", "rule_regex"):
                pattern = str(row["campaign_name_normalized"] or "")
                if pattern:
                    rules.append((sp, strat, pattern, result))

        return cls(config=config, _id_exact=id_exact, _name_exact=name_exact, _rules=rules)

    def resolve(self, ctx: CampaignContext) -> Optional[RoutingResult]:
        if self.config.mode == WORKSPACE_ROUTING_SINGLE:
            if not self.config.default_workspace_id:
                return None
            return RoutingResult(
                workspace_id=self.config.default_workspace_id,
                match_strategy="single_workspace",
            )
        platform = ctx.source_platform
        platforms = (platform, "*")
        if ctx.campaign_platform_id:
            for sp in platforms:
                hit = self._id_exact.get((sp, ctx.campaign_platform_id))
                if hit:
                    return hit
        if ctx.campaign_name_normalized:
            for sp in platforms:
                hit = self._name_exact.get((sp, ctx.campaign_name_normalized))
                if hit:
                    return hit
            name_for_rules = ctx.campaign_name_normalized
            for sp, strat, pattern, result in self._rules:
                if sp not in platforms:
                    continue
                if strat == "rule_contains" and pattern in name_for_rules:
                    return result
                if strat == "rule_prefix" and name_for_rules.startswith(pattern):
                    return result
                if strat == "rule_regex":
                    try:
                        if re.search(pattern, name_for_rules):
                            return result
                    except re.error:
                        continue
        return None


def resolve_workspace(
    conn: sqlite3.Connection,
    org_id: str,
    ctx: CampaignContext,
) -> Optional[RoutingResult]:
    """ID-first campaign routing with name and rule fallbacks."""
    platform = ctx.source_platform

    if ctx.campaign_platform_id:
        row = conn.execute(
            """SELECT id, workspace_id, match_strategy FROM campaign_workspace_map
               WHERE org_id = ? AND source_platform IN (?, '*') AND is_active = 1
                 AND match_strategy = 'id_exact' AND campaign_platform_id = ?
               ORDER BY priority ASC LIMIT 1""",
            (org_id, platform, ctx.campaign_platform_id),
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
    *,
    routing_config: Optional[OrgRoutingConfig] = None,
    routing_cache: Optional[CampaignRoutingCache] = None,
) -> Optional[RoutingResult]:
    """
    Resolve workspace using org routing mode:
      single — all events go to default_workspace_id
      multi  — campaign maps required; None if unmapped
    """
    if routing_cache is not None:
        return routing_cache.resolve(ctx)
    config = routing_config or get_org_routing_config(conn, org_id)
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
    relay_id = (external_event_id or "").strip()
    if relay_id:
        existing = conn.execute(
            """SELECT id FROM unmapped_campaign_queue
               WHERE org_id = ? AND external_event_id = ? AND status = 'pending'""",
            (org_id, relay_id),
        ).fetchone()
        if existing:
            return existing["id"]
    qid = f"q_{datetime.now(timezone.utc).timestamp()}".replace(".", "")
    conn.execute(
        """INSERT INTO unmapped_campaign_queue (
               id, org_id, source_platform, campaign_platform_id, campaign_name_raw,
               campaign_name_normalized, external_event_id, reason, status,
               payload_json, received_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))""",
        (
            qid,
            org_id,
            ctx.source_platform,
            ctx.campaign_platform_id,
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
    add("linkedin_sales_nav_id", identity.get("linkedin_sales_nav_id"))
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
    if identity_type in ("linkedin_sales_nav_id", "external_id"):
        # Sales Nav IDs are stored in their original case (a design choice --
        # see normalize_linkedin_sales_nav_id()), so the same ID imported via
        # two different paths can differ only in casing and silently fail to
        # match on a plain '='. external_id is already lowercased at write
        # time (normalize_external_id) so this is a defensive no-op for it,
        # not a fix. idx_lead_identities_type_value_lower backs this so it
        # still hits an index instead of a table scan.
        row = conn.execute(
            """SELECT lead_id FROM lead_identities
               WHERE org_id = ? AND identity_type = ? AND LOWER(identity_value_normalized) = LOWER(?)""",
            (org_id, identity_type, value_normalized),
        ).fetchone()
    else:
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
    """Stable relay push/replay key: email > linkedin > external_id > composite fallback.

    The composite (name+company[+domain[+title]]) tier is computed on the fly
    from the leads/companies row rather than read from lead_identities -- those
    fuzzy types are no longer persisted there (NON_PERSISTED_IDENTITY_TYPES),
    since they were never used for actual lead matching and were >96%
    redundant with a strong identity already on the same lead.
    """
    row = conn.execute(
        """SELECT l.email, l.linkedin_url, l.name, l.company, l.title,
                  co.domain AS company_domain
           FROM leads l LEFT JOIN companies co ON co.id = l.company_id
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    if row and row["email"]:
        return str(row["email"]).strip().lower()
    if row and row["linkedin_url"]:
        return str(row["linkedin_url"]).strip()
    id_row = conn.execute(
        """SELECT identity_type, identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ? AND identity_type = 'external_id'
           ORDER BY created_at LIMIT 1""",
        (org_id, lead_id),
    ).fetchone()
    if id_row:
        return f"{id_row['identity_type']}:{id_row['identity_value_normalized']}"
    if not row:
        return ""
    norm_name = normalize_person_name(row["name"])
    domain = (row["company_domain"] or "").strip().lower()
    title = (row["title"] or "").strip().lower()
    if norm_name and domain:
        if title:
            return f"name_company_domain_title:{norm_name}|{domain}|{title}"
        return f"name_company_domain:{norm_name}|{domain}"
    if norm_name and row["company"]:
        ckey = normalize_company_name_key(row["company"])
        if ckey:
            return f"name_company:{norm_name}|{ckey}"
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
    *,
    promote_linkedin: bool = True,
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
               org_id, lead_id, identity_type, identity_value_normalized,
               source, is_verified, created_at
           ) VALUES (
               ?, ?, ?, ?, ?, 0, datetime('now')
           )""",
        (org_id, lead_id, identity_type, value_normalized, source),
    )
    if identity_type == "linkedin_url" and promote_linkedin:
        promote_linkedin_url_from_identities(conn, org_id, lead_id)
    elif identity_type == "linkedin_sales_nav_id" and promote_linkedin:
        promote_linkedin_sales_nav_id_from_identities(conn, org_id, lead_id)


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
        """SELECT id, status, current_status_label, current_status_sentiment, contact_priority
           FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
        (workspace_id, lead_id),
    ).fetchone()
    if row:
        extra_sets = []
        extra_params = []
        # Only bump updated_at (and therefore mark this row as needing a push)
        # when a provided field actually differs from what's stored — an UPDATE
        # that changes nothing is not a local change, and treating it as one is
        # exactly the self-bump loop in bug-pending-sync-self-bump.md (relay
        # syncs echoing back data we already have, forever, never settling).
        changed = False
        if current_status_label is not None and current_status_label != row["current_status_label"]:
            extra_sets.append("current_status_label = ?")
            extra_params.append(current_status_label)
            changed = True
        if current_status_sentiment is not None and current_status_sentiment != row["current_status_sentiment"]:
            extra_sets.append("current_status_sentiment = ?")
            extra_params.append(current_status_sentiment)
            changed = True
        if contact_priority is not None and contact_priority != row["contact_priority"]:
            extra_sets.append("contact_priority = ?")
            extra_params.append(contact_priority)
            changed = True
        if not changed:
            return row["id"]
        sets = "updated_at = datetime('now'), " + ", ".join(extra_sets)
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
                     datetime('now'), NULL, datetime('now'), datetime('now'))""",
        (ws_lead_id, org_id, workspace_id, lead_id, status, owner_user_id,
         current_status_label, current_status_sentiment, contact_priority),
    )
    return ws_lead_id


def append_workspace_event(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_id: str,
    lead_id: int,
    workspace_lead_id: str = "",
    *,
    event_type: str,
    event_at: str,
    idempotency_key: str,
    event_id: Optional[int] = None,
) -> Optional[int]:
    """Index an event into a workspace: inbound dedupe + the CRM age filter/cursor.

    `event_id` points at the `events` row that holds the content. This table stores
    no payload of its own -- it used to keep a full copy of events.metadata_json,
    body and all, that nothing ever read. Join `events` when you need content.

    `workspace_lead_id` is accepted and ignored (the column it fed was never read).

    Returns the new rowid, or None when the event was already recorded.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_events (
               org_id, workspace_id, lead_id, event_id, event_type, event_at,
               idempotency_key, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (org_id, workspace_id, lead_id, event_id, event_type, event_at, idempotency_key),
    )
    if cur.rowcount == 0:
        return None
    return int(cur.lastrowid)


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
    campaign_platform_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: str = "id_exact",
    priority: int = 100,
) -> str:
    if not campaign_platform_id and not campaign_name:
        raise ValueError("At least one of campaign_platform_id or campaign_name is required for a mapping rule")
    key = campaign_platform_id or normalize_campaign_name(campaign_name) or "rule"
    safe_key = re.sub(r"[^\w.-]+", "_", str(key))
    map_id = f"map_{source_platform}_{match_strategy}_{safe_key}"
    conn.execute(
        """INSERT OR REPLACE INTO campaign_workspace_map (
               id, org_id, source_platform, campaign_platform_id, campaign_name_normalized,
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
            campaign_platform_id,
            normalize_campaign_name(campaign_name),
            workspace_id,
            match_strategy,
            priority,
            map_id,
        ),
    )
    return map_id

