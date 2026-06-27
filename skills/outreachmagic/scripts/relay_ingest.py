"""Relay webhook ingest — maps vendor events to local SQLite via platform_registry."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from bounces import build_bounce_event_metadata
from constants import (
    AUTO_REPLY_LABELS,
    PLUSVIBE_BOUNCE_EVENTS,
    PLUSVIBE_PLATFORMS,
    PLUSVIBE_REPLY_EVENTS,
    PLUSVIBE_SENT_EVENTS,
)
from db_conn import get_conn
from platform_registry import (
    CHANNEL_BY_PLATFORM,
    PLUSVIBE_INTERESTED_STAGE_EVENTS,
    PLUSVIBE_LOST_STAGE_EVENTS,
    extract_reply_body,
    resolve_event,
)
from relay_extractors import (
    build_display_name,
    extract_relay_fields,
    extract_relay_identity,
    name_from_email,
)
from workspace_routing import DEFAULT_ORG_ID, extract_campaign_context


def normalize_lead_status_display(label: str) -> str:
    if not label:
        return ""
    return label.strip().lower().replace("_", " ")


def is_auto_reply_label(label: str) -> bool:
    normalized = (label or "").strip().lower().replace(" ", "_")
    return normalized in AUTO_REPLY_LABELS or "out_of_office" in normalized


def build_plusvibe_status_metadata(
    raw: dict,
    signals: dict,
    envelope_event_type: str,
) -> dict:
    """Normalized status fields stored on event metadata_json."""
    meta: dict = {}
    et = (envelope_event_type or "").lower()
    forced_label = ""
    for prefix in ("lead_marked_as_", "marked_as_"):
        if et.startswith(prefix):
            forced_label = et[len(prefix):]
            break
    if et == "bounced_email":
        forced_label = "email_bounced"

    payload_label = (signals.get("label") or raw.get("label") or "").strip().lower()
    label = forced_label or payload_label

    payload_sentiment = (signals.get("sentiment") or raw.get("sentiment") or "").strip().lower()
    sentiment = payload_sentiment
    if forced_label in ("interested", "qc_interested", "meeting_booked", "meeting_completed"):
        sentiment = "positive"
    elif et == "lead_marked_as_qc_crm_only":
        sentiment = "positive"
    elif forced_label in ("not_interested", "not interested", "wrong_person", "closed"):
        sentiment = "negative"
    if is_auto_reply_label(label):
        sentiment = "autoreply"
    if not sentiment and label == "email_bounced":
        sentiment = "invalid"

    if label:
        meta["lead_status_raw"] = label
        meta["lead_status_display"] = normalize_lead_status_display(label)
    if sentiment:
        meta["lead_status_sentiment"] = sentiment
    if signals.get("status"):
        meta["lead_status_platform_status"] = signals["status"].lower()
    if envelope_event_type:
        meta["plusvibe_webhook_event"] = envelope_event_type

    if is_auto_reply_label(label):
        meta["is_auto_reply"] = True
        meta["auto_reply_type"] = "ooo"

    return meta


def build_calendly_status_metadata(envelope_event_type: str) -> dict:
    """Status fields for Calendly webhook events (invitee.created / invitee.canceled)."""
    meta: dict = {}
    et = (envelope_event_type or "").lower()
    if et == "invitee.created":
        meta["lead_status_raw"] = "meeting_booked"
        meta["lead_status_display"] = "meeting booked"
        meta["lead_status_sentiment"] = "positive"
    elif et == "invitee.canceled":
        meta["lead_status_raw"] = "canceled"
        meta["lead_status_display"] = "canceled"
        meta["lead_status_sentiment"] = "negative"
    return meta


def _format_calendly_timestamp(iso_value: str) -> str:
    text = (iso_value or "").strip()
    if not text:
        return ""
    try:
        from datetime import datetime

        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ").lstrip()
    except ValueError:
        return text[:19].replace("T", " ")


def _calendly_location_label(location: dict) -> str:
    loc_type = (location.get("type") or "").strip().lower()
    if loc_type == "google_conference":
        return "Google Meet"
    if loc_type:
        return loc_type.replace("_", " ").title()
    return ""


def build_calendly_meeting_note(raw: dict, envelope_event_type: str = "") -> str:
    """Flat meeting summary for events.body — time, people, UTMs, form answers."""
    payload = raw.get("payload") or {}
    if not payload:
        return ""

    scheduled = payload.get("scheduled_event") or {}
    lines: list[str] = []

    event_name = (scheduled.get("name") or "").strip()
    if event_name:
        lines.append(event_name)

    et = (envelope_event_type or raw.get("event") or "").strip().lower()
    if et == "invitee.canceled":
        lines.append("Status: canceled")
    elif (payload.get("status") or scheduled.get("status") or "").strip():
        status = (payload.get("status") or scheduled.get("status") or "").strip()
        lines.append(f"Status: {status}")

    start = (scheduled.get("start_time") or "").strip()
    end = (scheduled.get("end_time") or "").strip()
    tz = (payload.get("timezone") or "").strip()
    if start:
        start_fmt = _format_calendly_timestamp(start)
        if end:
            end_fmt = _format_calendly_timestamp(end)
            window = f"{start_fmt} – {end_fmt}"
        else:
            window = start_fmt
        if tz:
            window = f"{window} ({tz})"
        lines.append(window)

    invitee_name = (payload.get("name") or "").strip()
    invitee_email = (payload.get("email") or "").strip()
    if invitee_name and invitee_email:
        lines.append(f"Invitee: {invitee_name} ({invitee_email})")
    elif invitee_email:
        lines.append(f"Invitee: {invitee_email}")
    elif invitee_name:
        lines.append(f"Invitee: {invitee_name}")

    hosts: list[str] = []
    for member in scheduled.get("event_memberships") or []:
        if not isinstance(member, dict):
            continue
        name = (member.get("user_name") or "").strip()
        email = (member.get("user_email") or "").strip()
        if name and email:
            hosts.append(f"{name} ({email})")
        elif name or email:
            hosts.append(name or email)
    if hosts:
        lines.append("Hosts: " + ", ".join(hosts))

    location = scheduled.get("location") or {}
    if isinstance(location, dict):
        label = _calendly_location_label(location)
        if label:
            lines.append(f"Location: {label}")

    tracking = payload.get("tracking") or {}
    utm_parts: list[str] = []
    for key, short in (
        ("utm_campaign", "campaign"),
        ("utm_source", "source"),
        ("utm_medium", "medium"),
        ("utm_content", "content"),
        ("utm_term", "term"),
    ):
        val = tracking.get(key)
        if val is not None and str(val).strip():
            utm_parts.append(f"{short}={str(val).strip()}")
    if utm_parts:
        lines.append("UTM: " + " · ".join(utm_parts))

    for qa in payload.get("questions_and_answers") or []:
        if not isinstance(qa, dict):
            continue
        question = (qa.get("question") or "").strip()
        answer = (qa.get("answer") or "").strip()
        if question and answer:
            lines.append(f"Question: {question} → {answer}")

    if payload.get("rescheduled"):
        lines.append("Rescheduled: yes")

    return "\n".join(lines)


def relay_target_stage(
    platform: str,
    envelope_event_type: str,
    local_type: str,
    raw: dict,
    metadata: dict,
    *,
    resolved_stage: Optional[str] = None,
) -> Optional[str]:
    """Pipeline stage to apply after ingest; None = leave stage unchanged."""
    if resolved_stage:
        return resolved_stage

    et = envelope_event_type.lower()
    label = (metadata.get("lead_status_raw") or raw.get("label") or "").lower()
    sentiment = (metadata.get("lead_status_sentiment") or "").lower()

    if platform in PLUSVIBE_PLATFORMS:
        if local_type == "email_bounce" or et in PLUSVIBE_BOUNCE_EVENTS or sentiment == "invalid":
            return None
        if metadata.get("is_auto_reply") or is_auto_reply_label(label):
            return None
        if (
            et in PLUSVIBE_LOST_STAGE_EVENTS
            or "not_interested" in et
            or label in ("not_interested", "not interested", "wrong_person", "closed")
            or (sentiment == "negative" and label not in ("out_of_office", "automatic_reply"))
        ):
            return "lost"
        if (
            et in PLUSVIBE_INTERESTED_STAGE_EVENTS
            or local_type in ("meeting_booked", "meeting_completed", "lead_disposition")
            or "interested" in et
            or label in ("interested", "qc_interested", "meeting_booked", "meeting_completed", "qc_crm_only")
            or sentiment == "positive"
        ):
            return "interested"
        if local_type == "email_reply" or et in PLUSVIBE_REPLY_EVENTS:
            return "replied"
        if local_type == "email_sent" or et in PLUSVIBE_SENT_EVENTS:
            return "contacted"
        return None

    return None


def relay_dedupe_key(event: dict) -> str:
    if event.get("relay_id"):
        return f"relay:{event['relay_id']}"
    raw = event.get("raw") or {}
    if event.get("platform") in PLUSVIBE_PLATFORMS and raw.get("webhook_id"):
        return f"pv:{raw['webhook_id']}"
    if raw.get("sent_email_id"):
        return f"sent:{raw['sent_email_id']}"
    if raw.get("message_id"):
        return f"msg:{raw['message_id']}"
    return (
        f"fp:{event.get('platform')}|{event.get('lead')}|{event.get('event_type')}"
        f"|{event.get('received_at')}"
    )


RELAY_INGESTED_PREFETCH_CHUNK = 500
PLUSVIBE_POSITIVE_REPLY_DEDUP_SECONDS = 60


def _plusvibe_body_fingerprint(body: str) -> str:
    normalized = (body or "").strip().lower()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def plusvibe_positive_reply_is_duplicate(
    conn: sqlite3.Connection,
    *,
    email: str,
    campaign: str,
    body: str,
    received_at: str,
    window_seconds: int = PLUSVIBE_POSITIVE_REPLY_DEDUP_SECONDS,
) -> bool:
    """Skip all_positive_replies when ALL_EMAIL_REPLIES already captured the same reply."""
    fingerprint = _plusvibe_body_fingerprint(body)
    if not email or not fingerprint:
        return False
    email_norm = email.strip().lower()
    campaign_norm = (campaign or "").strip()
    since = received_at
    if since:
        try:
            ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
            since = (ts - timedelta(seconds=window_seconds)).isoformat()
        except ValueError:
            since = None
    params: list = [email_norm]
    time_clause = ""
    if since:
        time_clause = " AND e.created_at >= ?"
        params.append(since)
    campaign_clause = ""
    if campaign_norm:
        campaign_clause = (
            " AND (json_extract(e.metadata_json, '$.campaign') = ?"
            " OR c.name = ?)"
        )
        params.extend([campaign_norm, campaign_norm])
    row = conn.execute(
        f"""SELECT 1
            FROM events e
            JOIN leads l ON l.id = e.lead_id
            LEFT JOIN campaigns c ON c.id = e.campaign_id
            WHERE lower(l.email) = ?
              AND e.event_type = 'email_reply'
              AND lower(json_extract(e.metadata_json, '$.platform')) = 'plusvibe'
              AND (
                json_extract(e.metadata_json, '$.plusvibe_positive_reply_skipped') IS NULL
                OR json_extract(e.metadata_json, '$.plusvibe_positive_reply_skipped') = 0
              )
              {time_clause}
              {campaign_clause}
              AND (
                substr(lower(coalesce(json_extract(e.metadata_json, '$.body'), '')), 1, 200)
                  = substr(lower(?), 1, 200)
                OR lower(coalesce(e.body_preview, '')) = substr(lower(?), 1, 200)
              )
            LIMIT 1""",
        [*params, body or "", body or ""],
    ).fetchone()
    return row is not None


def _skip_plusvibe_positive_reply_duplicate(
    dedupe_key: str,
    *,
    defer_mark: bool,
    pending_marks: Optional[list],
    pull_conn: Optional[sqlite3.Connection],
    own_conn: bool,
    conn: sqlite3.Connection,
    quiet: bool,
) -> None:
    if defer_mark and pending_marks is not None:
        pending_marks.append((dedupe_key, None))
    elif pull_conn is not None:
        pull_conn.execute(
            "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
            (dedupe_key, None),
        )
    else:
        mark_relay_ingested(dedupe_key, None)
    if own_conn:
        conn.close()
    if not quiet:
        print(
            "[relay] skipped plusvibe all_positive_replies duplicate "
            f"(dedupe_key={dedupe_key})",
            file=sys.stderr,
        )


def relay_already_ingested(dedupe_key: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM relay_ingested WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    conn.close()
    return row is not None


def prefetch_relay_ingested(
    dedupe_keys: list[str],
    conn: Optional[sqlite3.Connection] = None,
) -> set[str]:
    """Return which dedupe keys already exist (batched IN lookup for pull pages)."""
    if not dedupe_keys:
        return set()
    unique = list(dict.fromkeys(dedupe_keys))
    found: set[str] = set()
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        for i in range(0, len(unique), RELAY_INGESTED_PREFETCH_CHUNK):
            chunk = unique[i : i + RELAY_INGESTED_PREFETCH_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT dedupe_key FROM relay_ingested WHERE dedupe_key IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(row[0] for row in rows)
    finally:
        if own_conn and conn is not None:
            conn.close()
    return found


def prefetch_ws_idempotency_keys(
    conn: sqlite3.Connection,
    org_id: str,
    idempotency_keys: list[str],
) -> set[str]:
    """Return workspace_lead_events idempotency keys already stored for a pull page."""
    if not idempotency_keys:
        return set()
    unique = list(dict.fromkeys(idempotency_keys))
    found: set[str] = set()
    for i in range(0, len(unique), RELAY_INGESTED_PREFETCH_CHUNK):
        chunk = unique[i : i + RELAY_INGESTED_PREFETCH_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT idempotency_key FROM workspace_lead_events
                WHERE org_id = ? AND idempotency_key IN ({placeholders})""",
            [org_id, *chunk],
        ).fetchall()
        found.update(row[0] for row in rows)
    return found


def _coerce_relay_mark_lead_id(
    conn: sqlite3.Connection,
    lead_id: Optional[int],
) -> Optional[int]:
    """Ensure relay_ingested.lead_id satisfies FK (merged/deleted leads → keep_id or NULL)."""
    if lead_id is None:
        return None
    try:
        lid = int(lead_id)
    except (TypeError, ValueError):
        return None
    if conn.execute("SELECT 1 FROM leads WHERE id = ?", (lid,)).fetchone():
        return lid
    merged = conn.execute(
        """SELECT keep_id FROM lead_merges
           WHERE merge_id = ? ORDER BY merged_at DESC LIMIT 1""",
        (lid,),
    ).fetchone()
    if merged:
        keep_id = int(merged["keep_id"])
        if conn.execute("SELECT 1 FROM leads WHERE id = ?", (keep_id,)).fetchone():
            return keep_id
    return None


def mark_relay_ingested(dedupe_key: str, lead_id: Optional[int]) -> None:
    conn = get_conn()
    safe_lead_id = _coerce_relay_mark_lead_id(conn, lead_id)
    conn.execute(
        "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
        (dedupe_key, safe_lead_id),
    )
    conn.commit()
    conn.close()


def mark_relay_ingested_many(
    entries: list[tuple[str, Optional[int]]],
    conn: Optional[sqlite3.Connection] = None,
    *,
    commit: bool = True,
) -> None:
    """Batch-insert dedupe keys after a pull page (single commit)."""
    if not entries:
        return
    unique: list[tuple[str, Optional[int]]] = []
    seen: set[str] = set()
    for key, lead_id in entries:
        if key in seen:
            continue
        seen.add(key)
        unique.append((key, lead_id))
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        safe_rows = [
            (key, _coerce_relay_mark_lead_id(conn, lead_id))
            for key, lead_id in unique
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
            safe_rows,
        )
        if commit:
            conn.commit()
    finally:
        if own_conn and conn is not None:
            conn.close()


def _relay_event_timestamp(event: dict, normalize) -> Optional[str]:
    """Prefer relay/webhook time over import time (datetime('now') fallback in log_event)."""
    for key in ("received_at", "created_at", "timestamp"):
        val = event.get(key)
        if val:
            return normalize(val)
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    for key in ("timestamp", "created_at", "received_at", "event_time", "sent_at"):
        val = raw.get(key)
        if val:
            return normalize(val)
    return None


def ingest_relay_event(
    event: dict,
    debug_sentiment: bool = False,
    force_workspace_id: Optional[str] = None,
    quiet: bool = False,
    *,
    defer_mark: bool = False,
    pending_marks: Optional[list] = None,
    pull_conn: Optional[sqlite3.Connection] = None,
    routing_config: Optional[object] = None,
    ws_slug_map: Optional[dict[str, str]] = None,
    routing_cache: Optional[object] = None,
    ingested_prefetch: Optional[set[str]] = None,
    ws_idempotent_prefetch: Optional[set[str]] = None,
    defer_activity_refresh: bool = False,
    activity_refresh_pairs: Optional[set[tuple[int, str]]] = None,
) -> Optional[int]:
    """Take a relay event and write it to the local SQLite database. Returns None if duplicate."""
    import pipeline as om  # noqa: PLC0415 — avoid circular import at module load

    if event.get("platform") == "agent":
        return om.ingest_agent_entry(
            event,
            quiet=quiet,
            defer_mark=defer_mark,
            pending_marks=pending_marks,
            pull_conn=pull_conn,
            routing_config=routing_config,
            ws_slug_map=ws_slug_map,
            defer_activity_refresh=defer_activity_refresh,
            activity_refresh_pairs=activity_refresh_pairs,
        )

    dedupe_key = relay_dedupe_key(event)
    ws_idempotency = f"ws:{dedupe_key}"
    own_conn = pull_conn is None
    conn = pull_conn or get_conn()
    prefetched = ingested_prefetch or set()
    if ws_idempotent_prefetch is not None:
        ws_dup = ws_idempotency in ws_idempotent_prefetch
    else:
        ws_dup = bool(
            conn.execute(
                "SELECT 1 FROM workspace_lead_events WHERE org_id = ? AND idempotency_key = ?",
                (DEFAULT_ORG_ID, ws_idempotency),
            ).fetchone()
        )
    relay_dup = (
        dedupe_key in prefetched
        if defer_mark or ingested_prefetch is not None
        else relay_already_ingested(dedupe_key)
    )
    if ws_dup or relay_dup:
        if debug_sentiment and relay_dup and event.get("platform") in PLUSVIBE_PLATFORMS:
            print(
                "[debug:sentiment] skipped duplicate "
                f"event_type={event.get('event_type','unknown')} "
                f"relay_id={event.get('relay_id') or '-'} dedupe_key={dedupe_key}"
            )
        if own_conn:
            conn.close()
        return None

    envelope_lead = event.get("lead") or ""
    envelope_event_type = (event.get("event_type") or "unknown").lower()
    platform = event.get("platform", "unknown")
    sender_raw = event.get("sender", "")
    sender_norm = om.normalize_event_sender(platform, sender_raw)
    raw = event.get("raw") or {}
    received_at = _relay_event_timestamp(event, om.normalize_relay_timestamp) or ""

    extracted = extract_relay_fields(platform, raw)
    lead_fields = extracted["lead"]
    event_fields = extracted["event"]
    signals = extracted.get("signals") or {}
    identity = extract_relay_identity(platform, raw, envelope_lead)

    email_hint = identity.get("email") or (
        envelope_lead if "@" in str(envelope_lead) else None
    )
    display_name = build_display_name(lead_fields, email_hint)
    if not display_name and email_hint and "@" in email_hint:
        display_name = name_from_email(email_hint)
    elif not display_name and identity.get("linkedin_url"):
        slug = identity["linkedin_url"].rstrip("/").split("/")[-1]
        display_name = slug.replace("-", " ").title() or f"Unknown ({platform})"
    elif not display_name:
        display_name = f"Unknown ({platform})"

    channel = CHANNEL_BY_PLATFORM.get(platform, "email")

    campaign_ctx = extract_campaign_context(platform, event_fields, raw)
    if (
        platform in PLUSVIBE_PLATFORMS
        and envelope_event_type == "all_positive_replies"
    ):
        dup_body = (
            event_fields.get("body")
            or raw.get("text_body")
            or raw.get("body")
            or raw.get("last_lead_reply")
            or ""
        )
        dup_campaign = event_fields.get("campaign") or campaign_ctx.campaign_name_raw or ""
        dup_email = email_hint or (envelope_lead if "@" in str(envelope_lead) else "")
        if plusvibe_positive_reply_is_duplicate(
            conn,
            email=str(dup_email or ""),
            campaign=str(dup_campaign or ""),
            body=str(dup_body or ""),
            received_at=received_at,
        ):
            _skip_plusvibe_positive_reply_duplicate(
                dedupe_key,
                defer_mark=defer_mark,
                pending_marks=pending_marks,
                pull_conn=pull_conn,
                own_conn=own_conn,
                conn=conn,
                quiet=quiet,
            )
            return None
    if (
        not force_workspace_id
        and not campaign_ctx.campaign_id
        and not campaign_ctx.campaign_name_raw
    ):
        om.quarantine_event(
            conn,
            DEFAULT_ORG_ID,
            campaign_ctx,
            reason="no_campaign_id",
            payload=event,
            external_event_id=str(event.get("relay_id") or ""),
        )
        if own_conn:
            conn.commit()
            conn.close()
        if not quiet:
            print(om.format_no_campaign_event_message(campaign_ctx), file=sys.stderr)
        return None

    workspace_id = force_workspace_id
    if not workspace_id:
        routing = om.resolve_workspace_for_ingest(
            conn,
            DEFAULT_ORG_ID,
            campaign_ctx,
            routing_config=routing_config,
            routing_cache=routing_cache,
        )
        if not routing:
            om.quarantine_event(
                conn,
                DEFAULT_ORG_ID,
                campaign_ctx,
                reason="no_campaign_map",
                payload=event,
                external_event_id=str(event.get("relay_id") or ""),
            )
            if own_conn:
                conn.commit()
                conn.close()
            if not quiet:
                print(om.format_unmapped_campaign_message(campaign_ctx), file=sys.stderr)
            return None
        workspace_id = routing.workspace_id

    profile = om.profile_from_relay_lead(lead_fields, identity, display_name)
    campaign_name_for_detail = event_fields.get("campaign") or campaign_ctx.campaign_name_raw
    upsert_result = om.upsert_lead_profile(
        profile,
        channel=channel,
        stage="prospecting",
        notes=f"Auto-imported from {platform} via relay",
        enrich_name=display_name if lead_fields.get("first_name") else None,
        source="relay_sync",
        source_detail=campaign_name_for_detail,
        source_platform=platform,
        conn=conn,
    )
    if upsert_result.get("status") == "error":
        identities = om.collect_identities_from_event(identity, raw, platform)
        if not identities:
            om.ensure_organization(conn)
            om.quarantine_event(
                conn,
                DEFAULT_ORG_ID,
                campaign_ctx,
                reason="missing_identity",
                payload=event,
                external_event_id=str(event.get("relay_id") or ""),
            )
            if own_conn:
                conn.commit()
                conn.close()
        elif own_conn:
            conn.close()
        return None
    lead_id = upsert_result["id"]

    cfg = routing_config or om.get_org_routing_config(conn, DEFAULT_ORG_ID)
    if cfg.mode == om.WORKSPACE_ROUTING_SINGLE:
        om.ensure_default_org_workspace(conn)
    identities = om.collect_identities_from_event(identity, raw, platform)
    for itype, val in identities:
        try:
            om.upsert_identity_alias(conn, DEFAULT_ORG_ID, lead_id, itype, val, source=platform)
        except ValueError:
            om.enqueue_identity_conflict_merge(
                conn, DEFAULT_ORG_ID, lead_id, itype, val, source=platform,
            )

    resolved = resolve_event(platform, envelope_event_type, raw)
    local_type = resolved.local_type
    direction = resolved.direction

    bounce_payload = None
    if local_type == "email_bounce":
        bounce_payload = om._extract_bounce_payload(raw, platform)

    subject = event_fields.get("subject") or f"{platform}: {envelope_event_type}"
    body = event_fields.get("body") or ""
    if platform == "calendly":
        body = build_calendly_meeting_note(raw, envelope_event_type)
    if local_type == "email_bounce" and bounce_payload and bounce_payload.get("bounce_message"):
        body = bounce_payload["bounce_message"]
    if body:
        body, _ = om.cap_event_body(body)
        body_preview = body[:200]
    elif sender_norm:
        body_preview = f"From {sender_norm}"[:200]
    else:
        body_preview = ""

    metadata = {
        "source": "relay",
        "platform": platform,
        "relay_received_at": received_at,
        "webhook_event": envelope_event_type,
    }
    if sender_norm:
        metadata["sender"] = sender_norm
    if event_fields.get("campaign"):
        metadata["campaign"] = event_fields["campaign"]
    if body:
        metadata["body"] = body
    if event.get("relay_id"):
        metadata["relay_id"] = event["relay_id"]

    reply_body = extract_reply_body(platform, local_type, raw, metadata, body_preview)
    if reply_body and reply_body != body:
        metadata["body"] = reply_body
        body = reply_body
        body_preview = reply_body[:200]

    if platform in PLUSVIBE_PLATFORMS:
        metadata.update(build_plusvibe_status_metadata(raw, signals, envelope_event_type))
    if platform == "calendly":
        metadata.update(build_calendly_status_metadata(envelope_event_type))
    if local_type == "email_bounce" and bounce_payload:
        metadata.update(build_bounce_event_metadata(bounce_payload, envelope_event_type))

    if debug_sentiment and platform in PLUSVIBE_PLATFORMS:
        raw_label = (raw.get("label") or "").strip().lower()
        raw_sentiment = (raw.get("sentiment") or "").strip().lower()
        signal_label = (signals.get("label") or "").strip().lower()
        signal_sentiment = (signals.get("sentiment") or "").strip().lower()
        normalized_label = metadata.get("lead_status_raw", "")
        normalized_sentiment = metadata.get("lead_status_sentiment", "")
        if normalized_label or normalized_sentiment or envelope_event_type.startswith("lead_marked_as_"):
            print(
                "[debug:sentiment] "
                f"event_type={envelope_event_type} "
                f"raw_label={raw_label or '-'} raw_sentiment={raw_sentiment or '-'} "
                f"signal_label={signal_label or '-'} signal_sentiment={signal_sentiment or '-'} "
                f"normalized_label={normalized_label or '-'} "
                f"normalized_sentiment={normalized_sentiment or '-'}"
            )

    campaign_name_for_event = event_fields.get("campaign") or campaign_ctx.campaign_name_raw
    event_id = om.log_event(
        lead_id=lead_id,
        event_type=local_type,
        direction=direction,
        channel=channel,
        subject=subject,
        body_preview=body_preview,
        metadata=metadata,
        campaign=campaign_name_for_event,
        event_at=received_at or None,
        sender=sender_norm,
        conn=conn,
        commit=False,
        refresh_activity=False,
    )

    event_time = received_at or None
    target_stage = relay_target_stage(
        platform, envelope_event_type, local_type, raw, metadata,
        resolved_stage=resolved.target_stage,
    )
    if target_stage:
        om.update_lead_stage(
            lead_id,
            target_stage,
            event_at=event_time,
            conn=conn,
            commit=False,
        )

    ws_status = target_stage or "prospecting"
    ws_lead_id = om.upsert_workspace_lead(
        conn, DEFAULT_ORG_ID, workspace_id, lead_id, status=ws_status,
    )
    if target_stage:
        stage_ts = event_time or datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE workspace_leads SET status = ?, stage_entered_at = ? WHERE id = ?",
            (target_stage, stage_ts, ws_lead_id),
        )
    ws_payload = {
        "event": metadata,
        "subject": subject,
        "body_preview": body_preview,
        "direction": direction,
        "channel": channel,
        "campaign_id": campaign_ctx.campaign_id,
        "campaign_name": campaign_ctx.campaign_name_raw,
    }
    om.append_workspace_event(
        conn,
        DEFAULT_ORG_ID,
        workspace_id,
        lead_id,
        ws_lead_id,
        event_type=local_type,
        event_at=received_at or datetime.now(timezone.utc).isoformat(),
        source_platform=platform,
        idempotency_key=ws_idempotency,
        payload=ws_payload,
        external_event_id=str(event.get("relay_id") or ""),
    )

    status_label = metadata.get("lead_status_raw")
    status_sentiment = metadata.get("lead_status_sentiment")
    if status_label or status_sentiment:
        mat_sets, mat_params = [], []
        if status_label:
            mat_sets.append("current_status_label = ?")
            mat_params.append(status_label)
        if status_sentiment:
            mat_sets.append("current_status_sentiment = ?")
            mat_params.append(status_sentiment)
        mat_sets.append("updated_at = datetime('now')")
        mat_params.append(ws_lead_id)
        conn.execute(
            f"UPDATE workspace_leads SET {', '.join(mat_sets)} WHERE id = ?", mat_params
        )

    if sender_norm:
        event_at_ts = received_at or datetime.now(timezone.utc).isoformat()
        om._update_lead_sender(conn, lead_id, workspace_id, sender_norm, platform, event_at_ts)

    if local_type in ("linkedin_connect", "linkedin_connection_accepted") and workspace_id:
        sender_li = sender_norm or om.normalize_linkedin(sender_raw)
        if sender_li:
            event_at_ts = received_at or datetime.now(timezone.utc).isoformat()
            om.upsert_linkedin_status(
                conn, workspace_id, lead_id, sender_li, local_type, event_at_ts
            )

    if local_type == "email_bounce":
        payload = bounce_payload or om._extract_bounce_payload(raw, platform)
        bounce_type = payload["bounce_type"]
        bounce_reason = payload["bounce_message"]
        om._record_platform_bounce(
            conn, lead_id, email_hint, platform,
            bounce_type=bounce_type,
            bounce_reason=bounce_reason,
            event_at=received_at,
        )
        campaign_name = event_fields.get("campaign") or campaign_ctx.campaign_name_raw
        campaign_id = None
        if campaign_name and str(campaign_name).strip():
            campaign_id = om.ensure_campaign(conn, str(campaign_name).strip(), lead_id)
        om._record_bounce_event(
            conn,
            lead_id=lead_id,
            event_id=event_id,
            platform=platform,
            sender_email=sender_norm or raw.get("sender_email") or "unknown",
            lead_email=email_hint or envelope_lead or "",
            payload=payload,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            workspace_id=workspace_id,
            event_at=received_at,
            relay_id=str(event.get("relay_id") or "") or None,
        )

    if defer_activity_refresh and activity_refresh_pairs is not None:
        activity_refresh_pairs.add((lead_id, workspace_id))
    else:
        om.refresh_lead_activity_from_events(conn, lead_id, workspace_id)

    if own_conn:
        conn.commit()
        conn.close()

    if defer_mark and pending_marks is not None:
        pending_marks.append((dedupe_key, lead_id))
    else:
        mark_relay_ingested(dedupe_key, lead_id)
    return lead_id
