"""pipeline.py reprocess — Re-apply current extractors to ingested data.

Usage (wired through pipeline.py CLI):
    pipeline.py reprocess --kind events --from 0 --to 100000 --platform prosp

Design:
    The relay replay endpoints (GET /api/v1/replay/events and
    GET /api/v1/replay/snapshots) return the exact same event shape as the
    normal /pull endpoint, but bounded by an explicit ID range.  The relay
    does no field extraction — it's a thin D1 pass-through of stored payloads.

    This module fetches raw payloads from the replay endpoints, re-runs the
    same extractor functions that normal ingest uses, and batch-UPDATEs the
    local DB rows (metadata_json, subject, created_at, and bounce tables).
    No INSERTs, no cursor changes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bounces import (
    build_bounce_event_metadata,
    extract_bounce_payload,
    record_bounce_event,
    record_platform_bounce,
)
from db_conn import get_conn
from platform_registry import resolve_event
from relay_extractors import extract_relay_fields, extract_relay_identity
from workspace_routing import DEFAULT_ORG_ID, extract_campaign_context

RELAY_URL = "https://api.outreachmagic.io"
try:
    _VERSION_PATH = Path(__file__).parent / "VERSION"
    __version__ = _VERSION_PATH.read_text().strip()
except Exception:
    __version__ = "0.0.0"

REPROCESS_PAGE_SIZE = 1000
REPROCESS_HTTP_TIMEOUT = 120


def _normalize_ts(ts: Optional[str]) -> Optional[str]:
    """Normalize an ISO timestamp to UTC with timezone offset."""
    if not ts:
        return None
    s = str(ts).strip()
    if "T" in s:
        if s.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", s):
            return s
        return s + "+00:00"
    return s


def _reprocess_event_timestamp(event: dict) -> Optional[str]:
    """Prefer original send timestamp (sent_on) over relay received time."""
    payload = event.get("payload") or {}
    for key in ("sent_on", "sent_at"):
        val = payload.get(key)
        if val:
            return _normalize_ts(val)
    for key in ("received_at", "created_at", "timestamp"):
        val = event.get(key)
        if val:
            return _normalize_ts(val)
    return None


def _replay_http_get(url: str, agent_key: str, timeout: int = REPROCESS_HTTP_TIMEOUT) -> dict:
    """GET a relay replay endpoint and return the JSON response."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Outreach Magic/{__version__}",
            "Authorization": f"Bearer {agent_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_replay_page(
    agent_key: str,
    kind: str,
    after_id: int,
    to_id: Optional[int],
    platform: Optional[str],
) -> dict:
    """Fetch one page of replay data from the relay."""
    if kind == "events":
        path = f"/api/v1/replay/events?after_id={after_id}"
        if to_id is not None:
            path += f"&max_id={to_id}"
        if platform:
            path += f"&platform={urllib.parse.quote(platform)}"
        path += f"&limit={REPROCESS_PAGE_SIZE}"
    else:
        path = f"/api/v1/replay/snapshots?after_id={after_id}&kind={kind}"
        if to_id is not None:
            path += f"&max_id={to_id}"
        path += f"&limit={REPROCESS_PAGE_SIZE}"
    return _replay_http_get(f"{RELAY_URL}{path}", agent_key)


def _reprocess_events_batch(conn: sqlite3.Connection, events: list[dict], *, verbose: bool, reingest: bool = False) -> int:
    """Re-extract and batch-UPDATE events from one replay response page.

    Preserves any existing metadata_json fields that extractors don't touch.
    Also re-classifies bounce events and updates bounce-specific tables.
    When reingest=True, re-ingests all events from scratch (deletes existing
    local rows first, then re-runs full ingest) so metadata is refreshed from the
    latest D1 state.
    """
    # Collect all relay_ids in this batch to load existing metadata in one query.
    relay_ids = [evt.get("relay_id") for evt in events if evt.get("relay_id") is not None]
    if not relay_ids:
        return 0

    placeholders = ",".join("?" for _ in relay_ids)
    existing_rows = conn.execute(
        f"SELECT relay_id, id, lead_id, metadata_json, created_at, subject FROM events WHERE relay_id IN ({placeholders})",
        relay_ids,
    ).fetchall()
    existing_map: dict[int, dict] = {}
    # Also track local event ID and lead ID for bounce table updates.
    event_id_map: dict[int, int] = {}
    lead_id_map: dict[int, int] = {}
    # Track existing columns for comparison.
    existing_created_at: dict[int, Optional[str]] = {}
    existing_subject: dict[int, Optional[str]] = {}
    for row in existing_rows:
        rid = row[0]
        event_id_map[rid] = row[1]
        lead_id_map[rid] = row[2]
        existing_created_at[rid] = row[4]
        existing_subject[rid] = row[5]
        try:
            existing_map[rid] = json.loads(row[3]) if row[3] else {}
        except (json.JSONDecodeError, TypeError):
            existing_map[rid] = {}

    # Ingest (or re-ingest) events into the local database.
    # When reingest is True, all events from the replay are re-ingested
    # from scratch — existing rows are deleted first so ingest_relay_event
    # runs fresh (lead resolution, dedup, campaign linking, etc.).
    if reingest:
        from relay_ingest import ingest_relay_event  # noqa: PLC0415

        for evt in events:
            rid = evt.get("relay_id")
            if rid is None:
                continue
            # Delete existing row so we get a clean re-ingest.
            existing_id = event_id_map.get(rid)
            if existing_id is not None:
                conn.execute("DELETE FROM events WHERE id = ?", (existing_id,))
            try:
                ingested = ingest_relay_event(evt, quiet=True, pull_conn=conn)
                if ingested is not None:
                    existing_map[rid] = {}
                    event_id_map[rid] = ingested
            except Exception as exc:
                if not verbose:
                    print(f"  [reprocess] ingest failed relay_id={rid}: {exc}", file=sys.stderr)

    # Pre-fetch lead emails for bounce re-classification.
    lead_ids = list(set(lead_id_map.values()))
    lead_email_map: dict[int, str] = {}
    if lead_ids:
        lp = ",".join("?" for _ in lead_ids)
        for lr in conn.execute(
            f"SELECT id, email FROM leads WHERE id IN ({lp})",
            lead_ids,
        ).fetchall():
            lead_email_map[lr[0]] = lr[1] or ""

    col_updates: list[tuple[str, Optional[str], Optional[str], int]] = []  # (new_json, subject, created_at, relay_id)
    for evt in events:
        relay_id = evt.get("relay_id")
        if relay_id is None:
            continue

        platform = evt.get("platform", "unknown")
        envelope_event_type = (evt.get("event_type") or "unknown").lower()
        payload = evt.get("payload") or {}
        envelope_lead = evt.get("entity_key") or evt.get("lead") or ""

        # Re-run extraction on the full relay payload.
        extracted = extract_relay_fields(platform, payload)
        event_fields = extracted.get("event") or {}

        # Re-extract campaign context.
        campaign_ctx = extract_campaign_context(platform, event_fields, payload)

        # Compute event_at from sent_on when available.
        event_at = _reprocess_event_timestamp(evt)

        # Start from existing metadata, then overlay re-extracted fields.
        old_meta = existing_map.get(relay_id, {})
        new_meta = dict(old_meta)
        new_meta["relay_id"] = relay_id  # always set

        if event_fields.get("campaign"):
            new_meta["campaign"] = event_fields["campaign"]
        if campaign_ctx.campaign_platform_id:
            new_meta["campaign_platform_id"] = campaign_ctx.campaign_platform_id
        if event_fields.get("subject"):
            new_meta["subject"] = event_fields["subject"]
        if event_fields.get("message_id"):
            new_meta["message_id"] = event_fields["message_id"]
        body_text = event_fields.get("body")
        if body_text:
            new_meta["body"] = body_text
        if evt.get("sender"):
            new_meta["sender"] = evt["sender"]

        # Determine re-extracted subject and timestamp for column updates.
        re_subject = event_fields.get("subject") or existing_subject.get(relay_id)
        re_created_at = event_at or existing_created_at.get(relay_id)

        # Determine if this is a bounce event using the full resolve pipeline
        # (matches how relay_ingest.py classifies events — catches all bounce
        # types including those from is_bounce_event_type()).
        resolved = resolve_event(platform, envelope_event_type, payload)
        is_bounce = resolved.local_type == "email_bounce"

        bounce_payload = None
        if is_bounce:
            bounce_payload = extract_bounce_payload(payload, platform)
            if bounce_payload.get("bounce_type"):
                new_meta.update(build_bounce_event_metadata(bounce_payload, envelope_event_type))

        if verbose:
            for field in ("campaign", "bounce_type", "subject"):
                old_val = old_meta.get(field)
                new_val = new_meta.get(field)
                if old_val != new_val:
                    print(
                        f"  [reprocess] event relay_id={relay_id} {field}: "
                        f"{old_val!r} -> {new_val!r}",
                        file=sys.stderr,
                    )

        old_json = json.dumps(old_meta, sort_keys=True, default=str)
        new_json = json.dumps(new_meta, sort_keys=True, default=str)
        meta_changed = old_json != new_json
        subject_changed = re_subject != existing_subject.get(relay_id)
        created_at_changed = re_created_at != existing_created_at.get(relay_id)
        if meta_changed or subject_changed or created_at_changed:
            col_updates.append((new_json, re_subject, re_created_at, relay_id))

        # Also record bounce classification in bounce-specific tables.
        if is_bounce and bounce_payload and bounce_payload.get("bounce_type"):
            local_event_id = event_id_map.get(relay_id)
            lead_id = lead_id_map.get(relay_id)
            if local_event_id is not None and lead_id is not None:
                sender_raw = payload.get("sender", "") or evt.get("sender", "")
                sender_email = sender_raw or payload.get("sender_email") or "unknown"
                lead_email = lead_email_map.get(lead_id) or envelope_lead or ""
                campaign_name = event_fields.get("campaign") or campaign_ctx.campaign_name_raw
                bounce_campaign_id = None
                if campaign_name and str(campaign_name).strip():
                    # ensure_campaign lives in pipeline module;
                    # for simplicity, use campaign_id if resolved.
                    pass
                record_platform_bounce(
                    conn,
                    lead_id,
                    lead_email,
                    platform,
                    bounce_type=bounce_payload["bounce_type"],
                    bounce_reason=bounce_payload.get("bounce_message", ""),
                )
                record_bounce_event(
                    conn,
                    lead_id=lead_id,
                    event_id=local_event_id,
                    platform=platform,
                    sender_email=sender_email,
                    lead_email=lead_email,
                    payload=bounce_payload,
                    workspace_id=None,  # workspace lookup requires pipeline module
                )

    if not col_updates:
        return 0

    conn.executemany(
        "UPDATE events SET metadata_json = ?, subject = COALESCE(?, subject), created_at = COALESCE(?, created_at) WHERE relay_id = ?",
        [(uj, subj, cat, rid) for uj, subj, cat, rid in col_updates],
    )
    conn.commit()
    return len(col_updates)


def reprocess_events(
    agent_key: str,
    *,
    from_id: int = 0,
    to_id: Optional[int] = None,
    platform: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
    reingest: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Re-extract all events in an ID range and UPDATE metadata_json in place.

    When reingest=True, re-ingests all events from scratch (deletes existing
    local rows first, then re-runs full ingest pipeline) so metadata is refreshed
    from the latest D1 state.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()

    total_updated = 0
    total_fetched = 0
    after_id = from_id
    pages = 0
    started = time.monotonic()

    try:
        while True:
            result = _fetch_replay_page(agent_key, "events", after_id, to_id, platform)
            events = result.get("events") or []
            if not events:
                break
            total_fetched += len(events)

            if not dry_run:
                updated = _reprocess_events_batch(conn, events, verbose=verbose, reingest=reingest)
                total_updated += updated

            after_id = result.get("next_after_id", after_id)
            pages += 1

            if not result.get("has_more"):
                break
    finally:
        if own_conn:
            conn.close()

    elapsed = time.monotonic() - started
    return {
        "kind": "events",
        "pages": pages,
        "fetched": total_fetched,
        "updated": total_updated,
        "dry_run": dry_run,
        "elapsed_s": round(elapsed, 1),
        "rate": round(total_fetched / elapsed, 0) if elapsed > 0 else 0,
    }
