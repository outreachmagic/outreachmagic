"""
Platform-specific field extraction from relay event `raw` payloads.

Each platform maps canonical field names to one or more dot-paths in the webhook
body (first non-empty wins). Add new platforms by registering a spec in
PLATFORM_SPECS — no changes to ingest logic required.

Platforms without a spec use _DEFAULT_SPEC (generic key guessing only).
"""

from __future__ import annotations

from typing import Any, Optional

# Canonical keys returned by extract_relay_fields()
LEAD_KEYS = ("first_name", "last_name", "job_title", "industry", "company_name")
EVENT_KEYS = ("subject", "body", "campaign")


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _pick(data: dict, paths: tuple[str, ...]) -> Optional[str]:
    for path in paths:
        val = _get_path(data, path) if "." in path else data.get(path)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _extract_block(data: dict, spec: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, paths in spec.items():
        val = _pick(data, paths)
        if val:
            out[key] = val
    return out


# Plusvibe webhook shape (verified). Top-level flat fields on POST body, e.g.:
#   first_name, last_name, job_title, industry, company_name,
#   lead_email, subject, body, campaign_name, webhook_event, ...
# Smartlead, Instantly, etc. are not this shape — add a separate spec when we have samples.
_PLUSVIBE_SPEC = {
    "lead": {
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "job_title": ("job_title",),
        "industry": ("industry",),
        "company_name": ("company_name",),
    },
    "event": {
        "subject": ("subject",),
        "body": ("body",),
        "campaign": ("campaign_name", "campaign_id"),
    },
}

# Registry: platform id -> {lead: {canonical: (paths...)}, event: {...}}
PLATFORM_SPECS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "plusvibe": _PLUSVIBE_SPEC,
}

# Fallback for platforms not yet mapped — tries common keys across vendors
_DEFAULT_SPEC = {
    "lead": {
        "first_name": ("first_name", "lead.first_name", "data.lead.first_name"),
        "last_name": ("last_name", "lead.last_name", "data.lead.last_name"),
        "job_title": ("job_title", "title", "lead.title", "data.lead.job_title"),
        "industry": ("industry", "lead.industry", "data.lead.industry"),
        "company_name": ("company_name", "company", "lead.company", "data.lead.company_name"),
    },
    "event": {
        "subject": ("subject", "email_subject", "data.subject"),
        "body": ("body", "email_body", "message", "data.body"),
        "campaign": ("campaign_name", "campaign", "campaign_id", "data.campaign_name"),
    },
}


def extract_relay_fields(platform: str, raw: dict | None) -> dict[str, dict[str, str]]:
    """Return {lead: {...}, event: {...}} with canonical string fields."""
    if not raw or not isinstance(raw, dict):
        return {"lead": {}, "event": {}}
    spec = PLATFORM_SPECS.get(platform, _DEFAULT_SPEC)
    return {
        "lead": _extract_block(raw, spec["lead"]),
        "event": _extract_block(raw, spec["event"]),
    }


def build_display_name(lead: dict[str, str], email: Optional[str] = None) -> Optional[str]:
    first = lead.get("first_name")
    if not first:
        return None
    last = lead.get("last_name", "")
    return f"{first} {last}".strip() if last else first


def name_from_email(email: str) -> str:
    if not email or "@" not in email:
        return email or "Unknown"
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()
