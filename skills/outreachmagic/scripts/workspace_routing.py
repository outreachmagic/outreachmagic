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


def matchable_identities(
    identities: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """The identities that can actually match this lead again later.

    build_import_identities() always returns *something* for a named profile --
    it falls through to name_company/import_key. Those types are never persisted
    to lead_identities and are excluded from matching, so a lead whose only
    identities are weak is created, is unmatchable, and is therefore re-created
    from scratch on every subsequent sync. That is what produced ~10.8k
    "Unknown"/no-email rows in a single backfill window (name="Unknown" is
    truthy, so it earns an import_key and sails past a `if not identities` check).

    Callers gate lead *creation* on this being non-empty.
    """
    return [(t, v) for t, v in identities if t not in NON_PERSISTED_IDENTITY_TYPES]

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


def build_sales_nav_url(nav_id: str) -> Optional[str]:
    """Synthesize a Sales Navigator lead URL from a stored member token.

    Inverse of extract_sales_nav_id_from_linkedin_url: given a normalized
    ACwAA.../ACoAA... token (or anything normalize_linkedin_sales_nav_id
    accepts), return the linkedin.com/sales/lead/<token> URL a user can open.
    Returns None when the value isn't a valid Sales Navigator id.
    """
    norm = normalize_linkedin_sales_nav_id(nav_id)
    if not norm:
        return None
    return f"https://www.linkedin.com/sales/lead/{norm}"


def linkedin_display_url(
    linkedin_url: Optional[str] = None,
    linkedin_sales_nav_id: Optional[str] = None,
) -> Optional[str]:
    """The best openable LinkedIn URL for a lead: prefer the public profile
    URL, else synthesize a Sales Navigator URL from the stored member token.

    Accepts the two columns directly so callers can pass a sqlite row's fields
    without building a dict. Returns None when neither is usable.
    """
    public = (linkedin_url or "").strip()
    if public:
        if public.startswith(("http://", "https://")):
            return public
        return f"https://{public.lstrip('/')}"
    return build_sales_nav_url(linkedin_sales_nav_id or "")


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
    # profile["linkedin"] is already hash/Sales-Nav filtered (that's the field
    # written to leads.linkedin_url) -- fall back to the unfiltered
    # profile["linkedin_raw"] so a Sales Nav URL that arrived ONLY through the
    # linkedin/LinkedInUrl column (no separate sales-nav-id column) still
    # yields a linkedin_sales_nav_id identity. add()'s (itype, norm) dedup
    # means trying both candidates is safe even when they're the same value.
    for li in (profile.get("linkedin"), profile.get("linkedin_raw")):
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
    # This hand-rolled normalization handled scheme-less www. + path stripping
    # but not www2./www3., not a trailing FQDN root dot, and not the "no dot at
    # all" case -- so it produced identity keys that disagreed with the ones
    # ensure_company()/company_identities store. One normalizer, one key.
    from pipeline_utils import normalize_company_domain
    domain = normalize_company_domain(company_domain or extra.get("company_domain")) or ""
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
    """True for a Sales Nav hash slug OR a bare Sales Nav URL. linkedin_in_slug()
    only matches linkedin.com/in/<slug> -- a linkedin.com/sales/people/<token>
    or /sales/lead/<token> URL (Apify's salesNavigatorUrl field, among others)
    has no /in/ segment at all, so it fell through as "not a hash" and leaked
    into the linkedin_url column verbatim. Every caller (_best_linkedin_from_row,
    should_replace_linkedin_url, linkedin_url_field_conflict) treats this
    function as the single gatekeeper for "is this safe to store as the public
    linkedin_url", so the fix belongs here rather than at each call site."""
    raw = (url or "").strip().lower()
    if "linkedin.com/sales/people/" in raw or "linkedin.com/sales/lead/" in raw:
        return True
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
        # Case-insensitive: the display column is mixed-case where we have it,
        # lowercase where we don't, so equality would miss a legitimate conflict
        # between an "ACwAA..." write and a stored "acwaa..." row for the same person.
        "SELECT id FROM leads WHERE LOWER(linkedin_sales_nav_id) = LOWER(?) AND id != ?",
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


def _sales_nav_match_key(value: str) -> str:
    """Case-folded storage key for lead_identities.identity_value_normalized.
    Sales Navigator matches case-insensitively; folding at storage keeps the
    UNIQUE constraint from splitting 'ACwAA...' and 'acwaa...' into two rows."""
    return value.lower()


def _upgrade_lead_sales_nav_id_case(
    conn: sqlite3.Connection, lead_id: int, canonical_value: str,
) -> None:
    """Prefer mixed case on the display column. Called after any write that
    carries a canonical (mixed-case) sales-nav id, so a lead whose column is
    empty or lowercase gets upgraded as soon as we see the properly-cased form
    -- including on a fresh D1 pull after this box's local was lowercased by an
    earlier migration pass."""
    if not canonical_value or canonical_value == canonical_value.lower():
        return
    conn.execute(
        """UPDATE leads
              SET linkedin_sales_nav_id = ?, updated_at = datetime('now')
            WHERE id = ?
              AND (linkedin_sales_nav_id IS NULL
                   OR linkedin_sales_nav_id = LOWER(linkedin_sales_nav_id))""",
        (canonical_value, lead_id),
    )


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
    persist_weak: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Register all identities; return (identity conflicts, linkedin_url conflicts).

    persist_weak=True also stores the composite (name_company/import_key) types.
    Only set it for leads that have no strong identity at all -- otherwise they
    would have nothing to match on and would duplicate on every re-import.
    """
    conflicts: list[dict] = []
    linkedin_conflicts: list[dict] = []
    rows = [
        (t, v) for t, v in identities
        if persist_weak or t not in NON_PERSISTED_IDENTITY_TYPES
    ]
    if not rows:
        return conflicts, linkedin_conflicts

    # Case-fold sales-nav for the storage key; keep the canonical (input) value
    # separately so we can upgrade the display column after.
    def _key(t: str, v: str) -> str:
        return _sales_nav_match_key(v) if t == "linkedin_sales_nav_id" else v
    storage_rows = [(t, _key(t, v)) for t, v in rows]

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
        [(org_id, lead_id, t, v, source) for t, v in storage_rows],
    )
    inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if inserted < len(storage_rows):
        pairs = ", ".join("(?, ?)" for _ in storage_rows)
        params: list = [org_id]
        for t, v in storage_rows:
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
        for (t, canonical_v), (_, stored_v) in zip(rows, storage_rows):
            owner = owners.get((t, stored_v))
            if owner is not None and owner != lead_id:
                conflicts.append({
                    "identity_type": t,
                    "value": canonical_v,
                    "existing_lead_id": owner,
                })

    # Opportunistically upgrade the display column with the canonical case.
    for t, canonical_v in rows:
        if t == "linkedin_sales_nav_id":
            _upgrade_lead_sales_nav_id_case(conn, lead_id, canonical_v)

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


def _campaign_name_matches_rule(match_strategy: str, pattern: str, name_normalized: str) -> bool:
    """Whether a pattern rule (rule_contains/rule_prefix/rule_regex) matches a normalized campaign name."""
    if not pattern or not name_normalized:
        return False
    if match_strategy == "rule_contains":
        return pattern in name_normalized
    if match_strategy == "rule_prefix":
        return name_normalized.startswith(pattern)
    if match_strategy == "rule_regex":
        try:
            return bool(re.search(pattern, name_normalized))
        except re.error:
            return False
    return False


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
                if _campaign_name_matches_rule(strat, pattern, name_for_rules):
                    return result
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
            if _campaign_name_matches_rule(rule["match_strategy"], pattern, name_for_rules):
                return RoutingResult(
                    workspace_id=rule["workspace_id"],
                    match_strategy=rule["match_strategy"],
                    map_id=rule["id"],
                )

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


def find_company_by_identity(
    conn: sqlite3.Connection,
    org_id: str,
    identity_type: str,
    value_normalized: str,
) -> Optional[int]:
    """Mirrors find_lead_by_identity() for companies. 'domain' falls back to
    the legacy companies.domain column, since most of the 60k+ existing
    companies predate company_identities and were never backfilled into it.
    linkedin_company_id/linkedin_company_url/name_normalized are brand-new
    identity types with no legacy column to fall back to."""
    row = conn.execute(
        """SELECT company_id FROM company_identities
           WHERE org_id = ? AND identity_type = ? AND identity_value_normalized = ?""",
        (org_id, identity_type, value_normalized),
    ).fetchone()
    if row:
        return int(row["company_id"])
    if identity_type == "domain":
        row = conn.execute(
            "SELECT id FROM companies WHERE domain = ?", (value_normalized,)
        ).fetchone()
        return int(row["id"]) if row else None
    return None


_IDENTITY_VALUE_NORMALIZERS = {
    "email": normalize_email,
    "linkedin_url": normalize_linkedin,
}


def resolve_lead_ids_by_identity(
    conn: sqlite3.Connection, org_id: str, identity_type: str, values: list[str],
) -> tuple[list[int], list[str]]:
    """Resolve raw identity values (e.g. a batch of linkedin_sales_nav_id
    strings) to lead ids via find_lead_by_identity, one lookup per value --
    no compound SELECT, so there's no SQLITE_LIMIT_COMPOUND_SELECT to chunk
    around. Returns (lead_ids, values that didn't resolve to any lead).
    """
    normalize = _IDENTITY_VALUE_NORMALIZERS.get(identity_type)
    lead_ids: list[int] = []
    unresolved: list[str] = []
    for raw_value in values:
        value = normalize(raw_value) if normalize else (raw_value or "").strip()
        if not value:
            unresolved.append(raw_value)
            continue
        lead_id = find_lead_by_identity(conn, org_id, identity_type, value)
        if lead_id:
            lead_ids.append(lead_id)
        else:
            unresolved.append(raw_value)
    return lead_ids, unresolved


def lead_entity_key(conn: sqlite3.Connection, org_id: str, lead_id: int) -> str:
    """The lead's immutable relay key: uid:<uid>.

    This used to derive the key from mutable columns (email > linkedin > ...),
    which had two failure modes:

      * Finding a lead's email MOVED its wire identity from the LinkedIn URL to
        the address. The relay's old snapshot orphaned and a new one appeared
        under the new key, stranding every bit of workspace state filed under the
        old one. 52,693 leads are one email-find away from exactly that today.

      * A lead with neither email nor LinkedIn produced an EMPTY key, and the
        push loop skips empty keys -- so 2,830 real leads have never reached the
        relay at all. (This function had a name+company fallback;
        entity_key_from_prefetch, which the push path actually calls, did not.
        Two implementations of one concept, disagreeing.)

    A uid has neither problem: every lead has one, it is stamped once by a
    database trigger, and nothing can change it. Email/LinkedIn/sales-nav become
    *aliases* -- see lead_aliases() -- so inbound webhooks keyed by a natural
    identifier still resolve.
    """
    row = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row or not row["uid"]:
        return ""
    return f"uid:{row['uid']}"


"""Aliases are built in lead_sync._assemble_lead_core_sync_payload(), which already
has the identity rows prefetched. Deliberately NOT duplicated here: two
implementations of one concept is what produced the entity_key divergence this
migration exists to fix (lead_entity_key had a name+company fallback that
entity_key_from_prefetch lacked, so 2,830 leads were pushable by one code path and
invisible to the other)."""


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
    """The lead's external_id, or None. A merge can leave a lead with more than
    one external_id row (each merged lead brings its own) -- ORDER BY makes the
    pick deterministic (most recently recorded wins) and, critically, agrees
    with the prefetch batch path in lead_sync.py's _load_lead_sync_prefetch,
    which must resolve to the exact same value or a lead's payload flips
    depending on which code path built it.
    """
    row = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ? AND identity_type = 'external_id'
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (org_id, lead_id),
    ).fetchone()
    return row["identity_value_normalized"] if row else None


class IdentityConflict(ValueError):
    """One identity, two leads.

    A ValueError subclass so every existing `except ValueError` keeps working
    and the API still answers 400-shaped errors. The extra fields are what lets
    a caller offer the merge instead of dead-ending on the message: the whole
    resolution is "these are the same person", and the person reading the error
    already knows that.
    """

    def __init__(self, message: str, *, owner_lead_id: int, lead_id: int,
                 identity_type: str, value: str):
        super().__init__(message)
        self.owner_lead_id = owner_lead_id
        self.lead_id = lead_id
        self.identity_type = identity_type
        self.value = value

    def as_payload(self) -> dict:
        return {
            "owner_lead_id": self.owner_lead_id,
            "lead_id": self.lead_id,
            "identity_type": self.identity_type,
            "value": self.value,
        }


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
    stored_value = (
        _sales_nav_match_key(value_normalized)
        if identity_type == "linkedin_sales_nav_id"
        else value_normalized
    )
    existing = conn.execute(
        """SELECT lead_id FROM lead_identities
           WHERE org_id = ? AND identity_type = ? AND identity_value_normalized = ?""",
        (org_id, identity_type, stored_value),
    ).fetchone()
    if existing and int(existing["lead_id"]) != lead_id:
        # Two records claiming one identity is usually one person recorded
        # twice, so the refusal names the other lead. Queuing the merge is NOT
        # done here: this raise unwinds the caller's transaction, which would
        # take the queue row with it, and opening a second connection while the
        # caller holds a write lock deadlocks against yourself. The surface that
        # catches this queues it afterwards -- see dashboard_server.dispatch.
        raise IdentityConflict(
            f"identity conflict: {identity_type}={stored_value} belongs to lead "
            f"{existing['lead_id']}, not {lead_id}",
            owner_lead_id=int(existing["lead_id"]),
            lead_id=lead_id,
            identity_type=identity_type,
            value=stored_value,
        )
    conn.execute(
        """INSERT OR IGNORE INTO lead_identities (
               org_id, lead_id, identity_type, identity_value_normalized,
               source, is_verified, created_at
           ) VALUES (
               ?, ?, ?, ?, ?, 0, datetime('now')
           )""",
        (org_id, lead_id, identity_type, stored_value, source),
    )
    if identity_type == "linkedin_sales_nav_id":
        _upgrade_lead_sales_nav_id_case(conn, lead_id, value_normalized)
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
    current_sentiment_since: Optional[str] = None,
    contact_priority: Optional[int] = None,
) -> str:
    # INSERT OR IGNORE is atomic — eliminates the SELECT→INSERT race that causes
    # "UNIQUE constraint failed: workspace_leads.id" when parallel pull batches
    # both see no row and both attempt to create the same wl_{ws}_{lead} id.
    ws_lead_id = f"wl_{workspace_id}_{lead_id}"
    conn.execute(
        """INSERT OR IGNORE INTO workspace_leads (
               id, org_id, workspace_id, lead_id, status, owner_user_id,
               current_status_label, current_status_sentiment, current_sentiment_since,
               contact_priority,
               stage_entered_at, last_activity_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                     COALESCE(?, CASE WHEN ? IS NOT NULL THEN datetime('now') END), ?,
                     datetime('now'), NULL, datetime('now'), datetime('now'))""",
        # current_sentiment_since: an explicit anchor wins; otherwise, if a
        # sentiment is being set on this fresh row, stamp now; else leave NULL.
        (ws_lead_id, org_id, workspace_id, lead_id, status, owner_user_id,
         current_status_label, current_status_sentiment,
         current_sentiment_since, current_status_sentiment,
         contact_priority),
    )
    row = conn.execute(
        """SELECT id, current_status_label, current_status_sentiment,
                  current_sentiment_since, contact_priority
           FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
        (workspace_id, lead_id),
    ).fetchone()
    if row is None:
        return ws_lead_id
    # Only bump updated_at when a provided field actually differs from what's
    # stored — an UPDATE that changes nothing is not a local change, and treating
    # it as one is exactly the self-bump loop in bug-pending-sync-self-bump.md.
    extra_sets: list[str] = []
    extra_params: list = []
    if current_status_label is not None and current_status_label != row["current_status_label"]:
        extra_sets.append("current_status_label = ?")
        extra_params.append(current_status_label)
    if current_status_sentiment is not None and current_status_sentiment != row["current_status_sentiment"]:
        extra_sets.append("current_status_sentiment = ?")
        extra_params.append(current_status_sentiment)
        # Sentiment changed: stamp the run start. Prefer an explicit since from
        # the caller (a snapshot carrying it); otherwise anchor to now.
        extra_sets.append("current_sentiment_since = COALESCE(?, datetime('now'))")
        extra_params.append(current_sentiment_since)
    elif (current_sentiment_since is not None
          and current_sentiment_since != row["current_sentiment_since"]):
        # Sentiment unchanged but the snapshot carries a corrected run start
        # (e.g. an earlier, backfilled anchor) — apply it without a spurious flip.
        extra_sets.append("current_sentiment_since = ?")
        extra_params.append(current_sentiment_since)
    if contact_priority is not None and contact_priority != row["contact_priority"]:
        extra_sets.append("contact_priority = ?")
        extra_params.append(contact_priority)
    if extra_sets:
        sets = "updated_at = datetime('now'), " + ", ".join(extra_sets)
        conn.execute(
            f"UPDATE workspace_leads SET {sets} WHERE id = ?",
            (*extra_params, row["id"]),
        )
    return row["id"]


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
    map_source: str = "manual",
) -> str:
    if not campaign_platform_id and not campaign_name:
        raise ValueError("At least one of campaign_platform_id or campaign_name is required for a mapping rule")
    key = campaign_platform_id or normalize_campaign_name(campaign_name) or "rule"
    safe_key = re.sub(r"[^\w.-]+", "_", str(key))
    map_id = f"map_{source_platform}_{match_strategy}_{safe_key}"
    conn.execute(
        """INSERT OR REPLACE INTO campaign_workspace_map (
               id, org_id, source_platform, campaign_platform_id, campaign_name_normalized,
               workspace_id, match_strategy, priority, is_active, map_source, created_at, updated_at
           ) VALUES (
               ?, ?, ?, ?, ?, ?, ?, ?, 1, ?,
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
            map_source,
            map_id,
        ),
    )
    return map_id


def deactivate_shadowed_backfill_rules(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    source_platform: str,
    match_strategy: str,
    pattern: str,
) -> list[dict]:
    """Deactivate single_mode_backfill name_exact rows that a newly-arrived pattern rule now covers.

    Prevention only: manual/cloud-synced name_exact rows are never touched, and
    rows that predate the map_source column all read as 'manual', so this cannot
    repair a database that already has the shadowing bug -- use detect_shadow_conflicts.
    """
    normalized = normalize_campaign_name(pattern) or (pattern or "").strip().lower()
    if not normalized:
        return []
    rows = conn.execute(
        """SELECT id, campaign_name_normalized, workspace_id FROM campaign_workspace_map
           WHERE org_id = ? AND source_platform IN (?, '*') AND is_active = 1
             AND match_strategy = 'name_exact'
             AND map_source = 'single_mode_backfill'""",
        (org_id, source_platform),
    ).fetchall()
    deactivated: list[dict] = []
    for row in rows:
        name_norm = row["campaign_name_normalized"] or ""
        if _campaign_name_matches_rule(match_strategy, normalized, name_norm):
            conn.execute(
                "UPDATE campaign_workspace_map SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            deactivated.append(
                {
                    "map_id": row["id"],
                    "campaign_name": name_norm,
                    "workspace_id": row["workspace_id"],
                }
            )
    return deactivated


def detect_shadow_conflicts(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    source_platform: Optional[str] = None,
) -> list[dict]:
    """Active name_exact rows shadowed by a broader active pattern rule that targets a different workspace.

    Content-based, so it works regardless of map_source or when a row was
    created -- this is the repair path for databases that already have stale
    name_exact rows from the single-mode backfill.
    """
    name_rows = conn.execute(
        """SELECT id, campaign_name_normalized, workspace_id, source_platform
           FROM campaign_workspace_map
           WHERE org_id = ? AND is_active = 1 AND match_strategy = 'name_exact'
             AND campaign_name_normalized IS NOT NULL""",
        (org_id,),
    ).fetchall()
    rule_rows = conn.execute(
        """SELECT id, campaign_name_normalized, workspace_id, source_platform, match_strategy, priority
           FROM campaign_workspace_map
           WHERE org_id = ? AND is_active = 1
             AND match_strategy IN ('rule_contains', 'rule_prefix', 'rule_regex')
           ORDER BY priority ASC""",
        (org_id,),
    ).fetchall()
    conflicts: list[dict] = []
    for name_row in name_rows:
        name_norm = name_row["campaign_name_normalized"] or ""
        if source_platform is not None and name_row["source_platform"] not in (source_platform, "*"):
            continue
        for rule in rule_rows:
            # A '*' rule matches any platform; a platform-specific rule only
            # shadows a name_exact row on the same platform or a '*' one.
            if rule["source_platform"] != "*" and name_row["source_platform"] not in (rule["source_platform"], "*"):
                continue
            pattern = rule["campaign_name_normalized"] or ""
            if not _campaign_name_matches_rule(rule["match_strategy"], pattern, name_norm):
                continue
            if rule["workspace_id"] == name_row["workspace_id"]:
                continue
            conflicts.append(
                {
                    "name_exact_map_id": name_row["id"],
                    "campaign_name": name_norm,
                    "name_exact_workspace_id": name_row["workspace_id"],
                    "shadowing_rule_map_id": rule["id"],
                    "shadowing_rule_pattern": pattern,
                    "shadowing_rule_workspace_id": rule["workspace_id"],
                }
            )
            break
    return conflicts


def deactivate_campaign_map(conn: sqlite3.Connection, org_id: str, map_id: str) -> dict:
    """Soft-deactivate a single campaign_workspace_map row by id (explicit, auditable, never bulk)."""
    row = conn.execute(
        "SELECT id, is_active FROM campaign_workspace_map WHERE org_id = ? AND id = ?",
        (org_id, map_id),
    ).fetchone()
    if not row:
        return {"status": "error", "error": f"campaign map not found: {map_id}"}
    if not row["is_active"]:
        return {"status": "noop", "map_id": map_id, "detail": "already inactive"}
    conn.execute(
        "UPDATE campaign_workspace_map SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
        (map_id,),
    )
    return {"status": "deactivated", "map_id": map_id}


def _move_workspace_lead_row(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    old_workspace_id: str,
    target_workspace_id: str,
) -> None:
    """Relocate a lead's workspace_leads row + tags + linkedin-status from one workspace to another.

    Follows the merge_leads() cross-workspace pattern: delete-if-destination-exists,
    else UPDATE workspace_id; INSERT OR IGNORE + DELETE for the join tables.
    """
    if old_workspace_id == target_workspace_id:
        return
    old_row = conn.execute(
        "SELECT id, status FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
        (old_workspace_id, lead_id),
    ).fetchone()
    if not old_row:
        upsert_workspace_lead(conn, org_id, target_workspace_id, lead_id)
        return
    existing = conn.execute(
        "SELECT id FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
        (target_workspace_id, lead_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM workspace_leads WHERE id = ?", (old_row["id"],))
    else:
        conn.execute(
            "UPDATE workspace_leads SET workspace_id = ?, updated_at = datetime('now') WHERE id = ?",
            (target_workspace_id, old_row["id"]),
        )
    for tag_row in conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
        (old_workspace_id, lead_id),
    ).fetchall():
        tag_id = f"wlt_{target_workspace_id}_{lead_id}_{hashlib.md5(tag_row['tag'].encode()).hexdigest()[:8]}"
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, ?)""",
            (tag_id, target_workspace_id, lead_id, tag_row["tag"]),
        )
    conn.execute(
        "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
        (old_workspace_id, lead_id),
    )
    for li in conn.execute(
        """SELECT sender_profile, is_connected, is_request_pending, connected_at, request_sent_at
           FROM workspace_lead_linkedin_status WHERE workspace_id = ? AND lead_id = ?""",
        (old_workspace_id, lead_id),
    ).fetchall():
        li_id = f"lis_{target_workspace_id}_{lead_id}_{(li['sender_profile'] or '')[:20]}"
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_linkedin_status
               (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending,
                connected_at, request_sent_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                li_id, target_workspace_id, lead_id, li["sender_profile"],
                li["is_connected"], li["is_request_pending"], li["connected_at"], li["request_sent_at"],
            ),
        )
    conn.execute(
        "DELETE FROM workspace_lead_linkedin_status WHERE workspace_id = ? AND lead_id = ?",
        (old_workspace_id, lead_id),
    )


def reconcile_workspace_routing(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    platform_filter: Optional[str] = None,
    from_workspace_id: Optional[str] = None,
    dry_run: bool = True,
    limit: int = 0,
) -> dict:
    """Re-apply current routing rules to already-ingested workspace_lead_events.

    Grouping is per campaign_name (source_platform is not persisted per event, so
    platform_filter is best-effort against leads.latest_source_platform). Only rows
    tied to campaigns whose resolution now differs from where they currently sit are
    moved. Events with no derivable campaign name are counted in skipped_no_campaign.

    Run this only after any stale name_exact row shadowing a broader rule has been
    cleared (see detect_shadow_conflicts / deactivate_campaign_map) -- resolution
    keeps returning the shadowing workspace until then.
    """
    rows = conn.execute(
        """SELECT wle.id AS wle_id, wle.workspace_id AS ws, wle.lead_id AS lead_id,
                  c.name AS campaign_name, l.latest_source_platform AS platform
           FROM workspace_lead_events wle
           LEFT JOIN events e ON e.id = wle.event_id
           LEFT JOIN campaigns c ON c.id = e.campaign_id
           LEFT JOIN leads l ON l.id = wle.lead_id
           WHERE wle.org_id = ?""",
        (org_id,),
    ).fetchall()

    skipped_no_campaign = 0
    # (campaign_name, resolve_platform) -> list of rows
    groups: dict[tuple, list] = {}
    for row in rows:
        campaign_name = row["campaign_name"]
        if not campaign_name:
            skipped_no_campaign += 1
            continue
        if platform_filter is not None and row["platform"] != platform_filter:
            continue
        if from_workspace_id is not None and row["ws"] != from_workspace_id:
            continue
        resolve_platform = row["platform"] or "*"
        groups.setdefault((campaign_name, resolve_platform), []).append(row)

    campaign_names_checked: set = set()
    mismatch_subgroups: list = []  # (campaign_name, target, [rows])
    for (campaign_name, resolve_platform), grp_rows in groups.items():
        campaign_names_checked.add(campaign_name)
        ctx = CampaignContext(
            source_platform=resolve_platform,
            campaign_platform_id=None,
            campaign_name_raw=campaign_name,
            campaign_name_normalized=normalize_campaign_name(campaign_name),
        )
        target = resolve_workspace(conn, org_id, ctx)
        if target is None:
            continue
        mism_rows = [r for r in grp_rows if r["ws"] != target.workspace_id]
        if mism_rows:
            mismatch_subgroups.append((campaign_name, target, mism_rows))

    if limit and limit > 0:
        mismatch_subgroups = mismatch_subgroups[:limit]

    mismatched_campaigns: set = set()
    move_ops: list = []  # (wle_id, lead_id, old_ws, target_ws)
    move_report: dict = {}  # (campaign_name, old_ws, target_ws) -> report dict
    for campaign_name, target, mism_rows in mismatch_subgroups:
        mismatched_campaigns.add(campaign_name)
        for r in mism_rows:
            move_ops.append((r["wle_id"], r["lead_id"], r["ws"], target.workspace_id))
            key = (campaign_name, r["ws"], target.workspace_id)
            rep = move_report.get(key)
            if rep is None:
                rep = {
                    "campaign_name": campaign_name,
                    "from_workspace": r["ws"],
                    "to_workspace": target.workspace_id,
                    "matched_rule": target.match_strategy,
                    "matched_map_id": target.map_id,
                    "event_count": 0,
                    "_leads": set(),
                }
                move_report[key] = rep
            rep["event_count"] += 1
            rep["_leads"].add(r["lead_id"])

    moved_lead_ids = {op[1] for op in move_ops}

    if not dry_run and move_ops:
        lead_targets: dict = {}  # (lead_id, old_ws) -> set(target_ws)
        for wle_id, lead_id, old_ws, target_ws in move_ops:
            conn.execute(
                "UPDATE workspace_lead_events SET workspace_id = ? WHERE id = ?",
                (target_ws, wle_id),
            )
            lead_targets.setdefault((lead_id, old_ws), set()).add(target_ws)
        for (lead_id, old_ws), targets in lead_targets.items():
            remaining = conn.execute(
                "SELECT COUNT(*) FROM workspace_lead_events WHERE lead_id = ? AND workspace_id = ?",
                (lead_id, old_ws),
            ).fetchone()[0]
            targets_list = sorted(targets)
            old_status_row = conn.execute(
                "SELECT status FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
                (old_ws, lead_id),
            ).fetchone()
            old_status = old_status_row["status"] if old_status_row else "prospecting"
            if remaining == 0:
                _move_workspace_lead_row(conn, org_id, lead_id, old_ws, targets_list[0])
                for extra in targets_list[1:]:
                    upsert_workspace_lead(conn, org_id, extra, lead_id, status=old_status)
            else:
                for t in targets_list:
                    upsert_workspace_lead(conn, org_id, t, lead_id, status=old_status)

    moves = []
    for rep in move_report.values():
        leads = sorted(rep.pop("_leads"))
        rep["lead_count"] = len(leads)
        rep["sample_lead_ids"] = leads[:5]
        moves.append(rep)

    event_count = len(move_ops)
    lead_count = len(moved_lead_ids)
    return {
        "dry_run": dry_run,
        "campaign_groups_checked": len(campaign_names_checked),
        "mismatched_groups": len(mismatched_campaigns),
        "skipped_no_campaign": skipped_no_campaign,
        "events_would_move": event_count,
        "leads_would_move": lead_count,
        "events_moved": 0 if dry_run else event_count,
        "leads_moved": 0 if dry_run else lead_count,
        "moves": moves,
    }

