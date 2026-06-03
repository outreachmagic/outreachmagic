"""Relay webhook ingest — maps vendor events to local SQLite via platform_registry."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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
    if forced_label == "interested":
        sentiment = "positive"
    elif forced_label in ("not_interested", "not interested"):
        sentiment = "negative"
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
            "not_interested" in et
            or label in ("not_interested", "not interested")
            or sentiment == "negative"
        ):
            return "lost"
        if (
            "interested" in et
            or label == "interested"
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


def relay_already_ingested(dedupe_key: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM relay_ingested WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    conn.close()
    return row is not None


def prefetch_relay_ingested(dedupe_keys: list[str]) -> set[str]:
    """Return which dedupe keys already exist (batched IN lookup for pull pages)."""
    if not dedupe_keys:
        return set()
    unique = list(dict.fromkeys(dedupe_keys))
    found: set[str] = set()
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
        conn.close()
    return found


def mark_relay_ingested(dedupe_key: str, lead_id: Optional[int]) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
        (dedupe_key, lead_id),
    )
    conn.commit()
    conn.close()


def mark_relay_ingested_many(entries: list[tuple[str, Optional[int]]]) -> None:
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
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
            unique,
        )
        conn.commit()
    finally:
        conn.close()


def ingest_relay_event(
    event: dict,
    debug_sentiment: bool = False,
    force_workspace_id: Optional[str] = None,
    quiet: bool = False,
    *,
    defer_mark: bool = False,
    pending_marks: Optional[list] = None,
) -> Optional[int]:
    """Take a relay event and write it to the local SQLite database. Returns None if duplicate."""
    import pipeline as om  # noqa: PLC0415 — avoid circular import at module load

    if event.get("platform") == "agent":
        return om.ingest_agent_entry(
            event,
            quiet=quiet,
            defer_mark=defer_mark,
            pending_marks=pending_marks,
        )

    dedupe_key = relay_dedupe_key(event)
    ws_idempotency = f"ws:{dedupe_key}"
    conn = get_conn()
    if conn.execute(
        "SELECT 1 FROM workspace_lead_events WHERE org_id = ? AND idempotency_key = ?",
        (DEFAULT_ORG_ID, ws_idempotency),
    ).fetchone():
        conn.close()
        if relay_already_ingested(dedupe_key):
            return None
    conn.close()

    if relay_already_ingested(dedupe_key):
        if debug_sentiment and event.get("platform") in PLUSVIBE_PLATFORMS:
            print(
                "[debug:sentiment] skipped duplicate "
                f"event_type={event.get('event_type','unknown')} "
                f"relay_id={event.get('relay_id') or '-'} dedupe_key={dedupe_key}"
            )
        return None

    envelope_lead = event.get("lead") or ""
    envelope_event_type = (event.get("event_type") or "unknown").lower()
    platform = event.get("platform", "unknown")
    sender_raw = event.get("sender", "")
    sender_norm = om.normalize_event_sender(platform, sender_raw)
    received_at = event.get("received_at", "")
    raw = event.get("raw") or {}

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
    workspace_id = force_workspace_id
    if not workspace_id:
        conn = get_conn()
        routing = om.resolve_workspace_for_ingest(conn, DEFAULT_ORG_ID, campaign_ctx)
        if not routing:
            om.quarantine_event(
                conn,
                DEFAULT_ORG_ID,
                campaign_ctx,
                reason="no_campaign_map",
                payload=event,
                external_event_id=str(event.get("relay_id") or ""),
            )
            conn.commit()
            conn.close()
            if not quiet:
                print(om.format_unmapped_campaign_message(campaign_ctx), file=sys.stderr)
            return None
        workspace_id = routing.workspace_id
        conn.close()

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
    )
    if upsert_result.get("status") == "error":
        identities = om.collect_identities_from_event(identity, raw, platform)
        if not identities:
            conn = get_conn()
            om.ensure_organization(conn)
            om.quarantine_event(
                conn,
                DEFAULT_ORG_ID,
                campaign_ctx,
                reason="missing_identity",
                payload=event,
                external_event_id=str(event.get("relay_id") or ""),
            )
            conn.commit()
            conn.close()
        return None
    lead_id = upsert_result["id"]

    conn = get_conn()
    if om.get_org_routing_config(conn, DEFAULT_ORG_ID).mode == om.WORKSPACE_ROUTING_SINGLE:
        om.ensure_default_org_workspace(conn)
    identities = om.collect_identities_from_event(identity, raw, platform)
    for itype, val in identities:
        try:
            om.upsert_identity_alias(conn, DEFAULT_ORG_ID, lead_id, itype, val, source=platform)
        except ValueError:
            om.enqueue_identity_conflict_merge(
                conn, DEFAULT_ORG_ID, lead_id, itype, val, source=platform,
            )
    conn.commit()
    conn.close()

    resolved = resolve_event(platform, envelope_event_type, raw)
    local_type = resolved.local_type
    direction = resolved.direction

    bounce_payload = None
    if local_type == "email_bounce":
        bounce_payload = om._extract_bounce_payload(raw, platform)

    subject = event_fields.get("subject") or f"{platform}: {envelope_event_type}"
    body = event_fields.get("body") or ""
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

    event_id = om.log_event(
        lead_id=lead_id,
        event_type=local_type,
        direction=direction,
        channel=channel,
        subject=subject,
        body_preview=body_preview,
        metadata=metadata,
        event_at=received_at or None,
        sender=sender_norm,
    )

    event_time = received_at or None
    target_stage = relay_target_stage(
        platform, envelope_event_type, local_type, raw, metadata,
        resolved_stage=resolved.target_stage,
    )
    if target_stage:
        om.update_lead_stage(lead_id, target_stage, event_at=event_time)

    ws_status = target_stage or "prospecting"
    conn = get_conn()
    ws_lead_id = om.upsert_workspace_lead(
        conn, DEFAULT_ORG_ID, workspace_id, lead_id, status=ws_status
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

    om.refresh_lead_activity_from_events(conn, lead_id, workspace_id)
    conn.execute(
        "UPDATE leads SET cloud_pending = 1, updated_at = datetime('now') WHERE id = ?",
        (lead_id,),
    )

    conn.commit()
    conn.close()

    if defer_mark and pending_marks is not None:
        pending_marks.append((dedupe_key, lead_id))
    else:
        mark_relay_ingested(dedupe_key, lead_id)
    return lead_id
