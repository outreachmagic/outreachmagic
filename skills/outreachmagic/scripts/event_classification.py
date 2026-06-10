"""Shared event type normalization for campaign stats and activity counts."""

from __future__ import annotations

from dataclasses import dataclass

from platform_registry import classify_activity_flags, normalize_reporting_bucket


@dataclass(frozen=True)
class EventActivityFlags:
    email_sent: bool = False
    linkedin_sent: bool = False
    reply: bool = False


def normalize_campaign_event_type(event_type: str, direction: str, channel: str) -> str:
    """Map raw event labels to reporting-friendly campaign event buckets."""
    return normalize_reporting_bucket(event_type, direction, channel)


def classify_event_for_activity(event_type: str, direction: str, channel: str) -> EventActivityFlags:
    """Return activity count flags for one timeline event."""
    flags = classify_activity_flags(event_type, direction, channel)
    return EventActivityFlags(
        email_sent=flags["email_sent"],
        linkedin_sent=flags["linkedin_sent"],
        reply=flags["reply"],
    )
