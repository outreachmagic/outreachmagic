"""Shared event type normalization for campaign stats and activity counts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EventActivityFlags:
    email_sent: bool = False
    linkedin_sent: bool = False
    reply: bool = False


def normalize_campaign_event_type(event_type: str, direction: str, channel: str) -> str:
    """Map raw event labels to reporting-friendly campaign event buckets."""
    et = (event_type or "unknown").strip().lower()
    flow = (direction or "").strip().lower()
    medium = (channel or "").strip().lower()
    if medium == "linkedin":
        if et in ("send_connection", "linkedin_connect", "linkedin_connection_sent"):
            return "linkedin_connection_sent"
        if et == "linkedin_connection_accepted":
            return "linkedin_connection_accepted"
        if et == "linkedin_reply":
            return "linkedin_message_reply"
        if et == "linkedin_message_sent":
            return "linkedin_message_sent"
        if et == "linkedin_message":
            return "linkedin_message_reply" if flow == "inbound" else "linkedin_message_sent"
    return et or "unknown"


def classify_event_for_activity(event_type: str, direction: str, channel: str) -> EventActivityFlags:
    """Return activity count flags for one timeline event."""
    et = (event_type or "").strip().lower()
    flow = (direction or "").strip().lower()
    medium = (channel or "").strip().lower()
    normalized = normalize_campaign_event_type(event_type, direction, channel)
    if flow == "inbound":
        if normalized == "linkedin_message_reply" or et in ("email_reply", "linkedin_reply"):
            return EventActivityFlags(reply=True)
        if et == "linkedin_message" and medium == "linkedin":
            return EventActivityFlags(reply=True)
        return EventActivityFlags()
    if medium == "email" and et in ("email_sent",):
        return EventActivityFlags(email_sent=True)
    if medium == "linkedin":
        if normalized in ("linkedin_message_sent", "linkedin_connection_sent"):
            return EventActivityFlags(linkedin_sent=True)
        if et in ("linkedin_message", "linkedin_connect", "send_connection", "linkedin_connection_sent"):
            return EventActivityFlags(linkedin_sent=True)
    return EventActivityFlags()
