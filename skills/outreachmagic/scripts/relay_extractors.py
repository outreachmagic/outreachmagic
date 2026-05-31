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
LEAD_KEYS = ("first_name", "last_name", "job_title", "industry", "company_name", "headcount")
EVENT_KEYS = ("subject", "body", "campaign", "campaign_id", "campaign_name")
SIGNAL_KEYS = ("label", "sentiment", "status", "webhook_event")


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
#   lead_email, subject, body, campaign_name, webhook_event, label, sentiment, ...
# Reply payloads use text_body; label webhooks use last_lead_reply / last_lead_reply_subject.
_PLUSVIBE_SPEC = {
    "lead": {
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "job_title": ("job_title",),
        "industry": ("industry",),
        "company_name": ("company_name",),
        "headcount": ("headcount", "company_headcount", "employee_count"),
    },
    "event": {
        "subject": ("subject", "last_lead_reply_subject", "latest_subject"),
        "body": ("body", "text_body", "last_lead_reply", "snippet"),
        "campaign": ("campaign_name", "campaign"),
        "campaign_id": ("campaign_id", "data.campaign_id", "campaign.id"),
        "campaign_name": ("campaign_name", "campaign"),
    },
    "signals": {
        "label": ("label",),
        "sentiment": ("sentiment",),
        "status": ("status",),
        "webhook_event": ("webhook_event",),
    },
}

_PROSP_SPEC = {
    "lead": {
        "first_name": ("eventData.profileInfo.firstName", "profileInfo.firstName", "firstName"),
        "last_name": ("eventData.profileInfo.lastName", "profileInfo.lastName", "lastName"),
        "job_title": ("eventData.profileInfo.jobTitle", "profileInfo.jobTitle", "jobTitle"),
        "industry": ("eventData.profileInfo.industry", "profileInfo.industry", "industry"),
        "company_name": ("eventData.profileInfo.company", "profileInfo.company", "company"),
        "headcount": ("eventData.profileInfo.headcount", "profileInfo.headcount", "headcount"),
    },
    "event": {
        "subject": ("eventData.subject", "subject"),
        "body": ("eventData.content", "content", "body", "message"),
        "campaign": (
            "eventData.campaignName",
            "campaignName",
            "eventData.campaign",
            "campaign",
        ),
        "campaign_id": ("eventData.campaignId", "campaignId"),
        "campaign_name": (
            "eventData.campaignName",
            "campaignName",
            "eventData.campaign",
            "campaign",
        ),
    },
    "signals": {
        "status": ("eventType", "event_type", "eventData.status"),
        "webhook_event": ("eventType", "event_type"),
    },
    "identity": {
        "email": (
            "eventData.profileInfo.email",
            "profileInfo.email",
            "email",
        ),
        "linkedin_url": (
            "eventData.lead",
            "lead",
            "eventData.profileInfo.linkedinUrl",
            "profileInfo.linkedinUrl",
            "linkedinUrl",
        ),
    },
}

_IDENTITY_DEFAULT = {
    "email": (
        "lead_email", "from_email", "email", "lead.email_address",
        "data.lead.email", "to_email", "sl_lead_email",
    ),
    "linkedin_url": (
        "linkedin_url", "lead_linkedin_url", "lead.profile_url",
        "linkedin", "profile_url",
    ),
}

_HEYREACH_IDENTITY = {
    "email": ("lead.email_address", "lead.email"),
    "linkedin_url": ("lead.profile_url", "lead.linkedin_url"),
}

# Fallback for platforms not yet mapped — tries common keys across vendors
_DEFAULT_SPEC = {
    "lead": {
        "first_name": ("first_name", "lead.first_name", "data.lead.first_name"),
        "last_name": ("last_name", "lead.last_name", "data.lead.last_name"),
        "job_title": ("job_title", "title", "lead.title", "data.lead.job_title"),
        "industry": ("industry", "lead.industry", "data.lead.industry"),
        "company_name": ("company_name", "company", "lead.company", "data.lead.company_name"),
        "headcount": ("headcount", "company_headcount", "employee_count", "lead.headcount"),
    },
    "event": {
        "subject": ("subject", "email_subject", "data.subject"),
        "body": ("body", "email_body", "message", "data.body", "text_body"),
        "campaign": ("campaign_name", "campaign"),
        "campaign_id": ("campaign_id", "data.campaign_id", "campaign.id"),
        "campaign_name": ("campaign_name", "campaign", "data.campaign_name"),
    },
    "signals": {
        "label": ("label", "lead_status", "data.label"),
        "sentiment": ("sentiment", "lead_sentiment"),
        "status": ("status",),
        "webhook_event": ("webhook_event", "event_type"),
    },
    "identity": _IDENTITY_DEFAULT,
}

# Registry: platform id -> {lead, event, signals?, identity?}
PLATFORM_SPECS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "plusvibe": {**_PLUSVIBE_SPEC, "identity": _IDENTITY_DEFAULT},
    "prosp": _PROSP_SPEC,
    "heyreach": {
        "lead": _DEFAULT_SPEC["lead"],
        "event": _DEFAULT_SPEC["event"],
        "signals": _DEFAULT_SPEC["signals"],
        "identity": _HEYREACH_IDENTITY,
    },
    "clay": {
        "lead": _DEFAULT_SPEC["lead"],
        "event": _DEFAULT_SPEC["event"],
        "signals": _DEFAULT_SPEC["signals"],
        "identity": _IDENTITY_DEFAULT,
    },
}


def extract_relay_fields(platform: str, raw: dict | None) -> dict[str, dict[str, str]]:
    """Return {lead, event, signals, identity} with canonical string fields."""
    if not raw or not isinstance(raw, dict):
        return {"lead": {}, "event": {}, "signals": {}, "identity": {}}
    spec = PLATFORM_SPECS.get(platform, _DEFAULT_SPEC)
    signals_spec = spec.get("signals", _DEFAULT_SPEC.get("signals", {}))
    identity_spec = spec.get("identity", _IDENTITY_DEFAULT)
    return {
        "lead": _extract_block(raw, spec["lead"]),
        "event": _extract_block(raw, spec["event"]),
        "signals": _extract_block(raw, signals_spec) if signals_spec else {},
        "identity": _extract_block(raw, identity_spec),
    }


def extract_relay_identity(
    platform: str, raw: dict | None, envelope_lead: str = ""
) -> dict[str, str]:
    """Resolve email and LinkedIn from raw payload and relay envelope lead field."""
    fields = extract_relay_fields(platform, raw)
    identity = dict(fields.get("identity") or {})
    env = (envelope_lead or "").strip()
    if env:
        if "@" in env:
            identity.setdefault("email", env)
        else:
            identity.setdefault("linkedin_url", env)
    return identity


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


# Bounce diagnostic fields per platform (first non-empty dot-path wins).
PLATFORM_BOUNCE_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "plusvibe": {
        "message": ("msg", "body", "bounce_reason", "reason", "error"),
        "bounce_type": ("bounce_type", "type"),
        "recipient_mx": ("lead_mx",),
        "sender_mx": ("sender_mx",),
    },
    "smartlead": {
        "message": ("bounce_reason", "reason", "error", "msg", "message"),
        "bounce_type": ("bounce_type", "type"),
    },
    "instantly": {
        "message": ("bounce_reason", "reason", "error_message", "message", "body", "msg"),
        "bounce_type": ("bounce_type", "type"),
    },
    "emailbison": {
        "message": (
            "data.bounce.reason",
            "data.bounce.message",
            "data.bounce.error",
            "bounce_reason",
            "reason",
            "message",
            "body",
        ),
        "bounce_type": ("data.bounce.type", "bounce_type", "type"),
        "recipient_mx": ("data.lead.mx_provider",),
    },
}

_DEFAULT_BOUNCE_SPEC = {
    "message": ("bounce_reason", "reason", "error", "msg", "message", "body"),
    "bounce_type": ("bounce_type", "type"),
}


def extract_bounce_fields(platform: str, raw: dict | None) -> dict[str, str]:
    """Extract bounce diagnostics from a relay webhook raw payload."""
    if not raw or not isinstance(raw, dict):
        return {}
    spec = PLATFORM_BOUNCE_SPECS.get(platform, _DEFAULT_BOUNCE_SPEC)
    return _extract_block(raw, spec)
