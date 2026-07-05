"""
Export, relay sync, replay, and account CLI functions.
Extracted from pipeline.py's sync section.
"""

import concurrent.futures
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from constants import (
    BILLING_UPGRADE_URL,
    RELAY_BULK_THRESHOLD,
    RELAY_PULL_EVENT_MAX,
    RELAY_PULL_HARD_TIMEOUT_BUFFER,
    RELAY_PULL_PAGE_SIZE,
    RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT,
    RELAY_PULL_SNAPSHOT_MAX,
    USAGE_CRITICAL_PERCENT,
    USAGE_WARNING_PERCENT,
)
from db_conn import (
    apply_bulk_pull_pragmas,
    database_has_schema,
    end_bulk_pull_session,
    get_conn,
)
from om_paths import (
    get_data_root,
    get_db_path,
    resolve_project_path,
    set_db_path_override,
    working_paths_payload,
)
from pipeline_utils import furthest_stage
from pipeline_update import (
    _chmod_best_effort,
    clear_snapshot_cursors,
    get_agent_key,
    get_last_max_id,
    get_last_sync,
    get_or_create_client_id,
    get_snapshot_cursor,
    load_config,
    normalize_relay_timestamp,
    save_config,
    set_last_max_id,
    set_last_pull,
    set_last_sync,
    set_snapshot_cursor,
)
from pipeline_tags import (
    update_lead_stage,
    log_event,
    get_stats,
    get_pipeline,
    _decode_event_metadata,
)
from relay_ingest import (
    ingest_relay_event,
    mark_relay_ingested,
    mark_relay_ingested_many,
    prefetch_relay_ingested,
    prefetch_ws_idempotency_keys,
    relay_already_ingested,
    relay_dedupe_key,
    unsynced_event_clause,
    unsynced_lead_clause,
)
from workspace_routing import (
    CampaignRoutingCache,
    DEFAULT_ORG_ID,
    OrgRoutingConfig,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    append_workspace_event,
    extract_campaign_context,
    find_lead_by_identity,
    get_org_routing_config,
    lead_entity_key,
    parse_entity_key,
    parse_linkedin_value,
    resolve_workspace,
    resolve_workspace_identity,
    upsert_workspace_lead,
)
from activity_sync import refresh_lead_activity_from_events
from formatters import format_pipeline_table, format_stats
from lead_sync import (
    apply_agent_lead_core_payload,
    apply_agent_lead_workspace_payload,
    build_lead_core_sync_payload,
    build_lead_workspace_sync_payload,
    resolve_lead_from_agent_sync,
)
from normalize import normalize_linkedin
from platform_registry import platform_map_json, PLATFORM_LABELS, PLATFORM_SETUP_HINTS
import connections_cloud
import quarantine_resolutions as qres
import routing_cloud
# ── Module-level constants (originally from pipeline.py) ──────────────
PULL_KINDS_ALL = frozenset({"events", "core", "workspace", "company"})
RELAY_PULL_HTTP_TIMEOUT = 60
RELAY_PULL_HTTP_RETRIES = 2
RELAY_URL = "https://api.outreachmagic.io"
_SNAPSHOT_KIND_STREAM = {"core": "Lead", "workspace": "Workspace", "company": "Company"}
_RELAY_STREAM_EVENT = "Event"
_ARROW_PULL = "↓"
_ARROW_PUSH = "↑"
_DB_OPTIONAL_COMMANDS = frozenset({
    "admin-diagnose-relay",
    "sheets-campaign-stats",
    "export-csv",
    "export-json",
    "export-local-analytics",
    "restore",
    "replay-quarantine",
    "login",
    "logout",
    "whoami",
    "platform-map",
    "status",
    "connections",
    "connect-platform",
    "disconnect-platform",
    "companion-persona-evaluate",
    "companion-campaign-evaluate",
})
_SYNC_LOG_FILE: Optional[Path] = None


def _init_relay_sync_log() -> None:
    """Optional file mirror: OM_SYNC_LOG=/path/to/batch_sync.log"""
    global _SYNC_LOG_FILE
    raw = os.environ.get("OM_SYNC_LOG", "").strip()
    if raw:
        _SYNC_LOG_FILE = Path(raw).expanduser()
        _SYNC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _relay_log(msg: str) -> None:
    """Stderr + optional log file, always flushed (safe for tail -f)."""
    global _SYNC_LOG_FILE
    if _SYNC_LOG_FILE is None and os.environ.get("OM_SYNC_LOG", "").strip():
        _init_relay_sync_log()
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    if _SYNC_LOG_FILE:
        try:
            with _SYNC_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
def _replay_quarantine_row(queue_id: str, workspace_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload_json FROM unmapped_campaign_queue WHERE id = ? AND status = 'pending'",
        (queue_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "error": "queue item not found or not pending"}
    try:
        event = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        conn.close()
        return {"status": "error", "error": "invalid payload"}
    conn.close()
    lead_id = ingest_relay_event(event, force_workspace_id=workspace_id, quiet=True)
    conn = get_conn()
    conn.execute(
        """UPDATE unmapped_campaign_queue
           SET status = 'replayed', resolved_at = datetime('now')
           WHERE id = ?""",
        (queue_id,),
    )
    conn.commit()
    conn.close()
    if lead_id is None:
        return {"status": "error", "error": "ingest failed", "queue_id": queue_id}
    return {"status": "ok", "queue_id": queue_id, "lead_id": lead_id}


def replay_pending_quarantine(workspace_slug: Optional[str] = None, limit: int = 100) -> dict:
    from pipeline import list_quarantine

    pending = list_quarantine(status="pending", limit=limit)
    replayed = skipped = 0
    slug_cache = qres.WorkspaceSlugCache()
    for item in pending:
        if workspace_slug:
            ws_id = slug_cache.workspace_id(workspace_slug)
        else:
            conn = get_conn()
            campaign_name = item.get("campaign_name_raw") or item.get("campaign_name_normalized")
            if not campaign_name and not item.get("campaign_platform_id"):
                campaign_name = "unknown"
            ctx = extract_campaign_context(
                item["source_platform"],
                {},
                {
                    "campaign_id": item.get("campaign_platform_id"),
                    "campaign_name": campaign_name,
                },
            )
            routing = resolve_workspace(conn, DEFAULT_ORG_ID, ctx)
            conn.close()
            if not routing:
                skipped += 1
                continue
            ws_id = routing.workspace_id
        if not ws_id:
            skipped += 1
            continue
        r = _replay_quarantine_row(item["id"], ws_id)
        if r.get("status") == "ok":
            replayed += 1
        else:
            skipped += 1
    return {"replayed": replayed, "skipped": skipped}


# Relay ingest lives in relay_ingest.py (imported above).


# ──────────────────────────────────────────────────────────────────────
# Export local changes & agent entry replay
# ──────────────────────────────────────────────────────────────────────


def _agent_sync_payload_from_entity_key(
    entity_key: str,
    payload: Optional[dict] = None,
) -> dict:
    """Build minimal lead_sync payload from an agent entity_key (email, LinkedIn, etc.)."""
    merged = dict(payload or {})
    key = (entity_key or "").strip()
    if not key:
        return merged
    if "@" in key and not merged.get("email"):
        merged["email"] = key.lower()
    elif not merged.get("linkedin") and (
        "linkedin" in key.lower()
        or key.startswith("http")
        or key.startswith("ACwAA")
        or key.lower().startswith("urn:li:")
    ):
        merged["linkedin"] = key
    return merged


def find_lead_by_identifier(conn: sqlite3.Connection, entity_key: str) -> Optional[int]:
    """Resolve entity_key (email, linkedin URL, or type:value identity) to a lead ID."""
    from pipeline import find_lead_by_email, find_lead_by_linkedin

    if not entity_key:
        return None
    key = entity_key.strip()
    if "@" in key:
        return find_lead_by_email(conn, key.lower())
    if "linkedin" in key.lower() or key.startswith("http") or key.startswith("ACwAA") or key.lower().startswith("urn:li:"):
        for itype, val in parse_linkedin_value(key):
            found = find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val)
            if found:
                return found
        norm = normalize_linkedin(key)
        return find_lead_by_linkedin(conn, norm) if norm else None
    itype, val = parse_entity_key(key)
    if itype and val:
        return find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val)
    return None


def _lead_workspace_slug(conn: sqlite3.Connection, lead_id: int) -> Optional[str]:
    """Return the workspace slug for a lead, or None."""
    row = conn.execute(
        """SELECT w.slug FROM workspace_leads wl
           JOIN workspaces w ON wl.workspace_id = w.id
           WHERE wl.lead_id = ? LIMIT 1""",
        (lead_id,),
    ).fetchone()
    return row["slug"] if row else None


def export_local_changes(
    *,
    all_leads: bool = False,
    workspace: Optional[str] = None,
    events_only: bool = False,
) -> dict:
    """Export locally-created leads and events as a JSON structure
    suitable for pushing to the relay or importing on another machine."""
    client_id = get_or_create_client_id()
    conn = get_conn()

    workspace_filter = ""
    workspace_params: list = []
    if workspace:
        ws_row = resolve_workspace_identity(conn, workspace)
        if ws_row:
            workspace_filter = """
                AND l.id IN (
                    SELECT lead_id FROM workspace_leads WHERE workspace_id = ?
                )"""
            workspace_params.append(ws_row["id"])

    entries: list[dict] = []
    if not events_only:
        entries = _export_local_lead_entries(
            conn,
            all_leads=all_leads,
            workspace_filter=workspace_filter,
            workspace_params=workspace_params,
        )

    _relay_log("export: querying unpushed timeline events from SQLite ...")
    t_export = time.monotonic()
    event_rows = conn.execute(
        f"""SELECT e.*, l.email, l.linkedin_url, c.name AS campaign_name
           FROM events e
           JOIN leads l ON e.lead_id = l.id
           LEFT JOIN campaigns c ON c.id = e.campaign_id
           WHERE {unsynced_event_clause("e")}
           ORDER BY e.created_at ASC""",
    ).fetchall()
    _relay_log(
        f"export: loaded {len(event_rows):,} event rows in {time.monotonic() - t_export:.1f}s — building payloads ..."
    )

    for n, row in enumerate(event_rows, start=1):
        entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, row["lead_id"])
        if not entity_key:
            continue
        ws_slug = _lead_workspace_slug(conn, row["lead_id"])
        meta = _decode_event_metadata(row["metadata_json"])
        campaign_name = (row["campaign_name"] or meta.get("campaign") or "").strip() or None
        event_entry: dict = {
            "action": "event_log",
            "entity_key": entity_key,
            "timestamp": normalize_relay_timestamp(row["created_at"]),
            "event_id": row["id"],
            "payload": {
                "event_type": row["event_type"],
                "direction": row["direction"],
                "channel": row["channel"],
            },
        }
        if ws_slug:
            event_entry["workspace"] = ws_slug
        if campaign_name:
            event_entry["payload"]["campaign"] = campaign_name
        if row["subject"]:
            event_entry["payload"]["subject"] = row["subject"]
        if row["body_preview"]:
            event_entry["payload"]["body_preview"] = row["body_preview"]
        if meta.get("body"):
            event_entry["payload"]["body"] = str(meta.get("body"))
        if row["sender"]:
            event_entry["payload"]["sender"] = row["sender"]
        entries.append(event_entry)
        if n % 5000 == 0:
            _relay_log(f"export: built {n:,}/{len(event_rows):,} event_log entries ...")

    if event_rows:
        _relay_log(
            f"export: done — {len(entries):,} event_log entries in {time.monotonic() - t_export:.1f}s"
        )

    conn.close()
    return {
        "version": 1,
        "client_id": client_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }


def _export_local_lead_entries(
    conn,
    *,
    all_leads: bool,
    workspace_filter: str,
    workspace_params: list,
) -> list[dict]:
    """Lead snapshot entries for export_local_changes (skipped when events_only)."""
    if all_leads:
        lead_rows = conn.execute(
            f"""SELECT l.*, COALESCE(co.name, l.company) AS company_display
                FROM leads l
                LEFT JOIN companies co ON l.company_id = co.id
                WHERE 1=1 {workspace_filter}
                ORDER BY l.created_at ASC""",
            workspace_params,
        ).fetchall()
    else:
        lead_rows = conn.execute(
            f"""SELECT l.*, COALESCE(co.name, l.company) AS company_display
                FROM leads l
                LEFT JOIN companies co ON l.company_id = co.id
                WHERE {unsynced_lead_clause("l")} {workspace_filter}
                ORDER BY l.created_at ASC""",
            workspace_params,
        ).fetchall()

    entries = []
    lead_ids = set()
    for row in lead_rows:
        lead_id = row["id"]
        lead_ids.add(lead_id)
        entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, lead_id)
        if not entity_key:
            continue
        core_payload = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
        if core_payload:
            entries.append({
                "action": "lead_core_update",
                "entity_key": entity_key,
                "timestamp": normalize_relay_timestamp(row["created_at"]),
                "payload": core_payload,
            })
        memberships = conn.execute(
            """SELECT w.slug FROM workspace_leads wl
               JOIN workspaces w ON w.id = wl.workspace_id
               WHERE wl.lead_id = ?""",
            (lead_id,),
        ).fetchall()
        if not memberships:
            ws_slug = _lead_workspace_slug(conn, lead_id)
            memberships = [{"slug": ws_slug}] if ws_slug else []
        for mem in memberships:
            ws_slug = mem["slug"]
            ws_payload = build_lead_workspace_sync_payload(
                conn, DEFAULT_ORG_ID, lead_id, workspace_slug=ws_slug,
            )
            if not ws_payload:
                continue
            entries.append({
                "action": "lead_workspace_update",
                "entity_key": entity_key,
                "workspace": ws_slug,
                "timestamp": normalize_relay_timestamp(row["created_at"]),
                "payload": ws_payload,
            })
        ws_slug = memberships[0]["slug"] if memberships else _lead_workspace_slug(conn, lead_id)

        if row["stage"] and row["stage"] != "prospecting":
            stage_entry: dict = {
                "action": "stage_change",
                "entity_key": entity_key,
                "timestamp": normalize_relay_timestamp(row["updated_at"]),
                "payload": {"stage": row["stage"]},
            }
            if ws_slug:
                stage_entry["workspace"] = ws_slug
            if row["next_action"]:
                stage_entry["payload"]["next_action"] = row["next_action"]
            entries.append(stage_entry)

    return entries


def write_export_csv(result: dict, path: str):
    """Write lead entries from an export as a CSV compatible with import-profiles."""
    lead_entries = [
        e for e in result.get("entries", [])
        if e["action"] in ("lead_core_update", "lead_workspace_update")
    ]
    if not lead_entries:
        print(json.dumps({"status": "empty", "message": "No local leads to export"}))
        return
    fieldnames = ["email", "linkedin", "name", "company", "title", "industry", "headcount", "stage", "notes"]
    out_path = resolve_project_path(path, kind="export", for_write=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for entry in lead_entries:
            writer.writerow(entry.get("payload", {}))
    print(json.dumps({"status": "exported", "file": str(out_path), "leads": len(lead_entries)}))


def agent_entry_dedupe_key(event: dict, local_client_id: Optional[str] = None) -> Optional[str]:
    """Dedupe key for agent pull entries (distinct from relay:{id} for snapshots)."""
    if event.get("platform") != "agent":
        return None
    payload = event.get("payload") or {}
    client_id = payload.get("client_id", "")
    entity_key = event.get("entity_key", "")
    action = payload.get("action", "")
    timestamp = payload.get("timestamp", "")
    if not client_id or not action:
        return None
    local = local_client_id if local_client_id is not None else get_or_create_client_id()
    if client_id == local:
        return None
    return f"agent:{client_id}:{entity_key}:{action}:{timestamp}"


def pull_page_dedupe_keys(events: list, local_client_id: str) -> list[str]:
    """All dedupe keys to prefetch for one relay pull page."""
    from relay_ingest import relay_dedupe_key

    keys: list[str] = []
    for event in events:
        keys.append(relay_dedupe_key(event))
        agent_key = agent_entry_dedupe_key(event, local_client_id)
        if agent_key:
            keys.append(agent_key)
    return keys


def pull_page_ws_idempotency_keys(events: list) -> list[str]:
    """Workspace event idempotency keys for sequencer events on one pull page."""
    from relay_ingest import relay_dedupe_key

    keys: list[str] = []
    for event in events:
        if event.get("platform") == "agent":
            continue
        keys.append(f"ws:{relay_dedupe_key(event)}")
    return keys


def _append_pull_ingest_marks(
    pending_marks: list,
    event: dict,
    lead_id: Optional[int],
    local_client_id: str,
) -> None:
    """Record sequencer/webhook dedupe keys (agent entries mark in ingest_agent_entry)."""
    from relay_ingest import relay_dedupe_key

    pending_marks.append((relay_dedupe_key(event), lead_id))


def _pull_workspace_slug_map(conn: sqlite3.Connection, org_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT slug, id FROM workspaces WHERE org_id = ?", (org_id,),
    ).fetchall()
    return {str(r["slug"]): str(r["id"]) for r in rows}


def _pull_page_already_ingested(
    event: dict,
    ingested_set: set[str],
    local_client_id: str,
) -> bool:
    from relay_ingest import relay_dedupe_key

    if relay_dedupe_key(event) in ingested_set:
        return True
    agent_key = agent_entry_dedupe_key(event, local_client_id)
    return bool(agent_key and agent_key in ingested_set)


def ingest_agent_entry(
    event: dict,
    quiet: bool = False,
    *,
    defer_mark: bool = False,
    pending_marks: Optional[list] = None,
    pull_conn: Optional[sqlite3.Connection] = None,
    routing_config: Optional[OrgRoutingConfig] = None,
    ws_slug_map: Optional[dict[str, str]] = None,
    defer_activity_refresh: bool = False,
    activity_refresh_pairs: Optional[set[tuple[int, str]]] = None,
) -> Optional[int]:
    """Replay an agent-originated mutation from another client during pull."""
    from pipeline import (
        append_workspace_event,
        find_lead_by_identifier,
        get_org_routing_config,
        get_or_create_client_id,
        log_event,
        upsert_workspace_lead,
    )

    envelope_payload = event.get("payload") or {}
    # Detect format: the new unified envelope format nests action/data in
    # payload (post-Jun 30 relay-db.js). Old flat format has action/client_id
    # at the event top level with enrichment data directly in event.payload.
    if "action" in envelope_payload or "data" in envelope_payload:
        action = envelope_payload.get("action", "")
        payload = envelope_payload.get("data") or {}
        client_id = envelope_payload.get("client_id", "")
        workspace_slug = envelope_payload.get("workspace")
        timestamp = envelope_payload.get("timestamp", "")
    else:
        action = event.get("action", "")
        payload = envelope_payload
        client_id = event.get("client_id", "")
        workspace_slug = event.get("workspace")
        timestamp = event.get("timestamp", "")
    entity_key = event.get("entity_key", "")

    local_client_id = get_or_create_client_id()
    if client_id == local_client_id:
        return None

    dedupe_key = f"agent:{client_id}:{entity_key}:{action}:{timestamp}"
    # Pull pages prefetch dedupe keys; skip per-row SELECT when batching marks.
    if not defer_mark and relay_already_ingested(dedupe_key):
        return None

    def _record_mark(key: str, lid: Optional[int]) -> None:
        if defer_mark and pending_marks is not None:
            pending_marks.append((key, lid))
        else:
            mark_relay_ingested(key, lid)

    own_conn = pull_conn is None
    conn = pull_conn or get_conn()
    lead_id = None
    company_id = None
    slug_map = ws_slug_map or {}
    try:
        org_id = DEFAULT_ORG_ID
        if action == "company_update":
            from pipeline import apply_agent_company_sync_payload, resolve_company_from_entity_key

            company_id = resolve_company_from_entity_key(conn, entity_key) if entity_key else None
            if company_id:
                apply_agent_company_sync_payload(
                    company_id,
                    payload,
                    conn=conn,
                )
            if own_conn:
                conn.commit()
                conn.close()
                conn = None
        elif action == "lead_core_update":
            lead_id = find_lead_by_identifier(conn, entity_key) if entity_key else None
            if not lead_id:
                if own_conn:
                    conn.close()
                    conn = None
                result = resolve_lead_from_agent_sync(
                    entity_key, payload, conn=None if own_conn else pull_conn,
                )
                if result.get("status") == "error":
                    _record_mark(dedupe_key, None)
                    return None
                lead_id = result.get("id")
                if not own_conn:
                    conn = pull_conn
            if lead_id:
                apply_agent_lead_core_payload(
                    lead_id, payload, org_id=org_id, entity_key=entity_key, conn=conn,
                )
            if own_conn and conn is not None:
                conn.commit()
                conn.close()
                conn = None
        elif action == "lead_workspace_update":
            routing = routing_config or get_org_routing_config(conn, org_id)
            workspace_id = None
            if routing.mode == WORKSPACE_ROUTING_SINGLE:
                workspace_id = routing.default_workspace_id
            elif workspace_slug:
                workspace_id = slug_map.get(workspace_slug)
                if not workspace_id:
                    ws_row = resolve_workspace_identity(conn, workspace_slug)
                    workspace_id = ws_row["id"] if ws_row else None
            if not workspace_id:
                if own_conn:
                    conn.close()
                    conn = None
                _record_mark(dedupe_key, None)
                return None
            lead_id = find_lead_by_identifier(conn, entity_key) if entity_key else None
            if not lead_id:
                if own_conn:
                    conn.close()
                    conn = None
                result = resolve_lead_from_agent_sync(
                    entity_key, {}, conn=None if own_conn else pull_conn,
                )
                if result.get("status") == "error":
                    _record_mark(dedupe_key, None)
                    return None
                lead_id = result.get("id")
                if not own_conn:
                    conn = pull_conn
            if lead_id:
                apply_agent_lead_workspace_payload(
                    lead_id, payload, org_id=org_id, workspace_id=workspace_id, conn=conn,
                )
            if own_conn and conn is not None:
                conn.commit()
                conn.close()
                conn = None
        else:
            routing = routing_config or get_org_routing_config(conn, org_id)
            workspace_id = None
            if routing.mode == WORKSPACE_ROUTING_SINGLE:
                workspace_id = routing.default_workspace_id
            elif workspace_slug:
                workspace_id = slug_map.get(workspace_slug)
                if not workspace_id:
                    ws_row = resolve_workspace_identity(conn, workspace_slug)
                    workspace_id = ws_row["id"] if ws_row else None

            if action == "stage_change":
                if not workspace_id:
                    if own_conn:
                        conn.close()
                        conn = None
                    _record_mark(dedupe_key, None)
                    return None
                lead_id = find_lead_by_identifier(conn, entity_key)
                if own_conn:
                    conn.close()
                    conn = None
                if lead_id and payload.get("stage"):
                    try:
                        update_lead_stage(
                            lead_id,
                            payload["stage"],
                            payload.get("next_action"),
                            conn=pull_conn if not own_conn else None,
                            commit=not own_conn,
                        )
                    except ValueError:
                        pass
            elif action == "event_log":
                if not workspace_id:
                    if own_conn:
                        conn.close()
                        conn = None
                    _record_mark(dedupe_key, None)
                    return None
                lead_id = find_lead_by_identifier(conn, entity_key)
                if not lead_id and entity_key:
                    bootstrap_payload = _agent_sync_payload_from_entity_key(
                        entity_key, payload,
                    )
                    if own_conn:
                        conn.close()
                        conn = None
                    result = resolve_lead_from_agent_sync(
                        entity_key,
                        bootstrap_payload,
                        conn=None if own_conn else pull_conn,
                    )
                    if result.get("status") == "error":
                        _record_mark(dedupe_key, None)
                        return None
                    lead_id = result.get("id")
                    if not own_conn:
                        conn = pull_conn
                if own_conn and conn is not None:
                    conn.close()
                    conn = None
                if lead_id:
                    event_at = normalize_relay_timestamp(timestamp) if timestamp else None
                    event_meta = {"source": "agent_sync", "origin_client": client_id}
                    if payload.get("body"):
                        event_meta["body"] = str(payload.get("body"))
                    relay_rid = event.get("relay_id")
                    if relay_rid is not None:
                        event_meta["relay_id"] = relay_rid
                    campaign = payload.get("campaign") or payload.get("campaign_name")
                    if campaign and str(campaign).strip():
                        event_meta["campaign"] = str(campaign).strip()
                    sender = payload.get("sender")
                    log_conn = conn if not own_conn else None
                    local_type = payload.get("event_type", "email_sent")
                    log_event(
                        lead_id,
                        event_type=local_type,
                        direction=payload.get("direction", "outbound"),
                        channel=payload.get("channel", "email"),
                        subject=payload.get("subject"),
                        body_preview=payload.get("body_preview"),
                        metadata=event_meta,
                        campaign=campaign,
                        event_at=event_at,
                        sender=sender,
                        conn=log_conn,
                        commit=log_conn is None,
                        refresh_activity=log_conn is None and not defer_activity_refresh,
                    )
                    ws_conn = log_conn
                    ws_own = ws_conn is None
                    if ws_own:
                        ws_conn = get_conn()
                    try:
                        ws_lead_id = upsert_workspace_lead(
                            ws_conn, org_id, workspace_id, lead_id,
                        )
                        ws_payload = {
                            "event": event_meta,
                            "subject": payload.get("subject"),
                            "body_preview": payload.get("body_preview"),
                            "direction": payload.get("direction", "outbound"),
                            "channel": payload.get("channel", "email"),
                            "campaign_name": campaign,
                        }
                        append_workspace_event(
                            ws_conn,
                            org_id,
                            workspace_id,
                            lead_id,
                            ws_lead_id,
                            event_type=local_type,
                            event_at=event_at or datetime.now(timezone.utc).isoformat(),
                            source_platform="agent",
                            idempotency_key=f"ws:{dedupe_key}",
                            payload=ws_payload,
                            external_event_id=str(relay_rid or ""),
                        )
                        if ws_own:
                            ws_conn.commit()
                    finally:
                        if ws_own:
                            ws_conn.close()
                    if (
                        defer_activity_refresh
                        and activity_refresh_pairs is not None
                        and workspace_id
                    ):
                        activity_refresh_pairs.add((lead_id, workspace_id))
            elif own_conn:
                conn.close()
                conn = None
    except Exception:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise

    if lead_id is not None or action in ("company_update", "lead_core_update", "lead_workspace_update"):
        _record_mark(dedupe_key, lead_id)
        relay_rid = event.get("relay_id")
        if relay_rid is not None:
            _record_mark(f"relay:{relay_rid}", lead_id)
    return lead_id




def _relay_http_get_json(req: urllib.request.Request, timeout: int) -> dict:
    """GET relay JSON with a hard wall-clock cap (urllib socket timeout alone can stall)."""
    from pipeline import urllib

    hard_limit = max(int(timeout) + RELAY_PULL_HARD_TIMEOUT_BUFFER, int(timeout))

    def _fetch() -> dict:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_fetch)
        try:
            return fut.result(timeout=hard_limit)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(f"relay HTTP read exceeded {hard_limit}s") from exc


def _estimate_relay_pages(pending: Optional[int], page_size: int = RELAY_PULL_PAGE_SIZE) -> Optional[int]:
    if pending is None or pending <= 0:
        return None
    return max(1, (pending + page_size - 1) // page_size)


def _snapshot_pull_limit_for_kind(kind: str, base: int) -> int:
    return min(int(base), RELAY_PULL_SNAPSHOT_MAX)




def parse_pull_kinds(raw: Optional[str]) -> Optional[frozenset[str]]:
    if not raw or not str(raw).strip():
        return None
    kinds = frozenset(k.strip().lower() for k in str(raw).split(",") if k.strip())
    unknown = kinds - PULL_KINDS_ALL
    if unknown:
        raise ValueError(
            f"unknown pull kind(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(PULL_KINDS_ALL))})"
        )
    return kinds


def _snapshot_pending_count(
    agent_key: str,
    snap_kind: str,
    after_cursor: int,
    *,
    timeout: int = RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT,
) -> Optional[int]:
    """One limit=1 relay read with include_pending (COUNT once per kind, not per page)."""
    from pipeline import pull_events_org

    snap = pull_events_org(
        agent_key,
        snapshot_after_id=after_cursor or None,
        snapshot_kind=snap_kind,
        snapshots_only=True,
        include_pending=True,
        limit=1,
        timeout=timeout,
    )
    if snap.get("error"):
        return None
    raw = snap.get("pending_snapshot_count")
    return int(raw) if raw is not None else None


def probe_relay_backlog(agent_key: str) -> dict:
    """Read-only backlog check: limit=1 per stream, no ingest (low relay/memory cost)."""
    from pipeline import pull_events_org

    report: dict = {"events": {}, "snapshots": {}}
    after_id = int(get_last_max_id() or 0)
    ev = pull_events_org(
        agent_key,
        after_id=after_id or None,
        include_pending=True,
        limit=1,
    )
    if ev.get("error"):
        raise RuntimeError(ev.get("message", "relay probe failed"))
    pending = ev.get("pending_event_count")
    report["events"] = {
        "cursor": after_id,
        "pending": pending,
        "page_size": RELAY_PULL_EVENT_MAX,
        "est_pages": _estimate_relay_pages(pending, RELAY_PULL_EVENT_MAX),
    }
    for kind in ("core", "workspace"):
        cur = int(get_snapshot_cursor(kind) or 0)
        page_size = _snapshot_pull_limit_for_kind(kind, RELAY_PULL_PAGE_SIZE)
        snap = pull_events_org(
            agent_key,
            snapshot_after_id=cur or None,
            snapshot_kind=kind,
            snapshots_only=True,
            include_pending=True,
            limit=1,
        )
        if snap.get("error"):
            raise RuntimeError(snap.get("message", f"relay probe failed ({kind})"))
        pending_snap = snap.get("pending_snapshot_count")
        report["snapshots"][kind] = {
            "cursor": cur,
            "pending": pending_snap,
            "page_size": page_size,
            "est_pages": _estimate_relay_pages(pending_snap, page_size),
        }
    return report


def print_relay_probe(report: dict) -> None:
    print("Relay backlog (probe — limit=1 row/stream, no ingest)")
    print("---------------------------------------------------")
    ev = report.get("events") or {}
    pending = ev.get("pending")
    pages = ev.get("est_pages")
    pending_s = f"~{pending:,}" if pending is not None else "unknown"
    pages_s = f"~{pages}p" if pages else "?"
    print(
        f"Events: cursor={ev.get('cursor', 0)} pending={pending_s} "
        f"({pages_s} @ {ev.get('page_size', RELAY_PULL_EVENT_MAX)}/p)"
    )
    for kind in ("core", "workspace"):
        snap = (report.get("snapshots") or {}).get(kind) or {}
        pending = snap.get("pending")
        pages = snap.get("est_pages")
        pending_s = f"~{pending:,}" if pending is not None else "unknown"
        pages_s = f"~{pages}p" if pages else "?"
        print(
            f"{kind.capitalize():9} cursor={snap.get('cursor', 0)} pending={pending_s} "
            f"({pages_s} @ {snap.get('page_size', '?')}/p)"
        )




def _progress_clock() -> str:
    return datetime.now().strftime("%H:%M")


def _progress_pct(done: int, total: Optional[int], *, remaining: bool = False) -> str:
    if total is None or total <= 0:
        return ""
    pct_done = min(100, int(100 * done / total))
    if remaining:
        return f" ({max(0, 100 - pct_done)}% remaining)"
    return f" ({pct_done}%)"


def _stream_pad(stream: str) -> str:
    return f"{stream:<9}"


def _page_label(page_n: int, total_pages: Optional[int] = None, *, more_follow: bool = False) -> str:
    if total_pages and total_pages > 0:
        return f"p{page_n}/{total_pages}"
    if more_follow:
        return f"p{page_n}+"
    return f"p{page_n}"


def _format_pull_pending_banner(
    stream: str,
    pending: int,
    est_pages: Optional[int],
    page_size: int,
) -> str:
    pages_hint = f"~{est_pages}p" if est_pages else "mult"
    return (
        f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: "
        f"~{pending:,} pending ({pages_hint} @ {page_size}/p) ..."
    )




def _progress_eta_seconds(done: int, total: Optional[int], elapsed_seconds: float) -> Optional[float]:
    if total is None or total <= 0 or done <= 0 or elapsed_seconds <= 0:
        return None
    remaining = max(0, total - done)
    if remaining <= 0:
        return 0.0
    rate = done / elapsed_seconds
    if rate <= 0:
        return None
    return remaining / rate


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


def _format_pull_progress(
    stream: str,
    *,
    page_n: int,
    total_pages: Optional[int] = None,
    page_len: int,
    seen: int,
    total: Optional[int] = None,
    more_follow: bool = False,
    total_only: bool = False,
    eta_seconds: Optional[float] = None,
) -> str:
    page = _page_label(page_n, total_pages, more_follow=more_follow)
    if total is not None and total > 0:
        counts = f"{seen:,}/{total:,}"
        pct = _progress_pct(seen, total, remaining=True)
    elif total_only:
        counts = f"{seen:,} total"
        pct = ""
    else:
        counts = f"{seen:,}"
        pct = ""
    eta = ""
    if eta_seconds is not None and eta_seconds > 0:
        eta = f", ~{_format_duration(eta_seconds)} left"
    return (
        f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: {page} — "
        f"{page_len:,} this page, {counts}{pct}{eta} ..."
    )


def _format_push_pending_banner(stream: str, pending: int, total_pages: int, page_size: int) -> str:
    return (
        f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(stream)}: "
        f"{pending:,} pending ({total_pages}p @ {page_size}/p) ..."
    )


def _format_push_progress(
    stream: str,
    *,
    page_n: int,
    total_pages: int,
    page_len: int,
    seen: int,
    total: int,
    elapsed: float,
    extra: str = "",
) -> str:
    page = _page_label(page_n, total_pages)
    extra_part = extra if extra else ""
    return (
        f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(stream)}: {page} — "
        f"ok {elapsed:.1f}s{extra_part}, {page_len:,} this page "
        f"({seen:,}/{total:,}{_progress_pct(seen, total)})"
    )


def _format_push_done(stream: str, total_pushed: int, total_pages: int, elapsed: float) -> str:
    return (
        f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(stream)}: "
        f"done — {total_pushed:,} in {total_pages} pages ({elapsed:.1f}s)"
    )


def _snapshot_kind_stream(kind: str) -> str:
    return _SNAPSHOT_KIND_STREAM.get(kind, kind)

def _pull_failure_message(exc: Exception) -> str:
    msg = str(exc).strip()
    if "routing api" in msg.lower():
        # Check whether the root cause is an invalid / stale agent key,
        # not just a transient routing API failure.
        if "401" in msg or "invalid" in msg.lower() or "unauthorized" in msg.lower():
            from user_messages import MSG_LOGIN
            return (
                f"{msg}\n\nYour agent key was rejected by the server — it may be invalid or stale. "
                f"{MSG_LOGIN} to refresh it."
            )
        from user_messages import MSG_PULL_SKIP_ROUTING

        return f"{msg}\n\nRouting sync failed. {MSG_PULL_SKIP_ROUTING}."
    if "sqlitenomem" in msg.lower() or "out of memory" in msg.lower():
        return (
            f"{msg}\n\nRelay D1 ran out of memory on a large pull page. "
            f"Re-run the same pull command — your cursor should resume from the last successful page. "
            f"(Events are capped at {RELAY_PULL_EVENT_MAX}/page.)"
        )
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        from user_messages import MSG_PULL_PROBE, MSG_PULL_SKIP_SNAPSHOTS

        return (
            f"{msg}\n\nRelay pull HTTP timed out. Events are usually enough: "
            f"{MSG_PULL_SKIP_SNAPSHOTS}, or {MSG_PULL_PROBE.lower()}. "
            "If you need snapshots, retry with a smaller backlog or pull one snapshot kind at a time."
        )
    # Catch invalid / stale agent key errors from the relay pull itself
    # (as opposed to the routing API check above).
    if "invalid agent key" in msg.lower() or ("401" in msg and "unauthorized" in msg.lower()):
        from user_messages import MSG_LOGIN

        return (
            f"{msg}\n\nYour agent key was rejected by the server — it may be invalid or stale. "
            f"{MSG_LOGIN} to refresh it."
        )
    return msg


def format_pull_summary(imported: int, skipped: int, stats: dict) -> str:
    dupes = int(stats.get("skipped_duplicates") or 0)
    filtered = int(stats.get("skipped_filtered") or 0)
    errors = int(stats.get("skipped_errors") or 0)
    conn = get_conn()
    try:
        lead_count = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    finally:
        conn.close()
    lines = [
        f"Imported: {imported} events ({lead_count} total leads).",
        (
            f"Duplicates: {dupes}"
            + (f", filtered: {filtered}" if filtered else "")
            + (f", errors: {errors}" if errors else "")
            + "."
        ),
    ]
    snap_records = int(stats.get("snapshot_records_seen") or 0)
    if snap_records:
        lines.append(
            f"Snapshots: {snap_records} records applied."
        )
    return "\n".join(lines)


def pull_events_org(
    agent_key: str,
    after_id: Optional[int] = None,
    platform: Optional[str] = None,
    *,
    snapshot_after_id: Optional[int] = None,
    snapshot_kind: str = "workspace",
    snapshots_only: bool = False,
    include_pending: bool = False,
    include_queue_resolutions: bool = False,
    limit: Optional[int] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Pull org events from relay (cursor-only: after_id / snapshot_after_id)."""
    params = []
    if limit and limit > 0:
        if snapshots_only:
            cap = _snapshot_pull_limit_for_kind(snapshot_kind, RELAY_PULL_SNAPSHOT_MAX)
        else:
            cap = RELAY_PULL_EVENT_MAX
        params.append(f"limit={min(int(limit), cap)}")
    pull_timeout = timeout if timeout is not None else RELAY_PULL_HTTP_TIMEOUT
    if after_id:
        params.append(f"after_id={after_id}")
    if platform:
        params.append(f"platform={urllib.parse.quote(platform)}")
    if snapshot_after_id:
        params.append(f"snapshot_after_id={snapshot_after_id}")
    if snapshots_only and snapshot_kind:
        params.append(f"snapshot_kind={urllib.parse.quote(snapshot_kind)}")
    if snapshots_only:
        params.append("snapshots_only=1")
    if include_pending:
        params.append("include_pending=1")
    if include_queue_resolutions:
        params.append("include_queue_resolutions=1")
    qs = f"?{'&'.join(params)}" if params else ""
    url = f"{RELAY_URL}/pull{qs}"

    from pipeline import __version__

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Outreach Magic/{__version__}",
            "Authorization": f"Bearer {agent_key}",
        },
    )
    last_error: Optional[dict] = None
    for attempt in range(RELAY_PULL_HTTP_RETRIES + 1):
        try:
            return _relay_http_get_json(req, pull_timeout)
        except TimeoutError as e:
            last_error = {"error": True, "message": str(e)}
            if attempt < RELAY_PULL_HTTP_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return last_error
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            return {"error": True, "status": e.code, "message": body}
        except urllib.error.URLError as e:
            last_error = {"error": True, "message": str(e.reason)}
            if attempt < RELAY_PULL_HTTP_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return last_error
    return last_error or {"error": True, "message": "pull failed"}

def _pull_diagnostics_verdict(stats: dict) -> str:
    if stats.get("cursor_stalled"):
        return "cursor stalled"
    if (stats.get("relay_events_seen") or 0) == 0:
        return "relay empty"
    if (stats.get("imported") or 0) == 0 and (stats.get("skipped_duplicates") or 0) > 0:
        return "relay has events but deduped"
    if stats.get("cursor_advanced"):
        return "cursor advanced"
    return "cursor unchanged"


def print_pull_diagnostics(stats: dict):
    verdict = _pull_diagnostics_verdict(stats)
    print("Pull diagnostics")
    print("---------------")
    print(f"Mode: {stats.get('mode', 'unknown')}")
    print(f"Newest relay_id seen: {stats.get('newest_relay_id_seen') or '-'}")
    print(
        f"Event cursor (last_max_id): {stats.get('pull_after_id_start') or '-'} -> "
        f"{stats.get('pull_after_id_end') or '-'} "
        f"({'advanced' if stats.get('cursor_advanced') else 'unchanged'})"
    )
    print(
        f"Snapshot cursors: {stats.get('snapshot_cursors_start') or '-'} -> "
        f"{stats.get('snapshot_cursors_end') or '-'}"
    )
    print(
        f"Skips: duplicates={stats.get('skipped_duplicates', 0)} "
        f"errors={stats.get('skipped_errors', 0)} "
        f"cloud_skipped={stats.get('skipped_resolved', 0)} "
        f"cloud_assigned={stats.get('assigned_resolved', 0)}"
    )
    if stats.get("cursor_stalled"):
        print("Cursor stall guard: triggered")
    if stats.get("pull_hint"):
        print(f"Hint: {stats['pull_hint']}")
    print(f"Verdict: {verdict}")


def _ingest_relay_page(
    events: list,
    *,
    debug_sentiment: bool = False,
    quiet: bool = True,
    resolution_map: Optional[dict[int, dict]] = None,
    slug_cache: Optional[qres.WorkspaceSlugCache] = None,
    pull_conn: Optional[sqlite3.Connection] = None,
    routing_config: Optional[OrgRoutingConfig] = None,
    ws_slug_map: Optional[dict[str, str]] = None,
    routing_cache: Optional[CampaignRoutingCache] = None,
) -> dict:
    from pipeline import prefetch_relay_ingested, prefetch_ws_idempotency_keys

    imported = skipped = skipped_duplicates = skipped_filtered = skipped_errors = 0
    skipped_resolved = assigned_resolved = 0
    newest_relay_id_seen = 0
    resolutions = resolution_map or {}
    ws_cache = slug_cache or qres.WorkspaceSlugCache()

    if not events:
        return {
            "imported": 0,
            "skipped": 0,
            "skipped_duplicates": 0,
            "skipped_filtered": 0,
            "skipped_errors": 0,
            "skipped_resolved": 0,
            "assigned_resolved": 0,
            "newest_relay_id_seen": 0,
        }

    local_client_id = get_or_create_client_id()
    pending_marks: list[tuple[str, Optional[int]]] = []
    own_page_conn = pull_conn is None
    if own_page_conn:
        from pipeline import db_exists
        from pipeline_migration import init_db

        if not db_exists():
            init_db()
        pull_conn = get_conn()
        apply_bulk_pull_pragmas(pull_conn)
        routing_config = get_org_routing_config(pull_conn, DEFAULT_ORG_ID)
        ws_slug_map = (
            _pull_workspace_slug_map(pull_conn, DEFAULT_ORG_ID)
            if routing_config.mode == WORKSPACE_ROUTING_MULTI
            else {}
        )
        if routing_cache is None and routing_config.mode == WORKSPACE_ROUTING_MULTI:
            routing_cache = CampaignRoutingCache.load(
                pull_conn, DEFAULT_ORG_ID, routing_config,
            )
    dedupe_keys = pull_page_dedupe_keys(events, local_client_id)
    ingested_prefetch = prefetch_relay_ingested(dedupe_keys, conn=pull_conn)
    ws_idempotent_prefetch = prefetch_ws_idempotency_keys(
        pull_conn, DEFAULT_ORG_ID, pull_page_ws_idempotency_keys(events),
    )
    activity_refresh_pairs: set[tuple[int, str]] = set()
    ingest_kw = {
        "debug_sentiment": debug_sentiment,
        "quiet": quiet,
        "defer_mark": True,
        "pending_marks": pending_marks,
        "pull_conn": pull_conn,
        "routing_config": routing_config,
        "ws_slug_map": ws_slug_map or {},
        "routing_cache": routing_cache,
        "ingested_prefetch": ingested_prefetch,
        "ws_idempotent_prefetch": ws_idempotent_prefetch,
        "defer_activity_refresh": True,
        "activity_refresh_pairs": activity_refresh_pairs,
    }

    page_start = time.monotonic()
    try:
        for event in events:
            relay_id = event.get("relay_id")
            if isinstance(relay_id, int) and relay_id > newest_relay_id_seen:
                newest_relay_id_seen = relay_id

            resolution = None
            if isinstance(relay_id, int):
                resolution = resolutions.get(relay_id)
            if resolution:
                if resolution["status"] == "skipped":
                    skipped_resolved += 1
                    continue
                if resolution["status"] == "assigned":
                    ws_id = ws_cache.workspace_id(resolution.get("workspace_slug") or "")
                    if not ws_id:
                        skipped += 1
                        skipped_errors += 1
                        if not quiet:
                            print(
                                f"Warning: assigned resolution for relay {relay_id} "
                                f"but workspace '{resolution.get('workspace_slug')}' not found",
                                file=sys.stderr,
                            )
                        continue
                    if _pull_page_already_ingested(event, ingested_prefetch, local_client_id):
                        skipped += 1
                        skipped_duplicates += 1
                        continue
                    try:
                        ingested = ingest_relay_event(
                            event,
                            force_workspace_id=ws_id,
                            **ingest_kw,
                        )
                    except Exception as exc:
                        if not quiet:
                            print(f"Warning: skipped webhook event {relay_id}: {exc}")
                        skipped += 1
                        skipped_errors += 1
                        continue
                    if ingested is None:
                        skipped += 1
                        skipped_filtered += 1
                    else:
                        imported += 1
                        assigned_resolved += 1
                        if event.get("platform") != "agent":
                            _append_pull_ingest_marks(
                                pending_marks, event, ingested, local_client_id
                            )
                    continue

            if _pull_page_already_ingested(event, ingested_prefetch, local_client_id):
                skipped += 1
                skipped_duplicates += 1
                continue

            try:
                ingested = ingest_relay_event(event, **ingest_kw)
            except Exception as exc:
                if not quiet:
                    print(f"Warning: skipped webhook event {event.get('relay_id') or '?'}: {exc}")
                skipped += 1
                skipped_errors += 1
                continue
            if ingested is None:
                skipped += 1
                skipped_filtered += 1
            else:
                imported += 1
                if event.get("platform") != "agent":
                    _append_pull_ingest_marks(pending_marks, event, ingested, local_client_id)

        for lead_id, workspace_id in activity_refresh_pairs:
            refresh_lead_activity_from_events(pull_conn, lead_id, workspace_id)
        if pending_marks and pull_conn is not None:
            mark_relay_ingested_many(pending_marks, conn=pull_conn, commit=False)
        if pull_conn is not None:
            pull_conn.commit()
    except Exception:
        if pull_conn is not None:
            try:
                pull_conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        elapsed = time.monotonic() - page_start
        if not quiet and elapsed >= 30:
            print(
                f"Slow pull page ingest: {len(events):,} events in {elapsed:.1f}s "
                f"(imported={imported}, skipped_dup={skipped_duplicates})",
                flush=True,
            )
        if own_page_conn and pull_conn is not None:
            end_bulk_pull_session(pull_conn)
            pull_conn.close()

    return {
        "imported": imported,
        "skipped": skipped,
        "skipped_duplicates": skipped_duplicates,
        "skipped_filtered": skipped_filtered,
        "skipped_errors": skipped_errors,
        "skipped_resolved": skipped_resolved,
        "assigned_resolved": assigned_resolved,
        "newest_relay_id_seen": newest_relay_id_seen,
    }


def _begin_pull_ingest_session(
    session: Optional[sqlite3.Connection],
    routing_config: Optional[OrgRoutingConfig],
    ws_slug_map: dict[str, str],
    routing_cache: Optional[CampaignRoutingCache],
) -> tuple[
    sqlite3.Connection,
    OrgRoutingConfig,
    dict[str, str],
    Optional[CampaignRoutingCache],
]:
    """One bulk-pull SQLite session for events and snapshot pages (avoids database is locked)."""
    if session is not None and routing_config is not None:
        return session, routing_config, ws_slug_map, routing_cache
    from pipeline import db_exists
    from pipeline_migration import init_db

    if not db_exists():
        init_db()
    session = get_conn()
    apply_bulk_pull_pragmas(session)
    routing_config = get_org_routing_config(session, DEFAULT_ORG_ID)
    ws_slug_map = {}
    routing_cache = None
    if routing_config.mode == WORKSPACE_ROUTING_MULTI:
        ws_slug_map = _pull_workspace_slug_map(session, DEFAULT_ORG_ID)
        routing_cache = CampaignRoutingCache.load(
            session, DEFAULT_ORG_ID, routing_config,
        )
    return session, routing_config, ws_slug_map, routing_cache


def _relay_pull_phases(full: bool, do_events: bool, kinds: frozenset) -> tuple[str, ...]:
    """Order relay pull phases.

    Full rebuild pulls snapshots before events so agent event_log replay can attach
    to leads that only exist after lead_core / lead_workspace snapshots ingest.
    """
    has_snapshots = bool(kinds & {"company", "core", "workspace"})
    if full and do_events and has_snapshots:
        return ("snapshots", "events")
    phases: list[str] = []
    if do_events:
        phases.append("events")
    if has_snapshots:
        phases.append("snapshots")
    return tuple(phases)


def sync_from_relay_org(
    agent_key: str,
    after_id: Optional[int] = None,
    full: bool = False,
    debug_sentiment: bool = False,
    quiet: bool = False,
    stats: Optional[dict] = None,
    *,
    skip_routing_sync: bool = False,
    pull_kinds: Optional[frozenset[str]] = None,
    skip_snapshots: bool = False,
) -> tuple[int, int]:
    """Import relay events for the org. Cursors: last_max_id (events), snapshot cursors (core/workspace/company)."""
    from pipeline import pull_events_org, _ingest_relay_page, _snapshot_pending_count, RELAY_PULL_EVENT_MAX

    kinds = pull_kinds or PULL_KINDS_ALL
    if skip_snapshots:
        kinds = frozenset(k for k in kinds if k == "events")
    do_events = "events" in kinds
    do_snapshots = bool(kinds & {"company", "core", "workspace"})
    needs_routing_sync = do_events or (full and do_snapshots)
    if not skip_routing_sync and needs_routing_sync:
        try:
            from pipeline import maybe_sync_routing_from_cloud, maybe_sync_agent_secrets_from_cloud

            maybe_sync_routing_from_cloud(quiet=quiet)
        except RuntimeError as exc:
            raise RuntimeError(_pull_failure_message(exc)) from exc
        try:
            maybe_sync_agent_secrets_from_cloud(quiet=quiet)
        except Exception:
            if not quiet:
                print("API key sync skipped (non-fatal).", flush=True)
    elif not quiet and needs_routing_sync:
        print("Skipped routing config sync (--skip-routing-sync).", flush=True)
    if not quiet:
        if do_events:
            print("Contacting relay to pull new events...", flush=True)
        elif kinds & {"company", "core", "workspace"}:
            print(f"Contacting relay to pull snapshots ({', '.join(sorted(kinds))})...", flush=True)

    imported = skipped = 0
    skipped_duplicates = skipped_filtered = skipped_errors = 0
    relay_events_seen = 0
    newest_relay_id_seen = 0
    cursor_stalled = False
    event_pages = 0
    snap_pages = 0
    snap_total = 0

    page_after_id = 0 if full else int(after_id if after_id is not None else (get_last_max_id() or 0))
    initial_after_id = page_after_id
    snapshot_cursors = {
        kind: 0 if full else get_snapshot_cursor(kind)
        for kind in ("core", "workspace", "company")
    }
    snapshot_cursors_start = dict(snapshot_cursors)

    pending_events: Optional[int] = None
    est_event_pages: Optional[int] = None
    pending_snapshots: Optional[int] = None
    est_snap_pages: Optional[int] = None
    resolution_map: dict[int, dict] = {}
    slug_cache = qres.WorkspaceSlugCache()
    skipped_resolved = assigned_resolved = 0

    # Always cap event pulls (D1 + local ingest); --full only resets after_id to 0.
    event_pull_limit = RELAY_PULL_EVENT_MAX
    snap_pull_limit = RELAY_PULL_SNAPSHOT_MAX if full else RELAY_PULL_PAGE_SIZE
    pull_timeout = RELAY_PULL_HTTP_TIMEOUT
    snapshot_pull_timeout = RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT

    if not quiet and do_events:
        snap_hint = (
            f", snapshots up to {snap_pull_limit}/page"
            if kinds & {"core", "workspace"}
            else ""
        )
        print(
            f"Pulling from relay (events: {event_pull_limit}/page{snap_hint})...",
            flush=True,
        )
    elif not quiet and kinds & {"core", "workspace"}:
        print("Pulling from relay (snapshots only)...", flush=True)

    pull_session: Optional[sqlite3.Connection] = None
    pull_routing_config: Optional[OrgRoutingConfig] = None
    pull_ws_slug_map: dict[str, str] = {}
    pull_routing_cache: Optional[CampaignRoutingCache] = None

    pull_phases = _relay_pull_phases(full, do_events, kinds)
    event_pull_started_at: Optional[float] = None
    if not quiet and pull_phases and pull_phases[0] == "snapshots":
        print(
            f"[{_progress_clock()}] Full pull: lead snapshots before event replay...",
            flush=True,
        )

    try:
        for _pull_phase in pull_phases:
            if _pull_phase == "events":
                while True:
                    event_pages += 1
                    request_limit = event_pull_limit
                    result = pull_events_org(
                        agent_key,
                        after_id=page_after_id or None,
                        include_pending=event_pages == 1,
                        include_queue_resolutions=event_pages == 1,
                        limit=request_limit,
                        timeout=pull_timeout,
                    )
                    if result.get("error"):
                        raise RuntimeError(result.get("message", "pull failed"))

                    if event_pages == 1:
                        resolution_map = qres.parse_queue_resolutions(result.get("queue_resolutions"))

                    events = result.get("events") or []
                    if not events:
                        break

                    if event_pages == 1 and result.get("pending_event_count") is not None:
                        pending_events = int(result["pending_event_count"])
                        est_event_pages = _estimate_relay_pages(pending_events, event_pull_limit)
                        if not quiet and pending_events > 0:
                            print(
                                _format_pull_pending_banner(
                                    _RELAY_STREAM_EVENT,
                                    pending_events,
                                    est_event_pages,
                                    event_pull_limit,
                                ),
                                flush=True,
                            )
                    elif (
                        event_pages == 1
                        and pending_events is None
                        and not quiet
                        and len(events) >= event_pull_limit
                    ):
                        print(
                            f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(_RELAY_STREAM_EVENT)}: "
                            f"first page has {len(events):,} records "
                            f"(@ {event_pull_limit}/page — more pages follow)...",
                            flush=True,
                        )

                    relay_events_seen += len(events)
                    if not quiet:
                        if event_pull_started_at is None:
                            event_pull_started_at = time.monotonic()
                        eta_seconds = None
                        if pending_events and pending_events > 0:
                            eta_seconds = _progress_eta_seconds(
                                relay_events_seen,
                                pending_events,
                                time.monotonic() - event_pull_started_at,
                            )
                        print(
                            _format_pull_progress(
                                _RELAY_STREAM_EVENT,
                                page_n=event_pages,
                                total_pages=est_event_pages,
                                page_len=len(events),
                                seen=relay_events_seen,
                                total=pending_events,
                                more_follow=len(events) >= request_limit and not est_event_pages,
                                eta_seconds=eta_seconds,
                            ),
                            flush=True,
                        )

                    pull_session, pull_routing_config, pull_ws_slug_map, pull_routing_cache = (
                        _begin_pull_ingest_session(
                            pull_session,
                            pull_routing_config,
                            pull_ws_slug_map,
                            pull_routing_cache,
                        )
                    )

                    ingest_started = time.monotonic()
                    batch = _ingest_relay_page(
                        events,
                        debug_sentiment=debug_sentiment,
                        quiet=quiet,
                        resolution_map=resolution_map,
                        slug_cache=slug_cache,
                        pull_conn=pull_session,
                        routing_config=pull_routing_config,
                        ws_slug_map=pull_ws_slug_map,
                        routing_cache=pull_routing_cache,
                    )
                    ingest_elapsed = time.monotonic() - ingest_started
                    if not quiet:
                        print(
                            f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(_RELAY_STREAM_EVENT)}: "
                            f"ingest {ingest_elapsed:.1f}s "
                            f"(+{batch['imported']} new, {batch['skipped_duplicates']} dupes, "
                            f"{batch['skipped_filtered']} filtered, {batch['skipped_errors']} errors)",
                            flush=True,
                        )
                    imported += batch["imported"]
                    skipped += batch["skipped"]
                    skipped_duplicates += batch["skipped_duplicates"]
                    skipped_filtered += batch["skipped_filtered"]
                    skipped_errors += batch["skipped_errors"]
                    skipped_resolved += batch.get("skipped_resolved", 0)
                    assigned_resolved += batch.get("assigned_resolved", 0)
                    newest_relay_id_seen = max(newest_relay_id_seen, batch["newest_relay_id_seen"])

                    next_after_id = int(result.get("max_id") or page_after_id)
                    if len(events) >= request_limit and next_after_id <= page_after_id:
                        cursor_stalled = True
                        break
                    page_after_id = next_after_id
                    if page_after_id:
                        set_last_max_id(page_after_id)
                    has_more = result.get("has_more_events")
                    effective_limit = int(result.get("pull_limit") or request_limit)
                    if (
                        has_more is False
                        and len(events) >= effective_limit
                        and effective_limit < request_limit
                    ):
                        # Old clients may request 5k; worker caps events at RELAY_PULL_EVENT_MAX.
                        has_more = True
                    if len(events) < effective_limit or has_more is False:
                        break

            elif _pull_phase == "snapshots":
                for snap_kind in ("company", "core", "workspace"):
                    if snap_kind not in kinds:
                        continue
                    kind_pages = 0
                    kind_seen = 0
                    stream = _snapshot_kind_stream(snap_kind)
                    pending_snapshots = None
                    est_snap_pages = None
                    kind_limit = _snapshot_pull_limit_for_kind(snap_kind, snap_pull_limit)
                    if not quiet:
                        pending_snapshots = _snapshot_pending_count(
                            agent_key,
                            snap_kind,
                            int(snapshot_cursors[snap_kind] or 0),
                            timeout=snapshot_pull_timeout,
                        )
                        if pending_snapshots is not None and pending_snapshots > 0:
                            if pending_snapshots >= RELAY_BULK_THRESHOLD:
                                snap_pull_limit = RELAY_PULL_SNAPSHOT_MAX
                                kind_limit = _snapshot_pull_limit_for_kind(snap_kind, snap_pull_limit)
                            est_snap_pages = _estimate_relay_pages(pending_snapshots, kind_limit)
                            print(
                                _format_pull_pending_banner(
                                    stream,
                                    pending_snapshots,
                                    est_snap_pages,
                                    kind_limit,
                                ),
                                flush=True,
                            )
                    while True:
                        snap_pages += 1
                        kind_pages += 1
                        if not quiet:
                            print(
                                f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: "
                                f"fetching p{kind_pages} (@ {kind_limit}/p)...",
                                flush=True,
                            )
                        snap_result = pull_events_org(
                            agent_key,
                            snapshot_after_id=snapshot_cursors[snap_kind] or None,
                            snapshot_kind=snap_kind,
                            snapshots_only=True,
                            include_pending=False,
                            limit=kind_limit,
                            timeout=snapshot_pull_timeout,
                        )
                        if snap_result.get("error"):
                            raise RuntimeError(snap_result.get("message", "snapshot pull failed"))
                        snap_events = snap_result.get("events") or []
                        if not snap_events:
                            snap_pages -= 1
                            kind_pages -= 1
                            break

                        kind_seen += len(snap_events)
                        snap_total += len(snap_events)
                        if not quiet:
                            snap_total_pages = est_snap_pages
                            if snap_total_pages is None and pending_snapshots and pending_snapshots > 0:
                                snap_total_pages = _estimate_relay_pages(pending_snapshots, kind_limit)
                            elif snap_total_pages is None and len(snap_events) >= kind_limit:
                                snap_total_pages = kind_pages + 1
                            elif snap_total_pages is not None and len(snap_events) >= kind_limit:
                                snap_total_pages = max(snap_total_pages, kind_pages + 1)
                            if pending_snapshots and pending_snapshots > 0:
                                print(
                                    _format_pull_progress(
                                        stream,
                                        page_n=kind_pages,
                                        total_pages=snap_total_pages,
                                        page_len=len(snap_events),
                                        seen=kind_seen,
                                        total=pending_snapshots,
                                        more_follow=len(snap_events) >= kind_limit and not snap_total_pages,
                                    ),
                                    flush=True,
                                )
                            elif kind_seen > 0:
                                print(
                                    _format_pull_progress(
                                        stream,
                                        page_n=kind_pages,
                                        total_pages=snap_total_pages,
                                        page_len=len(snap_events),
                                        seen=kind_seen,
                                        more_follow=len(snap_events) >= kind_limit and not snap_total_pages,
                                        total_only=not snap_total_pages,
                                    ),
                                    flush=True,
                                )
                        pull_session, pull_routing_config, pull_ws_slug_map, pull_routing_cache = (
                            _begin_pull_ingest_session(
                                pull_session,
                                pull_routing_config,
                                pull_ws_slug_map,
                                pull_routing_cache,
                            )
                        )
                        ingest_started = time.monotonic()
                        batch = _ingest_relay_page(
                            snap_events,
                            debug_sentiment=debug_sentiment,
                            quiet=quiet,
                            pull_conn=pull_session,
                            routing_config=pull_routing_config,
                            ws_slug_map=pull_ws_slug_map,
                            routing_cache=pull_routing_cache,
                        )
                        ingest_elapsed = time.monotonic() - ingest_started
                        imported += batch["imported"]
                        skipped += batch["skipped"]
                        skipped_duplicates += batch["skipped_duplicates"]
                        skipped_filtered += batch["skipped_filtered"]
                        skipped_errors += batch["skipped_errors"]
                        newest_relay_id_seen = max(newest_relay_id_seen, batch["newest_relay_id_seen"])
                        prev_snap_cursor = snapshot_cursors[snap_kind]
                        next_snap_cursor = int(snap_result.get("max_snapshot_id") or 0)
                        if (
                            len(snap_events) >= kind_limit
                            and next_snap_cursor <= prev_snap_cursor
                        ):
                            cursor_stalled = True
                            if not quiet:
                                print(
                                    f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: "
                                    f"cursor stalled at snapshot_id={prev_snap_cursor} — stopping",
                                    flush=True,
                                )
                            break
                        snapshot_cursors[snap_kind] = next_snap_cursor
                        if snapshot_cursors[snap_kind]:
                            set_snapshot_cursor(snapshot_cursors[snap_kind], snap_kind)
                        if (
                            not quiet
                            and batch["imported"] == 0
                            and batch["skipped_duplicates"] == len(snap_events)
                            and len(snap_events) > 0
                        ):
                            print(
                                f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: "
                                f"page all duplicates locally — cursor still advanced to {next_snap_cursor}",
                                flush=True,
                            )
                        if not quiet:
                            print(
                                f"[{_progress_clock()}] {_ARROW_PULL} {_stream_pad(stream)}: "
                                f"ingest {ingest_elapsed:.1f}s "
                                f"(+{batch['imported']} new, {batch['skipped_duplicates']} dupes, "
                                f"{batch['skipped_filtered']} filtered, {batch['skipped_errors']} errors)",
                                flush=True,
                            )
                        if not snap_result.get("has_more_snapshots"):
                            break
    finally:
        if pull_session is not None:
            end_bulk_pull_session(pull_session)
            pull_session.close()

    if page_after_id:
        set_last_max_id(page_after_id)
    for kind, cursor in snapshot_cursors.items():
        if cursor:
            set_snapshot_cursor(cursor, kind)
    set_last_pull(datetime.now(timezone.utc).isoformat())

    pull_hint = None
    if full and imported > 0:
        set_last_sync(datetime.now(timezone.utc).isoformat())
    if not full and relay_events_seen == 0 and not cursor_stalled and page_after_id == initial_after_id:
        pull_hint = "no new webhook events — run `pull --full` once or clear last_max_id in config"

    cursor_advanced = bool(page_after_id > initial_after_id)
    if stats is not None:
        stats.update({
            "mode": "full" if full else "incremental",
            "pull_phases": list(pull_phases),
            "config_last_max_id_before": after_id,
            "pull_after_id_start": initial_after_id,
            "pull_after_id_end": page_after_id,
            "snapshot_cursors_start": snapshot_cursors_start,
            "snapshot_cursors_end": snapshot_cursors,
            "pull_hint": pull_hint,
            "cursor_advanced": cursor_advanced,
            "cursor_stalled": cursor_stalled,
            "event_pages": event_pages,
            "snapshot_pages": snap_pages,
            "pages": event_pages + snap_pages,
            "relay_events_seen": relay_events_seen,
            "snapshot_records_seen": snap_total,
            "pending_events": pending_events,
            "pending_snapshots": pending_snapshots,
            "newest_relay_id_seen": newest_relay_id_seen or None,
            "imported": imported,
            "skipped_duplicates": skipped_duplicates,
            "skipped_filtered": skipped_filtered,
            "skipped_errors": skipped_errors,
            "skipped_resolved": skipped_resolved,
            "assigned_resolved": assigned_resolved,
            "resolution_count": len(resolution_map),
            "skipped_total": skipped,
            "verdict": _pull_diagnostics_verdict({
                "cursor_stalled": cursor_stalled,
                "relay_events_seen": relay_events_seen,
                "imported": imported,
                "skipped_duplicates": skipped_duplicates,
                "cursor_advanced": cursor_advanced,
            }),
        })
    if not quiet:
        from pipeline import print_quarantine_guidance

        print_quarantine_guidance()
    return imported, skipped


REFRESH_WARNING = """
⚠ LOCAL DATABASE REFRESH — destructive, use rarely

This will:
  1. Push pending local changes to the relay (sync) unless you pass --skip-sync
  2. Sync workspace + campaign routing from the cloud and print a routing summary
  3. Back up your local SQLite file
  4. Delete the local database and re-import from api.outreachmagic.io
     (lead snapshots first, then events — so event history can replay)

You will lose any local-only data that was NOT synced to the relay.
pull --full alone does NOT refresh — it still skips rows already in relay_ingested.

Ask Outreach Magic to refresh the local database (confirm when prompted)

If refresh times out during the relay pull, your previous database is kept intact
(staging pull). Resume with a full sync when ready.

If the database is empty or corrupted after a failed refresh:
  Ask Outreach Magic to restore from backup
""".strip()


def _refresh_staging_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.stem}.refresh-staging.db")


def _remove_staging_db(staging_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(staging_path) + suffix) if suffix else staging_path
        if p.exists():
            p.unlink()


def list_database_backups(db_dir: Optional[Path] = None) -> list[Path]:
    """Newest-first backup files created by refresh or restore."""
    directory = db_dir or get_db_path().parent
    if not directory.is_dir():
        return []
    seen: set[Path] = set()
    ordered: list[tuple[float, Path]] = []
    for pattern in ("*.backup-*.db", "*.pre-restore-*.db"):
        for path in directory.glob(pattern):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                ordered.append((path.stat().st_mtime, path))
            except OSError:
                continue
    ordered.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in ordered]


def restore_local_database(
    *,
    source: Optional[str] = None,
    latest: bool = False,
    yes: bool = False,
) -> dict:
    """Replace the live database from a backup file."""
    db_path = get_db_path()
    db_dir = db_path.parent
    backups = list_database_backups(db_dir)

    if latest:
        if not backups:
            return {
                "status": "error",
                "error": "no_backups",
                "message": (
                    f"No backup files found in {db_dir}. "
                    "Refresh creates outreachmagic.backup-<timestamp>.db automatically."
                ),
            }
        backup_path = backups[0]
    elif source:
        backup_path = Path(source).expanduser()
        if not backup_path.is_file():
            return {
                "status": "error",
                "error": "backup_not_found",
                "message": f"Backup not found: {backup_path}",
            }
    else:
        return {
            "status": "error",
            "error": "source_required",
            "message": "Pass --latest or --from <backup.db>",
            "backups": [str(p) for p in backups[:10]],
        }

    if not yes:
        from user_messages import MSG_RESTORE_LATEST_YES

        return {
            "status": "error",
            "error": "confirmation_required",
            "message": (
                f"This will replace {db_path} with backup:\n  {backup_path}\n"
                f"{MSG_RESTORE_LATEST_YES}"
            ),
            "backup": str(backup_path),
        }

    db_dir.mkdir(parents=True, exist_ok=True)
    pre_restore = db_path.with_name(
        f"{db_path.stem}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    )
    if db_path.exists():
        shutil.copy2(db_path, pre_restore)

    for suffix in ("", "-wal", "-shm"):
        live = Path(str(db_path) + suffix) if suffix else db_path
        if live.exists():
            live.unlink()

    shutil.copy2(backup_path, db_path)
    _chmod_best_effort(db_path, 0o600)

    if not database_has_schema():
        return {
            "status": "error",
            "error": "backup_invalid",
            "message": f"Backup restored but schema check failed: {backup_path}",
            "pre_restore_copy": str(pre_restore) if pre_restore.exists() else None,
        }

    return {
        "status": "ok",
        "restored_from": str(backup_path),
        "database": str(db_path),
        "pre_restore_copy": str(pre_restore) if pre_restore.exists() else None,
        "message": f"Restored database from {backup_path.name}",
    }


def cmd_restore(args) -> None:
    result = restore_local_database(
        source=getattr(args, "from_path", None),
        latest=getattr(args, "latest", False),
        yes=getattr(args, "yes", False),
    )
    if getattr(args, "list", False):
        backups = list_database_backups()
        payload = {
            "database": str(get_db_path()),
            "backups": [
                {"path": str(p), "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()}
                for p in backups
            ],
        }
        print(json.dumps(payload, indent=2))
        return
    if result.get("status") == "error" and result.get("error") == "confirmation_required":
        print(result["message"], file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))
    if result.get("status") != "ok":
        sys.exit(1)


def _atomic_refresh_swap(live_path: Path, staging_path: Path) -> None:
    """Replace live DB with a successfully pulled staging file."""
    old_path = live_path.with_name(f"{live_path.stem}.pre-refresh.db")
    if old_path.exists():
        old_path.unlink()
    if live_path.exists():
        live_path.rename(old_path)
    staging_path.rename(live_path)
    for suffix in ("-wal", "-shm"):
        for base in (old_path, staging_path):
            sidecar = Path(str(base) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    if old_path.exists():
        old_path.unlink()


def _clear_pull_cursors() -> None:
    from pipeline import load_config, save_config

    cfg = load_config()
    cfg.pop("last_pull", None)
    cfg.pop("last_max_id", None)
    clear_snapshot_cursors()
    save_config(cfg)


def refresh_local_database(
    *,
    yes: bool = False,
    skip_sync: bool = False,
    backup: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
    quiet: bool = False,
) -> dict:
    """Wipe local SQLite and rebuild from the relay archive (sync first by default)."""
    from pipeline import load_config, get_agent_key, sync_from_relay_org

    if not yes:
        return {
            "status": "error",
            "error": "confirmation_required",
            "message": REFRESH_WARNING,
        }

    result: dict = {"status": "ok", "steps": []}

    from pipeline import sync_all, get_sync_status, maybe_sync_routing_from_cloud, maybe_sync_agent_secrets_from_cloud, get_routing_config_summary, format_routing_refresh_summary

    if not skip_sync:
        tok = get_agent_key()
        if not tok:
            return {
                "status": "error",
                "error": "no_agent_key",
                "message": "Agent key required. Run login first, or pass --skip-sync (not recommended).",
            }
        if not routing_cloud.cloud_routing_enabled(load_config, tok):
            return {
                "status": "error",
                "error": "cloud_not_configured",
                "message": "Cloud routing not configured. Run login first.",
            }
        sync_result = sync_all(org_id=org_id)
        result["sync"] = sync_result
        result["steps"].append("sync")
        status = get_sync_status(org_id)
        pending = int(status.get("pending_total") or 0)
        if pending > 0:
            return {
                "status": "error",
                "error": "sync_incomplete",
                "message": (
                    f"Still {pending} item(s) pending after sync. "
                    "Resolve sync issues or re-run with --skip-sync (you may lose unsynced data)."
                ),
                "pending": status,
                "sync": sync_result,
            }
    else:
        result["steps"].append("sync_skipped")

    try:
        if maybe_sync_routing_from_cloud(quiet=quiet):
            result["steps"].append("pre_wipe_routing_sync")
        else:
            result["steps"].append("pre_wipe_routing_sync_skipped")
        routing_summary = get_routing_config_summary(org_id)
        result["routing_summary"] = routing_summary
        if not quiet:
            print(format_routing_refresh_summary(routing_summary), flush=True)
    except RuntimeError as exc:
        return {
            **result,
            "status": "error",
            "error": "pre_wipe_routing_sync_failed",
            "message": (
                f"Could not verify campaign maps from cloud before refresh: {exc}\n"
                "Fix login/routing, or abort refresh."
            ),
        }

    db_path = get_db_path()
    backup_path = Path(backup).expanduser() if backup else db_path.with_suffix(
        f".backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    )
    if db_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup_path)
        result["backup"] = str(backup_path)
        result["steps"].append("backup")
    else:
        result["steps"].append("no_existing_db")

    staging_path = _refresh_staging_path(db_path)
    _remove_staging_db(staging_path)
    result["staging"] = str(staging_path)
    result["steps"].append("staging_prepare")

    imported = 0
    skipped = 0
    staging_ready = False
    set_db_path_override(staging_path)
    try:
        from pipeline_migration import init_db

        init_db()
        result["steps"].append("staging_init")

        try:
            if maybe_sync_routing_from_cloud(quiet=quiet):
                result["steps"].append("routing_sync")
            else:
                result["steps"].append("routing_sync_skipped")
        except RuntimeError as exc:
            return {
                **result,
                "status": "error",
                "error": "routing_sync_failed",
                "message": (
                    f"Could not load campaign maps from cloud before re-import: {exc}\n"
                    "Your previous database was not modified."
                ),
            }
        try:
            maybe_sync_agent_secrets_from_cloud(quiet=quiet)
        except Exception:
            pass

        agent_key = get_agent_key()
        if not agent_key:
            return {
                **result,
                "status": "error",
                "error": "no_agent_key",
                "message": "No agent key to pull from relay. Run login, then pull --full.",
            }

        try:
            imported, skipped = sync_from_relay_org(
                agent_key,
                full=True,
                quiet=quiet,
            )
        except RuntimeError as exc:
            from user_messages import MSG_RESTORE_LATEST_YES

            return {
                **result,
                "status": "error",
                "error": "pull_failed",
                "message": (
                    f"{exc}\n\nYour previous database was not modified. "
                    f"Backup: {result.get('backup', '(none)')}\n"
                    f"{MSG_RESTORE_LATEST_YES}"
                ),
            }
        staging_ready = True
    finally:
        set_db_path_override(None)
        if not staging_ready:
            _remove_staging_db(staging_path)

    try:
        _atomic_refresh_swap(db_path, staging_path)
        result["steps"].append("staging_swap")
    except OSError as exc:
        return {
            **result,
            "status": "error",
            "error": "staging_swap_failed",
            "message": (
                f"Pull completed but could not swap databases: {exc}\n"
                f"Staging file preserved at: {staging_path}\n"
                f"Backup: {result.get('backup', '(none)')}"
            ),
        }

    _clear_pull_cursors()
    result["steps"].append("clear_pull_cursors")

    result["imported"] = imported
    result["skipped"] = skipped
    result["steps"].append("pull_full")
    result["message"] = (
        f"Refresh complete. Imported {imported} events, skipped {skipped} already-processed. "
        f"Backup: {result.get('backup', '(none)')}"
    )
    return result


def cmd_refresh(args) -> None:
    result = refresh_local_database(
        yes=getattr(args, "yes", False),
        skip_sync=getattr(args, "skip_sync", False),
        backup=getattr(args, "backup", None),
        quiet=False,
    )
    if result.get("status") == "error" and result.get("error") == "confirmation_required":
        print(result["message"])
        sys.exit(1)
    print(json.dumps(result, indent=2))
    if result.get("status") != "ok":
        sys.exit(1)


def login(
    platform: Optional[str] = None,
    *,
    generate_url: bool = False,
    claim_token: bool = False,
    device_code: Optional[str] = None,
    wait_seconds: int = 30,
    force: bool = False,
):
    """Connect this machine via browser device authorization (GitHub CLI-style)."""
    from pipeline import pull_events_org, _save_agent_key_and_validate

    if not force and not generate_url and not claim_token:
        existing = get_agent_key()
        if existing and existing.startswith("om_agent_"):
            print("Agent key already configured — validating...")
            result = pull_events_org(existing)
            if not result.get("error"):
                org_id = result.get("organization_id", "")
                count = result.get("count", 0)
                print(f"Already connected to org {org_id} — {count} events available.")
                print("Use `pipeline.py login --force` to re-authenticate via browser.")
                return
            status = str(result.get("status", ""))
            message = str(result.get("message", ""))
            if "401" not in status and "Invalid" not in message and "revoked" not in message.lower():
                print(f"Warning: could not reach relay ({message}). Key kept — will retry on pull.")
                return
            print("Stored agent key is invalid — starting device login...")
    try:
        import device_login
    except ModuleNotFoundError:
        # Allow `pipeline.py login` to work even when cwd/import paths differ.
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import device_login

    if generate_url and claim_token:
        print("Choose one mode: either --generate-url or --claim-token.")
        sys.exit(1)

    if generate_url:
        try:
            flow = device_login.start_device_authorization(
                load_config,
                platform=platform,
                client_id=get_or_create_client_id(),
            )
        except RuntimeError as exc:
            print(f"\nLogin failed: {exc}")
            sys.exit(1)
        print(f"OUTREACHMAGIC_URL={flow['connect_url']}")
        print(f"OUTREACHMAGIC_CODE={flow['user_code']}")
        print(f"OUTREACHMAGIC_DEVICE_CODE={flow['device_code']}")
        print(f"OUTREACHMAGIC_EXPIRES_IN={flow['expires_in']}")
        return

    if claim_token:
        if not device_code:
            print("Missing required flag: --device-code")
            sys.exit(1)
        try:
            claim = device_login.claim_device_token(
                routing_cloud.get_api_base(load_config),
                device_code=device_code,
                wait_seconds=max(0, int(wait_seconds)),
                interval=5,
            )
        except RuntimeError as exc:
            print(f"\nLogin failed: {exc}")
            sys.exit(1)

        status = str(claim.get("status") or "pending")
        if status == "success":
            cfg_before = load_config()
            reconnect = bool(cfg_before.get("organization_id") or get_last_max_id())
            _save_agent_key_and_validate(
                str(claim.get("access_token") or ""),
                reconnect=reconnect,
            )
            print("STATUS=success")
            return
        if status == "account_revoked":
            _set_account_access_revoked(True)
            print("STATUS=account_revoked")
            print(_account_error_message())
            return
        if status == "pending":
            print("STATUS=pending")
            return
        print(f"STATUS={status}")
        sys.exit(1)

    cfg_before = load_config()
    reconnect = bool(cfg_before.get("organization_id") or get_last_max_id())

    try:
        agent_key = device_login.run_device_login(
            load_config,
            platform=platform,
            client_id=get_or_create_client_id(),
        )
    except RuntimeError as exc:
        if str(exc) == "account_revoked":
            _set_account_access_revoked(True)
            print(_account_error_message())
            sys.exit(0)
        print(f"\nLogin failed: {exc}")
        sys.exit(1)
    _set_account_access_revoked(False)
    _save_agent_key_and_validate(agent_key, reconnect=reconnect)


def _account_error_message() -> str:
    from user_messages import MSG_ACCOUNT_ERROR

    return MSG_ACCOUNT_ERROR


def _set_account_access_revoked(revoked: bool) -> None:
    from pipeline import load_config, save_config

    cfg = load_config()
    if revoked:
        cfg["account_access_revoked"] = True
    else:
        cfg.pop("account_access_revoked", None)
    save_config(cfg)


def _account_access_revoked() -> bool:
    from pipeline import load_config

    return bool(load_config().get("account_access_revoked"))


def _check_account_access_revoked() -> bool:
    """Print guidance and return True if this machine recorded an account access error."""
    if not _account_access_revoked():
        return False
    from user_messages import MSG_ACCOUNT_ERROR

    print(MSG_ACCOUNT_ERROR)
    return True


def logout():
    from pipeline import load_config, save_config

    cfg = load_config()
    removed = False
    if cfg.pop("agent_key", None):
        removed = True
    if cfg.pop("token", None):
        removed = True
    save_config(cfg)
    if removed:
        print("Logged out. Cleared local agent credentials.")
    else:
        print("No local agent credentials found.")


def resolve_share_email(explicit: Optional[str] = None) -> Optional[str]:
    """Default Google Sheet share target: CLI flag → config → live status API."""
    from pipeline import connections_cloud, get_agent_key, load_config, routing_cloud

    if explicit and str(explicit).strip():
        return str(explicit).strip()
    cfg = load_config()
    stored = (cfg.get("account_email") or "").strip()
    if stored:
        return stored
    agent_key = get_agent_key()
    if not agent_key:
        return None
    try:
        api_base = routing_cloud.get_api_base(load_config)
        data = connections_cloud.fetch_status(api_base, agent_key)
        _persist_account_identity_from_status(data)
        email = (
            data.get("shareEmail") or data.get("accountEmail") or ""
        ).strip()
        return email or None
    except Exception:
        return None


def require_share_email_for_export(explicit: Optional[str] = None) -> str:
    """Resolve share email or exit with a clear error (avoids backend 500s)."""
    email = resolve_share_email(explicit)
    if email:
        return email
    print(
        json.dumps(
            {
                "error": (
                    "share_email required — pass --share-email, --anyone-with-link, or configure org owner in portal. "
                    "Tip: pipeline.py whoami --json → share_email"
                )
            }
        )
    )
    sys.exit(1)


def resolve_sheets_export_access(args) -> tuple[Optional[str], bool]:
    """Return (share_email, public_link) for sheets/review lead export."""
    public = bool(
        getattr(args, "anyone_with_link", False) or getattr(args, "public", False)
    )
    if public:
        return None, True
    explicit = getattr(args, "share_email", None)
    if explicit and str(explicit).strip():
        return str(explicit).strip(), False
    email = resolve_share_email(None)
    if email:
        return email, False
    print(
        json.dumps(
            {
                "error": (
                    "share_email required — pass --share-email, --anyone-with-link, or configure org owner. "
                    "Tip: pipeline.py whoami --json → share_email"
                ),
                "hint": "Use --anyone-with-link for unlisted URL edit access (no email delivery).",
            }
        )
    )
    sys.exit(1)


def _persist_account_identity_from_status(data: dict) -> None:
    """Store account email / org from agent status API when available."""
    from pipeline import load_config, save_config

    email = (data.get("accountEmail") or data.get("account_email") or "").strip()
    org_id = str(data.get("organizationId") or data.get("organization_id") or "").strip()
    cfg = load_config()
    changed = False
    if email and cfg.get("account_email") != email:
        cfg["account_email"] = email
        changed = True
    if org_id and cfg.get("organization_id") != org_id:
        cfg["organization_id"] = org_id
        changed = True
    if changed:
        save_config(cfg)


def cmd_whoami(*, json_output: bool = False) -> None:
    """Print connected account identity for agents."""
    from pipeline import get_agent_key, load_config

    if _account_access_revoked():
        payload = {
            "status": "access_revoked",
            "access_revoked": True,
            "email": load_config().get("account_email"),
            "org_id": load_config().get("organization_id"),
            "plan": None,
        }
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            print(_account_error_message())
        return

    agent_key = get_agent_key()
    if not agent_key:
        payload = {"status": "not_connected", "access_revoked": False}
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            from user_messages import MSG_NO_AGENT_KEY
            print(MSG_NO_AGENT_KEY)
        sys.exit(1)

    api_base = routing_cloud.get_api_base(load_config)
    try:
        data = connections_cloud.fetch_status(api_base, agent_key)
    except RuntimeError as exc:
        if json_output:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Could not fetch account: {exc}")
        sys.exit(1)

    _persist_account_identity_from_status(data)
    cfg = load_config()
    payload = {
        "status": "connected",
        "access_revoked": False,
        "email": data.get("accountEmail") or cfg.get("account_email"),
        "org_id": data.get("organizationId") or cfg.get("organization_id"),
        "plan": data.get("plan"),
        "share_email": data.get("shareEmail") or data.get("accountEmail") or cfg.get("account_email"),
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Email: {payload.get('email') or '(unknown)'}")
        print(f"Org:   {payload.get('org_id') or '(unknown)'}")
        print(f"Plan:  {(payload.get('plan') or 'free').capitalize()}")


def _save_agent_key_and_validate(agent_key: str, *, reconnect: bool = False):
    from pipeline import pull_events_org

    if not agent_key.startswith("om_agent_"):
        print("Invalid key format. Agent keys start with 'om_agent_'.")
        sys.exit(1)

    cfg = load_config()
    cfg["agent_key"] = agent_key
    cfg.pop("token", None)
    # Persist data_root so secondary install copies resolve to the same DB/config.
    cfg["data_root"] = str(get_data_root())
    save_config(cfg)

    print("\nValidating key...")
    result = pull_events_org(agent_key)
    if result.get("error"):
        status = result.get("status", "")
        message = result.get("message", "")
        if "401" in str(status) or "Invalid" in message or "revoked" in message.lower():
            print(f"Authentication failed: {message}")
            from user_messages import MSG_LOGIN

            print(MSG_LOGIN)
            cfg.pop("agent_key", None)
            save_config(cfg)
            sys.exit(1)
        print(f"Warning: could not reach relay ({message}). Key saved — will retry on next pull.")
    else:
        count = result.get("count", 0)
        org_id = result.get("organization_id", "")
        print(f"Connected to org {org_id} — {count} events available.")

    from pipeline import maybe_sync_routing_from_cloud, maybe_sync_agent_secrets_from_cloud

    try:
        maybe_sync_routing_from_cloud(quiet=True)
    except Exception:
        pass
    try:
        maybe_sync_agent_secrets_from_cloud(quiet=True)
    except Exception:
        pass

    try:
        api_base = routing_cloud.get_api_base(load_config)
        status_data = connections_cloud.fetch_status(api_base, agent_key)
        _persist_account_identity_from_status(status_data)
    except Exception:
        pass

    org_cloud_id = str(result.get("organization_id") or "").strip()
    if org_cloud_id:
        cfg = load_config()
        cfg["organization_id"] = org_cloud_id
        save_config(cfg)

    count = result.get("count", 0) if not result.get("error") else 0
    if count > 0:
        print("Importing events...")
        try:
            imported, skipped = sync_from_relay_org(agent_key, after_id=get_last_max_id(), full=not get_last_max_id())
            print(f"Imported {imported} new, {skipped} skipped or already on disk.")
        except Exception as e:
            print(f"Import warning: {e}")
            print("Your agent key is saved — run pull again later or use merge-leads for duplicates.")
        print()
        if reconnect:
            print(format_stats(get_stats()))
        else:
            leads = get_pipeline()
            print(format_pipeline_table(leads))
            print()
            print(format_stats(get_stats()))

    print()
    paths = working_paths_payload()
    print(f"Working files: {paths['working_root']}")
    print(f"  {paths['imports']}")
    print(f"  {paths['exports']}")
    print()
    print("Connected. Run 'pull' to sync events, 'show' to view pipeline.")


# Connection management (via app API) — platform labels/hints from platform_registry.py


def cmd_platform_map(platform: Optional[str] = None) -> None:
    """Print platform and event mapping registry (agent discovery)."""
    data = platform_map_json(platform)
    print(json.dumps(data, indent=2))


def _require_agent_key() -> str:
    if _check_account_access_revoked():
        sys.exit(1)
    key = get_agent_key()
    if not key:
        from user_messages import MSG_NO_AGENT_KEY

        print(MSG_NO_AGENT_KEY)
        sys.exit(1)
    return key


def _staleness_label(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "unknown"
    delta = datetime.now(timezone.utc) - dt
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _staleness_indicator(iso_ts: Optional[str]) -> str:
    """Return a unicode indicator: green dot < 24h, yellow 24h-7d, red > 7d."""
    if not iso_ts:
        return "\u26aa"  # white circle
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "\u26aa"
    delta = datetime.now(timezone.utc) - dt
    secs = delta.total_seconds()
    if secs < 86400:
        return "\U0001f7e2"  # green
    if secs < 7 * 86400:
        return "\U0001f7e1"  # yellow
    return "\U0001f534"  # red


def cmd_status(*, json_output: bool = False):
    """Dashboard-style status: plan, connections, usage, routing."""
    from pipeline import load_config, _require_agent_key

    if _check_account_access_revoked():
        payload = {
            "access_revoked": True,
            "status": "access_revoked",
            "message": _account_error_message(),
        }
        if json_output:
            print(json.dumps(payload, indent=2))
            return
        print()
        print("Outreach Magic Status")
        print("\u2500" * 50)
        print("Account: Access error")
        print(_account_error_message())
        print()
        return
    agent_key = _require_agent_key()
    api_base = routing_cloud.get_api_base(load_config)

    try:
        data = connections_cloud.fetch_status(api_base, agent_key)
    except RuntimeError as exc:
        if json_output:
            print(json.dumps({"error": str(exc), "access_revoked": False}))
        else:
            print(f"Could not fetch status: {exc}")
        sys.exit(1)

    _persist_account_identity_from_status(data)
    if json_output:
        cfg = load_config()
        print(json.dumps({
            "access_revoked": False,
            "status": "ok",
            "plan": data.get("plan"),
            "account_email": data.get("accountEmail") or cfg.get("account_email"),
            "organization_id": data.get("organizationId") or cfg.get("organization_id"),
            "share_email": data.get("shareEmail") or data.get("accountEmail") or cfg.get("account_email"),
            "eventsUsed": data.get("eventsUsed"),
            "eventsLimit": data.get("eventsLimit"),
            "connections": data.get("connections", []),
            "workspaceMode": data.get("workspaceMode"),
            "workspacesCount": data.get("workspacesCount"),
        }, indent=2))
        return

    plan = (data.get("plan") or "free").capitalize()
    events_used = int(data.get("eventsUsed", 0) or 0)
    events_limit = data.get("eventsLimit")
    events_buffered = int(data.get("eventsBuffered", 0) or 0)
    buffer_cap = data.get("bufferCap")
    billing_notice = data.get("billingNotice")
    usage_critical = data.get("usageCritical", False)
    usage_exhausted = data.get("usageExhausted", False)
    resets_at = data.get("resetsAt", "")
    is_canceling = data.get("isCanceling", False)
    upgrade_url = data.get("upgradeUrl") or BILLING_UPGRADE_URL

    resets_label = ""
    if resets_at:
        try:
            dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            resets_label = dt.strftime("%b %-d")
        except ValueError:
            resets_label = resets_at[:10]

    print()
    print("Outreach Magic Status")
    print("\u2500" * 50)

    usage_str = str(events_used)
    if events_limit:
        usage_str += f" / {events_limit}"
        pct = round((events_used / events_limit) * 100) if events_limit else 0
    else:
        pct = 0
    plan_suffix = ""
    if is_canceling:
        plan_suffix = " (canceling)"
    from user_messages import metered_usage_label

    print(
        f"Plan: {plan}{plan_suffix}  |  {metered_usage_label(plan)}: {usage_str}  |  Resets: {resets_label}"
    )

    if billing_notice:
        print(f"⚠  {billing_notice}")
        print(f"   {upgrade_url}")
    elif events_limit:
        pct = round((events_used / events_limit) * 100) if events_limit else 0
        if usage_exhausted:
            if events_buffered:
                print(f"⚠  Quota reached. {events_buffered} buffered (cap {buffer_cap or '—'}). Run pull to deliver.")
            else:
                print(f"⚠  Quota reached. Over-limit events buffer until pull or reset.")
            print(f"   {upgrade_url}")
        elif usage_critical or pct >= USAGE_CRITICAL_PERCENT:
            remaining = max(0, events_limit - events_used)
            print(f"⚠  {pct}% used ({remaining} remaining). Consider upgrading.")
            print(f"   {upgrade_url}")
        elif pct >= USAGE_WARNING_PERCENT:
            remaining = max(0, events_limit - events_used)
            print(f"⚠  {pct}% used ({remaining} remaining).")
    print()

    connections = data.get("connections", [])
    active = [c for c in connections if c.get("status") == "active"]
    print(f"Connections ({len(active)} active)")
    if not connections:
        print("  No connections. Ask Outreach Magic to connect a platform (e.g. Smartlead).")
    else:
        for c in connections:
            plat = c.get("platform", "?")
            label = PLATFORM_LABELS.get(plat, plat)
            status = (c.get("status") or "unknown").capitalize()
            events_30d = c.get("events30d", 0)
            last_event = c.get("lastEventAt")
            indicator = _staleness_indicator(last_event)
            age = _staleness_label(last_event)
            print(f"  {indicator} {label:<14} {status:<8}  {events_30d:>5} events (30d)   Last event: {age}")
    print()

    ws_mode = data.get("workspaceMode", "single")
    ws_count = data.get("workspacesCount", 1)
    cfg_version = data.get("routingConfigVersion", "?")
    print(f"Routing: {ws_mode} workspace{'s' if ws_count > 1 else ''}  |  Config v{cfg_version}")

    key_last_used = data.get("agentKeyLastUsedAt")
    if key_last_used:
        print(f"Agent key last used: {_staleness_label(key_last_used)}")
    print()


def cmd_connections(json_output: bool = False):
    """List connected platforms with webhook URLs and stats."""
    from pipeline import load_config, _require_agent_key

    agent_key = _require_agent_key()
    api_base = routing_cloud.get_api_base(load_config)

    try:
        data = connections_cloud.fetch_status(api_base, agent_key)
    except RuntimeError as exc:
        print(f"Could not fetch connections: {exc}")
        sys.exit(1)

    connections = data.get("connections", [])

    if json_output:
        print(json.dumps(connections, indent=2))
        return

    if not connections:
        print("No connections. Ask Outreach Magic to connect a platform (e.g. Smartlead).")
        return

    print()
    print("Platform Connections")
    print("\u2500" * 70)
    for c in connections:
        plat = c.get("platform", "?")
        label = PLATFORM_LABELS.get(plat, plat)
        status = (c.get("status") or "unknown").capitalize()
        events_30d = c.get("events30d", 0)
        last_event = c.get("lastEventAt")
        webhook_url = c.get("webhookUrl")
        indicator = _staleness_indicator(last_event)
        age = _staleness_label(last_event)

        print(f"\n  {indicator} {label} ({status})")
        print(f"    Events (30d): {events_30d}   |   Last event: {age}")
        if webhook_url:
            print(f"    Webhook URL:  {webhook_url}")
        else:
            print(f"    Webhook URL:  (paused/revoked)")
    print()


def cmd_connect_platform(platform: str):
    """Generate a webhook URL for a platform via the app API."""
    from pipeline import load_config, _require_agent_key

    agent_key = _require_agent_key()
    api_base = routing_cloud.get_api_base(load_config)
    platform = platform.lower().strip()

    try:
        result = connections_cloud.create_token(api_base, agent_key, platform=platform)
    except RuntimeError as exc:
        msg = str(exc)
        if "409" in msg:
            print(f"Platform '{platform}' already has a connection.")
            print("Fetching existing webhook URL...")
            try:
                status = connections_cloud.fetch_status(api_base, agent_key)
                for c in status.get("connections", []):
                    if c.get("platform") == platform and c.get("webhookUrl"):
                        print(f"\n  Webhook URL: {c['webhookUrl']}")
                        hint = PLATFORM_SETUP_HINTS.get(platform)
                        if hint:
                            print(f"\n  Setup: {hint}")
                        return
                print("Could not retrieve the existing webhook URL.")
            except RuntimeError:
                pass
            return
        print(f"Failed to create connection: {exc}")
        sys.exit(1)

    token_data = result.get("token", {})
    webhook_url = token_data.get("webhookUrl")
    label = PLATFORM_LABELS.get(platform, platform)

    print(f"\n  {label} connected!")
    if webhook_url:
        print(f"\n  Webhook URL: {webhook_url}")
        print(f"\n  Copy this URL and paste it into your {label} webhook settings.")
        hint = PLATFORM_SETUP_HINTS.get(platform)
        if hint:
            print(f"\n  Setup: {hint}")
    else:
        print("  Token created but webhook URL could not be resolved.")
    print()


def cmd_disconnect_platform(platform: str, skip_confirm: bool = False):
    """Delete a platform webhook token. The webhook URL stops working immediately."""
    from pipeline import load_config, _require_agent_key

    agent_key = _require_agent_key()
    api_base = routing_cloud.get_api_base(load_config)
    platform = platform.lower().strip()

    try:
        status = connections_cloud.fetch_status(api_base, agent_key)
    except RuntimeError as exc:
        print(f"Could not fetch connections: {exc}")
        sys.exit(1)

    match = None
    for c in status.get("connections", []):
        if c.get("platform") == platform:
            match = c
            break

    if not match:
        print(f"No connection found for platform '{platform}'.")
        return

    label = PLATFORM_LABELS.get(platform, platform)
    token_id = match.get("tokenId")
    if not token_id:
        print(f"Cannot disconnect: token ID not available for {label}.")
        return

    if not skip_confirm:
        print(f"\n  WARNING: This will permanently delete the {label} webhook token.")
        print(f"  The webhook URL will stop working immediately.")
        print(f"  Events (30d): {match.get('events30d', 0)}")
        try:
            answer = input("\n  Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return
        if answer != "yes":
            print("  Cancelled.")
            return

    try:
        connections_cloud.delete_token(api_base, agent_key, platform=platform, token_id=token_id)
        print(f"\n  {label} disconnected. Webhook URL is no longer active.")
    except RuntimeError as exc:
        print(f"Failed to disconnect: {exc}")
        sys.exit(1)
    print()




