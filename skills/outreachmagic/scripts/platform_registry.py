"""
Single source of truth for platform metadata and vendor event → canonical mappings.

Agents: run `pipeline.py platform-map --json` to discover all mappings.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# ── Extractor specs (field paths in webhook raw payloads) ─────────────────

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
        "body": (
            "eventData.body", "eventData.content", "content", "body", "message",
        ),
        "campaign": (
            "eventData.campaignName", "campaignName", "eventData.campaign", "campaign",
        ),
        "campaign_id": ("eventData.campaignId", "campaignId"),
        "campaign_name": (
            "eventData.campaignName", "campaignName", "eventData.campaign", "campaign",
        ),
    },
    "signals": {
        "status": ("eventType", "event_type", "eventData.status"),
        "webhook_event": ("eventType", "event_type"),
    },
    "identity": {
        "email": ("eventData.profileInfo.email", "profileInfo.email", "email"),
        "linkedin_url": (
            "eventData.lead", "lead", "eventData.profileInfo.linkedinUrl",
            "profileInfo.linkedinUrl", "linkedinUrl",
        ),
    },
}

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
            "data.bounce.reason", "data.bounce.message", "data.bounce.error",
            "bounce_reason", "reason", "message", "body",
        ),
        "bounce_type": ("data.bounce.type", "bounce_type", "type"),
        "recipient_mx": ("data.lead.mx_provider",),
    },
}

_DEFAULT_BOUNCE_SPEC = {
    "message": ("bounce_reason", "reason", "error", "msg", "message", "body"),
    "bounce_type": ("bounce_type", "type"),
}

# Shared cross-platform connection aliases
CONNECTION_SENT_TYPES = frozenset({
    "send_connection", "linkedin_connection_sent", "linkedin_connect",
    "connection_request_sent",
})
CONNECTION_ACCEPTED_TYPES = frozenset({
    "linkedin_connection_accepted", "connection_request_accepted",
    "connection_accepted", "linkedin_invite_accepted", "accept_invite",
})

# Vendor types that mean outbound LinkedIn DM/InMail (legacy rows + cross-platform stats)
LINKEDIN_MESSAGE_SENT_VENDOR = frozenset({
    "send_msg", "message_sent", "inmail_sent",
})

# Vendor types that mean inbound LinkedIn reply (Prosp may send wrong direction)
LINKEDIN_MESSAGE_REPLY_VENDOR = frozenset({
    "has_msg_replied", "has_msg_reply", "message_reply_received",
    "every_message_reply_received", "inmail_reply_received",
})

# PlusVibe vendor event sets (also referenced by ingest hooks)
PLUSVIBE_REPLY_EVENTS = frozenset({
    "all_email_replies", "first_email_replies", "all_positive_replies",
})
PLUSVIBE_SENT_EVENTS = frozenset({"email_sent"})
PLUSVIBE_BOUNCE_EVENTS = frozenset({"bounced_email"})
PLUSVIBE_INTERESTED_STAGE_EVENTS = frozenset({
    "lead_marked_as_interested",
    "lead_marked_as_meeting_booked",
    "lead_marked_as_meeting_completed",
    "lead_marked_as_qc_interested",
    "lead_marked_as_qc_crm_only",
})
PLUSVIBE_LOST_STAGE_EVENTS = frozenset({
    "lead_marked_as_not_interested",
    "lead_marked_as_wrong_person",
    "lead_marked_as_closed",
})
PLUSVIBE_STATUS_EVENTS = frozenset({
    "lead_marked_as_interested",
    "lead_marked_as_not_interested",
    "lead_marked_as_out_of_office",
    "lead_marked_as_automatic_reply",
    "lead_marked_as_meeting_booked",
    "lead_marked_as_meeting_completed",
    "lead_marked_as_wrong_person",
    "lead_marked_as_closed",
    "lead_marked_as_qc_interested",
    "lead_marked_as_qc_crm_only",
})

# Generic email event aliases (all non-PlusVibe platforms)
_GENERIC_EMAIL_MAP: dict[str, tuple[str, str, Optional[str], str]] = {
    "email_sent": ("email_sent", "outbound", "contacted", "email_sent"),
    "email_open": ("email_open", "inbound", None, "email_open"),
    "email_reply": ("email_reply", "inbound", "replied", "email_reply"),
    "email_bounce": ("email_bounce", "outbound", None, "email_bounce"),
    "email_bounced": ("email_bounce", "outbound", None, "email_bounce"),
    "email.bounced": ("email_bounce", "outbound", None, "email_bounce"),
    "email_click": ("email_click", "inbound", None, "email_click"),
    "email_unsubscribe": ("email_unsubscribe", "inbound", None, "email_unsubscribe"),
    "linkedin_connect": ("linkedin_connect", "outbound", "contacted", "linkedin_connection_sent"),
    "linkedin_connection_accepted": (
        "linkedin_connection_accepted", "inbound", None, "linkedin_connection_accepted",
    ),
    "linkedin_message": ("linkedin_message", "outbound", None, "linkedin_message_sent"),
    "linkedin_reply": ("linkedin_message", "inbound", "replied", "linkedin_message_reply"),
    "linkedin_message_sent": ("linkedin_message", "outbound", "contacted", "linkedin_message_sent"),
}


@dataclass(frozen=True)
class EventMapping:
    vendor_type: str
    local_type: str
    direction: str  # inbound | outbound | inherit
    stage: Optional[str] = None
    reporting_bucket: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlatformDef:
    id: str
    label: str
    channel: str  # email | linkedin
    category: str  # sequencer | enrichment
    setup_hint: str
    linkedin_platform: bool
    extractor_spec: dict[str, dict[str, tuple[str, ...]]]
    bounce_spec: Optional[dict[str, tuple[str, ...]]] = None
    event_mappings: tuple[EventMapping, ...] = ()
    plusvibe_style: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "channel": self.channel,
            "category": self.category,
            "setup_hint": self.setup_hint,
            "linkedin_platform": self.linkedin_platform,
            "plusvibe_style": self.plusvibe_style,
            "event_mappings": [m.to_dict() for m in self.event_mappings],
        }


def _em(
    vendor_type: str,
    local_type: str,
    direction: str,
    stage: Optional[str] = None,
    reporting_bucket: str = "",
    notes: str = "",
) -> EventMapping:
    return EventMapping(
        vendor_type=vendor_type,
        local_type=local_type,
        direction=direction,
        stage=stage,
        reporting_bucket=reporting_bucket or local_type,
        notes=notes,
    )


def _generic_email_mappings() -> tuple[EventMapping, ...]:
    return tuple(
        _em(v, lt, d, stage=st, reporting_bucket=rb)
        for v, (lt, d, st, rb) in _GENERIC_EMAIL_MAP.items()
    )


def _prosp_mappings() -> tuple[EventMapping, ...]:
    return (
        _em("has_msg_replied", "linkedin_message", "inbound", "replied", "linkedin_message_reply",
            "Prosp marks replies outbound in webhook; force inbound"),
        _em("has_msg_reply", "linkedin_message", "inbound", "replied", "linkedin_message_reply",
            "Alias for has_msg_replied"),
        _em("send_connection", "linkedin_connect", "outbound", "contacted", "linkedin_connection_sent"),
        _em("send_msg", "linkedin_message", "outbound", "contacted", "linkedin_message_sent",
            "Prosp outbound LinkedIn DM sent"),
        _em("linkedin_reply", "linkedin_message", "inbound", "replied", "linkedin_message_reply"),
        _em("linkedin_message", "linkedin_message", "outbound", None, "linkedin_message_sent"),
        _em("accept_invite", "linkedin_connection_accepted", "inbound", None,
            "linkedin_connection_accepted", "Prosp connection invite accepted"),
        _em("linkedin_connection_accepted", "linkedin_connection_accepted", "inbound", None,
            "linkedin_connection_accepted"),
    )


def _heyreach_linkedin_mappings() -> tuple[EventMapping, ...]:
    """HeyReach webhook event types (snake_case) → canonical LinkedIn local types."""
    return (
        _em("connection_request_sent", "linkedin_connect", "outbound", "contacted",
            "linkedin_connection_sent"),
        _em("connection_request_accepted", "linkedin_connection_accepted", "inbound", None,
            "linkedin_connection_accepted"),
        _em("send_connection", "linkedin_connect", "outbound", "contacted", "linkedin_connection_sent"),
        _em("message_sent", "linkedin_message", "outbound", "contacted", "linkedin_message_sent"),
        _em("inmail_sent", "linkedin_message", "outbound", "contacted", "linkedin_message_sent",
            "InMail stored as linkedin_message; see metadata.linkedin_message_kind"),
        _em("message_reply_received", "linkedin_message", "inbound", "replied", "linkedin_message_reply"),
        _em("every_message_reply_received", "linkedin_message", "inbound", "replied",
            "linkedin_message_reply", "Includes non-campaign replies when tracking enabled"),
        _em("inmail_reply_received", "linkedin_message", "inbound", "replied", "linkedin_message_reply"),
    )


def _heyreach_mappings() -> tuple[EventMapping, ...]:
    return _generic_email_mappings() + _heyreach_linkedin_mappings()


def _plusvibe_mappings() -> tuple[EventMapping, ...]:
    return (
        _em("all_email_replies", "email_reply", "inbound", "replied", "email_reply"),
        _em("first_email_replies", "email_reply", "inbound", "replied", "email_reply",
            notes="Subset of all_email_replies — do not enable both"),
        _em("all_positive_replies", "email_reply", "inbound", "replied", "email_reply",
            notes="Always co-fires with all_email_replies — do not enable"),
        _em("email_sent", "email_sent", "outbound", "contacted", "email_sent"),
        _em("bounced_email", "email_bounce", "outbound", None, "email_bounce"),
        _em("lead_marked_as_meeting_booked", "meeting_booked", "inbound", "interested", "meeting_booked"),
        _em("lead_marked_as_meeting_completed", "meeting_completed", "inbound", "interested", "meeting_completed"),
        _em("lead_marked_as_qc_interested", "lead_status_updated", "inbound", "interested", "lead_status_updated",
            notes="Treat same as lead_marked_as_interested"),
        _em("lead_marked_as_qc_crm_only", "lead_disposition", "inbound", "interested", "lead_disposition"),
        _em("lead_marked_as_wrong_person", "lead_status_updated", "inbound", "lost", "lead_status_updated"),
        _em("lead_marked_as_closed", "lead_status_updated", "inbound", "lost", "lead_status_updated"),
    )


PLATFORMS: dict[str, PlatformDef] = {
    "prosp": PlatformDef(
        id="prosp",
        label="Prosp",
        channel="linkedin",
        category="sequencer",
        setup_hint="In Prosp → Settings → Webhooks, paste the URL.",
        linkedin_platform=True,
        extractor_spec=_PROSP_SPEC,
        event_mappings=_prosp_mappings(),
    ),
    "heyreach": PlatformDef(
        id="heyreach",
        label="HeyReach",
        channel="linkedin",
        category="sequencer",
        setup_hint="In HeyReach → Settings → Webhooks, paste the URL. Enable all campaign events.",
        linkedin_platform=True,
        extractor_spec={
            "lead": _DEFAULT_SPEC["lead"],
            "event": _DEFAULT_SPEC["event"],
            "signals": _DEFAULT_SPEC["signals"],
            "identity": _HEYREACH_IDENTITY,
        },
        event_mappings=_heyreach_mappings(),
    ),
    "plusvibe": PlatformDef(
        id="plusvibe",
        label="PlusVibe",
        channel="email",
        category="sequencer",
        setup_hint=(
            "In PlusVibe → Settings → Webhooks, paste the URL. Enable: EMAIL_SENT, ALL_EMAIL_REPLIES, "
            "LEAD_MARKED_AS_INTERESTED, LEAD_MARKED_AS_NOT_INTERESTED, LEAD_MARKED_AS_OUT_OF_OFFICE, "
            "LEAD_MARKED_AS_AUTOMATIC_REPLY, BOUNCED_EMAIL, LEAD_MARKED_AS_MEETING_BOOKED, "
            "LEAD_MARKED_AS_MEETING_COMPLETED, LEAD_MARKED_AS_WRONG_PERSON, LEAD_MARKED_AS_CLOSED, "
            "LEAD_MARKED_AS_QC_INTERESTED, LEAD_MARKED_AS_QC_CRM_ONLY. "
            "Do NOT enable ALL_POSITIVE_REPLIES (duplicates ALL_EMAIL_REPLIES) or FIRST_EMAIL_REPLIES. "
            "Leave 'Skip out of office replies' and 'Skip autoreplies' unchecked."
        ),
        linkedin_platform=False,
        extractor_spec={**_PLUSVIBE_SPEC, "identity": _IDENTITY_DEFAULT},
        bounce_spec=PLATFORM_BOUNCE_SPECS["plusvibe"],
        event_mappings=_plusvibe_mappings(),
        plusvibe_style=True,
    ),
    "smartlead": PlatformDef(
        id="smartlead",
        label="Smartlead",
        channel="email",
        category="sequencer",
        setup_hint=(
            "In Smartlead → Settings → Webhooks, paste the URL. "
            "Enable: Email Sent, Email Reply, Email Bounced."
        ),
        linkedin_platform=False,
        extractor_spec=_DEFAULT_SPEC,
        bounce_spec=PLATFORM_BOUNCE_SPECS["smartlead"],
        event_mappings=_generic_email_mappings(),
    ),
    "instantly": PlatformDef(
        id="instantly",
        label="Instantly",
        channel="email",
        category="sequencer",
        setup_hint="In Instantly → Settings → Integrations → Webhooks, paste the URL. Enable all event types.",
        linkedin_platform=False,
        extractor_spec=_DEFAULT_SPEC,
        bounce_spec=PLATFORM_BOUNCE_SPECS["instantly"],
        event_mappings=_generic_email_mappings(),
    ),
    "emailbison": PlatformDef(
        id="emailbison",
        label="EmailBison",
        channel="email",
        category="sequencer",
        setup_hint="In EmailBison → Integrations → Webhooks, paste the URL and enable relevant events.",
        linkedin_platform=False,
        extractor_spec=_DEFAULT_SPEC,
        bounce_spec=PLATFORM_BOUNCE_SPECS["emailbison"],
        event_mappings=_generic_email_mappings(),
    ),
    "masterinbox": PlatformDef(
        id="masterinbox",
        label="MasterInbox",
        channel="email",
        category="sequencer",
        setup_hint="In MasterInbox → Settings → Webhooks, paste the URL.",
        linkedin_platform=False,
        extractor_spec=_DEFAULT_SPEC,
        event_mappings=_generic_email_mappings(),
    ),
    "clay": PlatformDef(
        id="clay",
        label="Clay",
        channel="email",
        category="enrichment",
        setup_hint="In Clay → Settings → Webhooks, paste the URL.",
        linkedin_platform=False,
        extractor_spec={
            "lead": _DEFAULT_SPEC["lead"],
            "event": _DEFAULT_SPEC["event"],
            "signals": _DEFAULT_SPEC["signals"],
            "identity": _IDENTITY_DEFAULT,
        },
        event_mappings=_generic_email_mappings(),
    ),
}

# Derived exports for pipeline CLI and extractors
PLATFORM_LABELS: dict[str, str] = {p.id: p.label for p in PLATFORMS.values()}
PLATFORM_SETUP_HINTS: dict[str, str] = {p.id: p.setup_hint for p in PLATFORMS.values()}
LINKEDIN_PLATFORMS: frozenset[str] = frozenset(p.id for p in PLATFORMS.values() if p.linkedin_platform)
CHANNEL_BY_PLATFORM: dict[str, str] = {p.id: p.channel for p in PLATFORMS.values()}

# Extractor registry for relay_extractors delegation
PLATFORM_SPECS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    p.id: p.extractor_spec for p in PLATFORMS.values()
}


def get_platform(platform_id: str) -> Optional[PlatformDef]:
    return PLATFORMS.get((platform_id or "").strip().lower())


def list_platforms(platform_id: Optional[str] = None) -> list[PlatformDef]:
    if platform_id:
        p = get_platform(platform_id)
        return [p] if p else []
    return list(PLATFORMS.values())


def platform_map_json(platform_id: Optional[str] = None) -> dict[str, Any]:
    platforms = list_platforms(platform_id)
    if platform_id and not platforms:
        return {"error": f"unknown platform: {platform_id}", "platforms": []}
    return {
        "platforms": [p.to_dict() for p in platforms],
        "connection_sent_types": sorted(CONNECTION_SENT_TYPES),
        "connection_accepted_types": sorted(CONNECTION_ACCEPTED_TYPES),
        "reply_vendor_types": sorted(reply_vendor_types()),
        "reply_local_types": sorted(reply_local_types()),
    }


def _mapping_index(platform: str) -> dict[str, EventMapping]:
    pdef = get_platform(platform)
    if not pdef:
        generic = {m.vendor_type: m for m in _generic_email_mappings()}
        for vt in CONNECTION_SENT_TYPES:
            generic.setdefault(vt, _em(vt, "linkedin_connect", "outbound", "contacted", "linkedin_connection_sent"))
        for vt in CONNECTION_ACCEPTED_TYPES:
            generic.setdefault(
                vt, _em(vt, "linkedin_connection_accepted", "inbound", None, "linkedin_connection_accepted"),
            )
        return generic
    return {m.vendor_type: m for m in pdef.event_mappings}


@dataclass
class ResolvedEvent:
    local_type: str
    direction: str
    target_stage: Optional[str]
    reporting_bucket: str
    vendor_type: str


def map_connection_event_type(envelope_event_type: str) -> str:
    """Map vendor webhook labels to local event_type for connection events."""
    et = (envelope_event_type or "unknown").strip().lower()
    if et in CONNECTION_SENT_TYPES:
        return "linkedin_connect"
    if et in CONNECTION_ACCEPTED_TYPES:
        return "linkedin_connection_accepted"
    return et


def resolve_event(platform: str, vendor_type: str, raw: Optional[dict] = None) -> ResolvedEvent:
    """Resolve vendor event_type to canonical local type, direction, and stage."""
    plat = (platform or "").strip().lower()
    et = (vendor_type or "unknown").strip().lower()
    raw = raw or {}
    pdef = get_platform(plat)

    if pdef and pdef.plusvibe_style:
        label = (raw.get("label") or "").strip().lower()
        if et in PLUSVIBE_REPLY_EVENTS:
            return ResolvedEvent("email_reply", "inbound", "replied", "email_reply", et)
        if et in PLUSVIBE_SENT_EVENTS:
            return ResolvedEvent("email_sent", "outbound", "contacted", "email_sent", et)
        if et in PLUSVIBE_BOUNCE_EVENTS:
            return ResolvedEvent("email_bounce", "outbound", None, "email_bounce", et)
        if et == "lead_marked_as_meeting_booked":
            return ResolvedEvent("meeting_booked", "inbound", "interested", "meeting_booked", et)
        if et == "lead_marked_as_meeting_completed":
            return ResolvedEvent("meeting_completed", "inbound", "interested", "meeting_completed", et)
        if et == "lead_marked_as_qc_interested":
            return ResolvedEvent("lead_status_updated", "inbound", "interested", "lead_status_updated", et)
        if et == "lead_marked_as_qc_crm_only":
            return ResolvedEvent("lead_disposition", "inbound", "interested", "lead_disposition", et)
        if et in PLUSVIBE_LOST_STAGE_EVENTS:
            return ResolvedEvent("lead_status_updated", "inbound", "lost", "lead_status_updated", et)
        if et in PLUSVIBE_INTERESTED_STAGE_EVENTS:
            return ResolvedEvent("lead_status_updated", "inbound", "interested", "lead_status_updated", et)
        if et.startswith("lead_marked_as_") or et.startswith("marked_as_"):
            return ResolvedEvent("lead_status_updated", "inbound", None, "lead_status_updated", et)
        if label in ("interested", "not_interested", "out_of_office"):
            return ResolvedEvent("lead_status_updated", "inbound", None, "lead_status_updated", et)
        direction = "inbound" if raw.get("direction", "").upper() == "IN" else "outbound"
        return ResolvedEvent(et, direction, None, et, et)

    idx = _mapping_index(plat)
    if et in idx:
        m = idx[et]
        return ResolvedEvent(m.local_type, m.direction, m.stage, m.reporting_bucket, et)

    local_type = map_connection_event_type(et)
    from bounces import is_bounce_event_type  # noqa: PLC0415 — avoid import cycle
    if is_bounce_event_type(et):
        local_type = "email_bounce"
        return ResolvedEvent(local_type, "outbound", None, "email_bounce", et)

    # Generic fallbacks for unregistered platforms
    generic = _mapping_index("")
    if et in generic:
        m = generic[et]
        return ResolvedEvent(m.local_type, m.direction, m.stage, m.reporting_bucket, et)

    direction = "inbound" if et in (
        "email_reply", "email_open", "email_click",
        "linkedin_connection_accepted", "linkedin_reply",
    ) or local_type == "linkedin_connection_accepted" else "outbound"

    stage = None
    if local_type in ("email_reply", "linkedin_message") and direction == "inbound":
        stage = "replied"
    elif local_type in ("email_sent", "linkedin_connect") and direction == "outbound":
        stage = "contacted"

    return ResolvedEvent(local_type, direction, stage, local_type, et)


def reply_vendor_types() -> frozenset[str]:
    """Vendor event_type strings that represent replies (including legacy stored types)."""
    types: set[str] = {"email_reply", "linkedin_reply", "has_msg_replied", "has_msg_reply"}
    for p in PLATFORMS.values():
        for m in p.event_mappings:
            if m.stage == "replied" or "reply" in m.reporting_bucket:
                types.add(m.vendor_type)
            if m.local_type in ("email_reply", "linkedin_message") and m.direction == "inbound":
                types.add(m.vendor_type)
    types.update(PLUSVIBE_REPLY_EVENTS)
    return frozenset(types)


def reply_local_types() -> frozenset[str]:
    return frozenset({"email_reply", "linkedin_message", "linkedin_reply"})


def is_reply_event(event_type: str, direction: str = "", channel: str = "") -> bool:
    """True if event counts as a reply (handles legacy vendor types and normalized types)."""
    et = (event_type or "").strip().lower()
    flow = (direction or "").strip().lower()
    if et in reply_vendor_types():
        if et in LINKEDIN_MESSAGE_REPLY_VENDOR:
            return True
        if et == "linkedin_message":
            return flow == "inbound"
        if et == "email":
            return flow == "inbound" and (channel or "").lower() == "email"
        return et.endswith("_reply") or et in PLUSVIBE_REPLY_EVENTS or "reply" in et
    if et == "linkedin_message" and flow == "inbound":
        return True
    if et == "email" and flow == "inbound" and (channel or "").lower() == "email":
        return True
    return False


def reply_event_sql_condition() -> str:
    """SQL WHERE fragment matching reply events (legacy + normalized)."""
    vendor = ", ".join(f"'{v}'" for v in sorted(reply_vendor_types()))
    local = ", ".join(f"'{v}'" for v in sorted(reply_local_types()))
    return (
        f"(lower(event_type) IN ({vendor}) "
        f"OR (lower(event_type) IN ({local}) AND lower(direction) = 'inbound') "
        f"OR (lower(direction) = 'inbound' AND lower(event_type) = 'email'))"
    )


def normalize_reporting_bucket(
    event_type: str,
    direction: str,
    channel: str,
    platform: str = "",
) -> str:
    """Map stored event to campaign reporting bucket."""
    et = (event_type or "unknown").strip().lower()
    flow = (direction or "").strip().lower()
    medium = (channel or "").strip().lower()
    plat = (platform or "").strip().lower()

    resolved = resolve_event(plat, et)
    if resolved.reporting_bucket and resolved.reporting_bucket != et:
        if resolved.local_type == "linkedin_message":
            if flow == "inbound" or resolved.direction == "inbound":
                return "linkedin_message_reply"
            return "linkedin_message_sent"
        return resolved.reporting_bucket

    if medium == "linkedin":
        if et in CONNECTION_SENT_TYPES or et == "linkedin_connect":
            return "linkedin_connection_sent"
        if et == "linkedin_connection_accepted":
            return "linkedin_connection_accepted"
        if et == "linkedin_reply":
            return "linkedin_message_reply"
        if et == "linkedin_message_sent":
            return "linkedin_message_sent"
        if et == "linkedin_message":
            return "linkedin_message_reply" if flow == "inbound" else "linkedin_message_sent"
        if et in LINKEDIN_MESSAGE_REPLY_VENDOR:
            return "linkedin_message_reply"
        if et in LINKEDIN_MESSAGE_SENT_VENDOR:
            return "linkedin_message_sent"
        if et in CONNECTION_ACCEPTED_TYPES:
            return "linkedin_connection_accepted"
    return et or "unknown"


def classify_activity_flags(event_type: str, direction: str, channel: str) -> dict[str, bool]:
    """Return email_sent / linkedin_sent / reply flags for activity materialization."""
    et = (event_type or "").strip().lower()
    flow = (direction or "").strip().lower()
    medium = (channel or "").strip().lower()
    normalized = normalize_reporting_bucket(event_type, direction, channel)

    if is_reply_event(et, flow, medium):
        return {"email_sent": False, "linkedin_sent": False, "reply": True}
    if flow == "inbound":
        return {"email_sent": False, "linkedin_sent": False, "reply": False}
    if medium == "email" and et in ("email_sent",):
        return {"email_sent": True, "linkedin_sent": False, "reply": False}
    if medium == "linkedin":
        if normalized in ("linkedin_message_sent", "linkedin_connection_sent"):
            return {"email_sent": False, "linkedin_sent": True, "reply": False}
        if et in (
            "linkedin_message", "linkedin_connect", "send_connection",
            "linkedin_connection_sent", "send_msg", *LINKEDIN_MESSAGE_SENT_VENDOR,
        ):
            return {"email_sent": False, "linkedin_sent": True, "reply": False}
    return {"email_sent": False, "linkedin_sent": False, "reply": False}


_PROSP_REPLY_PREFIXES = ("A lead has replied", "A lead has reacted with")


def extract_reply_body(
    platform: str,
    local_type: str,
    raw: dict,
    metadata: dict,
    body_preview: str = "",
) -> str:
    """Best-effort reply body for agents (prefers metadata.body over body_preview)."""
    meta_body = (metadata.get("body") or "").strip()
    if meta_body and meta_body != "A lead has replied":
        body = meta_body
    else:
        body = meta_body or body_preview or ""

    plat = (platform or "").strip().lower()
    if plat == "prosp" and local_type == "linkedin_message":
        if not body or body.strip() == "A lead has replied":
            for path in ("eventData.body", "eventData.content", "body", "content"):
                parts = path.split(".")
                cur: Any = raw
                for part in parts:
                    if not isinstance(cur, dict):
                        cur = None
                        break
                    cur = cur.get(part)
                if cur and str(cur).strip():
                    body = str(cur).strip()
                    break
        for prefix in _PROSP_REPLY_PREFIXES:
            body = body.replace(prefix, "").strip()
        if body.startswith("Re:"):
            body = body[3:].strip()
    return body.strip()


_HTML_TAG_RE = re.compile(r"<[a-zA-Z!/]")


def looks_like_html(text: str) -> bool:
    """True when text likely contains HTML markup (not plain `<` in prose)."""
    if not text or "<" not in text or ">" not in text:
        return False
    return bool(_HTML_TAG_RE.search(text))


def strip_html_reply(text: str, max_len: int = 250) -> str:
    """Strip HTML tags and collapse whitespace. max_len=0 keeps full plain text."""
    import html as html_mod
    clean = text or ""
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html_mod.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_len] if max_len else clean


def normalize_event_body_for_storage(text: str) -> tuple[str, bool]:
    """Convert HTML bodies to plain text before SQLite storage. Returns (text, was_html)."""
    if not text:
        return "", False
    if not looks_like_html(text):
        return text, False
    return strip_html_reply(text, max_len=0), True
