#!/usr/bin/env python3
"""Workspace routing, relay push, campaign-map CRUD, and quarantine handling."""

from __future__ import annotations

import agent_secrets_cloud
import db_health
import json
import os
import outbox
import quarantine_resolutions as qres
import re
import routing_cloud
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from constants import (
    BILLING_UPGRADE_URL,
    RELAY_BULK_THRESHOLD,
    RELAY_PUSH_ROUTINE_MAX,
)
from db_conn import get_conn
from lead_sync import (
    _load_lead_sync_prefetch,
    build_lead_core_sync_payload,
    build_lead_workspace_sync_payload,
    entity_key_from_prefetch,
)
from pipeline_personalize import build_company_sync_payload, company_entity_key
from pipeline_sender_accounts import (
    build_sender_account_sync_payload,
    build_sender_domain_sync_payload,
    sender_account_entity_key,
    sender_domain_entity_key,
)
from pipeline_sync import (
    _ARROW_PUSH,
    _format_push_done,
    _format_push_pending_banner,
    _format_push_progress,
    _page_label,
    _progress_clock,
    _RELAY_STREAM_EVENT,
    _SNAPSHOT_KIND_STREAM,
    _stream_pad,
    export_local_changes,
)
from pipeline_update import (
    _sync_events_only,
    _use_bulk_transport,
    get_agent_key,
    get_last_sync,
    get_or_create_client_id,
    get_relay_push_settings,
    load_config,
    normalize_relay_timestamp,
    snapshot_as_of,
    save_config,
    set_last_sync,
)
from relay_ingest import (
    relay_bump_explained_clause,
    unsynced_event_clause,
    unsynced_lead_clause,
    unsynced_workspace_lead_clause,
)
from workspace_routing import (
    DEFAULT_ORG_ID,
    MULTI_WORKSPACE_HOLD_MESSAGE,
    VALID_WORKSPACE_ROUTING_MODES,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    assign_campaign_map,
    deactivate_campaign_map,
    deactivate_shadowed_backfill_rules,
    detect_shadow_conflicts,
    ensure_default_org_workspace,
    ensure_organization,
    extract_campaign_context,
    format_no_campaign_event_message,
    format_unmapped_campaign_message,
    get_org_routing_config,
    lead_entity_key,
    quarantine_event,
    reconcile_workspace_routing,
    resolve_workspace_identity,
)

RELAY_URL = "https://api.outreachmagic.io"


def list_workspaces(org_id: str = DEFAULT_ORG_ID) -> list[dict]:
    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    if config.mode == WORKSPACE_ROUTING_MULTI:
        rows = conn.execute(
            "SELECT id, org_id, name, slug, created_at FROM workspaces WHERE org_id = ? AND slug != 'default' ORDER BY name",
            (org_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, org_id, name, slug, created_at FROM workspaces WHERE org_id = ? ORDER BY name",
            (org_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_routing_config_summary(org_id: str = DEFAULT_ORG_ID) -> dict:
    """Workspace + campaign-map counts for refresh / status checks."""
    routing = get_workspace_routing(org_id)
    workspaces = list_workspaces(org_id)
    maps = list_campaign_maps(org_id)
    return {
        "mode": routing.get("mode"),
        "workspace_count": len(workspaces),
        "campaign_map_count": len(maps),
        "workspaces": [
            {"slug": w.get("slug"), "name": w.get("name")}
            for w in workspaces
        ],
        "pending_quarantine": int(routing.get("pending_quarantine") or 0),
    }


def format_routing_refresh_summary(summary: dict) -> str:
    mode = summary.get("mode") or "unknown"
    ws_n = int(summary.get("workspace_count") or 0)
    map_n = int(summary.get("campaign_map_count") or 0)
    q = int(summary.get("pending_quarantine") or 0)
    line = f"Routing ready: {ws_n} workspace(s), {map_n} campaign map(s), mode={mode}"
    if q:
        line += f", {q} pending quarantine item(s)"
    return line


def get_workspace_routing(org_id: str = DEFAULT_ORG_ID) -> dict:
    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    ws = None
    if config.mode == WORKSPACE_ROUTING_SINGLE and config.default_workspace_id:
        ws = conn.execute(
            "SELECT id, name, slug FROM workspaces WHERE id = ?",
            (config.default_workspace_id,),
        ).fetchone()
    pending = conn.execute(
        """SELECT COUNT(*) AS n FROM unmapped_campaign_queue
           WHERE org_id = ? AND status = 'pending'""",
        (org_id,),
    ).fetchone()["n"]
    conn.close()
    out = {
        "mode": config.mode,
        "pending_quarantine": pending,
    }
    if config.mode == WORKSPACE_ROUTING_SINGLE:
        out["default_workspace_id"] = config.default_workspace_id
        out["default_workspace_slug"] = ws["slug"] if ws else None
        out["default_workspace_name"] = ws["name"] if ws else None
    else:
        out["message"] = MULTI_WORKSPACE_HOLD_MESSAGE
    return out


def _apply_cloud_routing_bundle(bundle: dict, org_id: str = DEFAULT_ORG_ID) -> None:
    conn = get_conn()
    routing_cloud.apply_routing_bundle_to_sqlite(conn, bundle, org_id=org_id)
    conn.commit()
    conn.close()
    cfg = load_config()
    cfg["routing_config_version"] = bundle.get("version")
    cfg["workspace_routing_mode"] = bundle.get("mode")
    save_config(cfg)


def maybe_sync_routing_from_cloud(*, quiet: bool = False) -> bool:
    """Pull routing config from wbhk-app when an agent key is configured."""
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return False
    conn = get_conn()
    try:
        routing_cloud.sync_routing_from_cloud(
            conn,
            api_base=routing_cloud.get_api_base(load_config),
            token=tok,
            org_id=DEFAULT_ORG_ID,
            load_config_fn=load_config,
            save_config_fn=save_config,
            quiet=quiet,
        )
        return True
    finally:
        conn.close()


def maybe_sync_agent_secrets_from_cloud(*, quiet: bool = False) -> bool:
    """Pull org BYOK API keys from wbhk-app when an agent key is configured."""
    return agent_secrets_cloud.maybe_sync_agent_secrets_from_cloud(
        load_config_fn=load_config,
        save_config_fn=save_config,
        get_agent_key_fn=get_agent_key,
        quiet=quiet,
    )


def sync_agent_secrets_cli(
    *,
    check_only: bool = False,
    as_json: bool = False,
    quiet: bool = False,
) -> dict:
    if check_only:
        result = agent_secrets_cloud.check_agent_secrets_local(load_config)
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Local API keys: {result.get('path')}")
            for key, ok in (result.get("configured") or {}).items():
                size = (result.get("pool_sizes") or {}).get(key, 0)
                print(f"  {key}: {'set' if ok else 'missing'} (pool={size})")
        return result

    tok = get_agent_key()
    if not agent_secrets_cloud.cloud_secrets_enabled(load_config, tok):
        err = {"ok": False, "error": "Not logged in. Ask Outreach Magic to log in."}
        if as_json:
            print(json.dumps(err))
        else:
            print(err["error"])
        return err

    try:
        result = agent_secrets_cloud.sync_agent_secrets_from_cloud(
            api_base=agent_secrets_cloud.get_api_base(load_config),
            token=tok or "",
            load_config_fn=load_config,
            save_config_fn=save_config,
            quiet=quiet,
        )
    except RuntimeError as exc:
        err = {"ok": False, "error": str(exc)}
        if as_json:
            print(json.dumps(err))
        else:
            print(f"Sync failed: {exc}")
        return err

    try:
        from api_key_pool import maybe_push_api_key_status_to_cloud

        status_result = maybe_push_api_key_status_to_cloud(
            load_config_fn=load_config,
            get_agent_key_fn=get_agent_key,
            get_client_id_fn=get_or_create_client_id,
            push_fn=routing_cloud.push_api_key_status,
            quiet=quiet,
        )
        result = {**result, **status_result}
    except ImportError:
        pass

    if as_json:
        print(json.dumps(result))
    elif result.get("api_key_status_reported") == "reported" and not quiet:
        print("Runtime API key status reported to dashboard.")
    return result


def api_keys_cli(*, as_json: bool = False, push: bool = False) -> dict:
    from api_key_pool import build_api_keys_report, format_api_keys_report_text, maybe_push_api_key_status_to_cloud

    report = build_api_keys_report()
    if push:
        report["cloud"] = maybe_push_api_key_status_to_cloud(
            load_config_fn=load_config,
            get_agent_key_fn=get_agent_key,
            get_client_id_fn=get_or_create_client_id,
            push_fn=routing_cloud.push_api_key_status,
            quiet=False,
        )
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(format_api_keys_report_text(report), end="")
        cloud = report.get("cloud")
        if isinstance(cloud, dict) and cloud.get("api_key_status_reported") == "reported":
            print("Runtime status reported to dashboard.", flush=True)
    return report


def set_workspace_routing(
    mode: str,
    *,
    workspace_slug: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    mode = (mode or "").strip().lower()
    if mode not in VALID_WORKSPACE_ROUTING_MODES:
        return {
            "status": "error",
            "error": f"mode must be one of: {', '.join(VALID_WORKSPACE_ROUTING_MODES)}",
        }
    tok = get_agent_key()
    if routing_cloud.cloud_routing_enabled(load_config, tok):
        try:
            bundle = routing_cloud.push_routing_mode(
                routing_cloud.get_api_base(load_config),
                tok,
                mode=mode,
                default_workspace_slug=workspace_slug,
            )
            _apply_cloud_routing_bundle(bundle, org_id)
            result = get_workspace_routing(org_id)
            result["status"] = "ok"
            if mode == WORKSPACE_ROUTING_MULTI:
                result["notice"] = MULTI_WORKSPACE_HOLD_MESSAGE
            return result
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}
    conn = get_conn()
    ensure_organization(conn, org_id)
    current = conn.execute(
        "SELECT workspace_routing_mode FROM organizations WHERE id = ?",
        (org_id,),
    ).fetchone()
    if (
        current
        and current["workspace_routing_mode"] == WORKSPACE_ROUTING_MULTI
        and mode == WORKSPACE_ROUTING_SINGLE
    ):
        conn.close()
        return {
            "status": "error",
            "error": "Cannot switch back to single-workspace mode after multi-workspace is enabled.",
        }
    ws_id: Optional[str] = None
    if mode == WORKSPACE_ROUTING_SINGLE:
        ws_id = ensure_default_org_workspace(conn)
        if workspace_slug:
            ws = conn.execute(
                "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
                (org_id, workspace_slug),
            ).fetchone()
            if not ws:
                conn.close()
                return {"status": "error", "error": f"workspace not found: {workspace_slug}"}
            ws_id = ws["id"]
        if not ws_id:
            ws_id = ensure_default_org_workspace(conn)
        conn.execute(
            """UPDATE organizations
               SET workspace_routing_mode = ?, default_workspace_id = ? WHERE id = ?""",
            (mode, ws_id, org_id),
        )
    else:
        conn.execute(
            """UPDATE organizations
               SET workspace_routing_mode = ?, default_workspace_id = NULL WHERE id = ?""",
            (mode, org_id),
        )
    conn.commit()
    conn.close()
    cfg = load_config()
    cfg["workspace_routing_mode"] = mode
    save_config(cfg)
    result = get_workspace_routing(org_id)
    result["status"] = "ok"
    if mode == WORKSPACE_ROUTING_MULTI:
        result["notice"] = MULTI_WORKSPACE_HOLD_MESSAGE
    return result


def create_workspace(name: str, slug: Optional[str] = None, org_id: str = DEFAULT_ORG_ID, *, sync: bool = False) -> dict:
    slug = slug or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    ws_id = f"ws_{slug}"
    conn = get_conn()
    ensure_organization(conn, org_id)
    try:
        conn.execute(
            """INSERT INTO workspaces (id, org_id, name, slug, created_at, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (ws_id, org_id, name, slug),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"status": "error", "error": f"workspace slug already exists: {slug}"}
    conn.close()

    result: dict = {"status": "created", "id": ws_id, "name": name, "slug": slug}

    tok = get_agent_key()
    can_sync = routing_cloud.cloud_routing_enabled(load_config, tok)

    if sync and can_sync:
        try:
            routing_cloud.push_workspace_create(
                routing_cloud.get_api_base(load_config),
                tok,
                name=name,
                slug=slug,
            )
            _mark_workspace_synced(slug, org_id)
            result["synced"] = True
        except RuntimeError as exc:
            result["synced"] = False
            result["sync_error"] = str(exc)
    elif can_sync:
        result["synced"] = False
        result["sync_hint"] = (
            f"Workspace '{name}' created locally. To make it visible in the webapp, "
            "Ask Outreach Magic to sync workspace routing"
        )
    else:
        result["synced"] = False
        result["sync_hint"] = (
            "Workspace created locally only. No cloud token configured — "
            "set up an agent key to enable syncing to the webapp."
        )

    return result


def sync_workspaces_to_cloud(org_id: str = DEFAULT_ORG_ID) -> dict:
    """Push all local workspaces to the cloud webapp."""
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return {"status": "error", "error": "No cloud token configured. Set up an agent key first."}

    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    rows = conn.execute(
        "SELECT name, slug FROM workspaces WHERE org_id = ?", (org_id,)
    ).fetchall()
    conn.close()

    if config.mode == WORKSPACE_ROUTING_MULTI:
        workspaces = [dict(r) for r in rows if r["slug"] != "default"]
    else:
        workspaces = [dict(r) for r in rows]

    api_base = routing_cloud.get_api_base(load_config)
    synced = []
    errors = []
    for ws in workspaces:
        try:
            routing_cloud.push_workspace_create(api_base, tok, name=ws["name"], slug=ws["slug"])
            _mark_workspace_synced(ws["slug"], org_id)
            synced.append(ws["slug"])
        except RuntimeError as exc:
            if "already exists" in str(exc).lower() or "unique" in str(exc).lower():
                _mark_workspace_synced(ws["slug"], org_id)
                synced.append(ws["slug"])
            else:
                errors.append({"slug": ws["slug"], "error": str(exc)})

    return {"status": "ok", "synced": synced, "errors": errors}


def get_sync_status(org_id: str = DEFAULT_ORG_ID) -> dict:
    """Compare local state with cloud and return what's pending sync."""
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return {"can_sync": False, "reason": "No cloud token configured."}

    try:
        bundle = routing_cloud.fetch_routing_bundle(
            routing_cloud.get_api_base(load_config), tok
        )
    except Exception as exc:
        return {"can_sync": False, "reason": f"Could not reach webapp: {exc}"}

    cloud_ws_slugs = {w["slug"] for w in bundle.get("workspaces") or []}
    cloud_map_ids = {m["id"] for m in bundle.get("campaignMaps") or []}
    cloud_map_sigs = routing_cloud.cloud_campaign_map_signatures(bundle)

    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    local_ws = conn.execute(
        "SELECT name, slug, cloud_synced FROM workspaces WHERE org_id = ?", (org_id,)
    ).fetchall()
    local_maps = conn.execute(
        """SELECT m.id, m.source_platform, m.campaign_name_normalized, m.campaign_platform_id,
                  m.match_strategy, m.cloud_synced, w.slug AS workspace_slug
           FROM campaign_workspace_map m
           JOIN workspaces w ON w.id = m.workspace_id
           WHERE m.org_id = ? AND m.is_active = 1""",
        (org_id,),
    ).fetchall()
    conn.close()

    pending_ws = []
    for row in local_ws:
        slug = row["slug"]
        if config.mode == WORKSPACE_ROUTING_MULTI and slug == "default":
            continue
        # Only workspaces created/edited locally (cloud_synced=0) need a push.
        if int(row["cloud_synced"] or 0) != 0:
            continue
        if slug not in cloud_ws_slugs:
            pending_ws.append({"name": row["name"], "slug": slug})

    pending_maps = []
    for row in local_maps:
        if int(row["cloud_synced"] or 0) != 0:
            continue
        if row["id"] in cloud_map_ids:
            continue
        sig = routing_cloud.campaign_map_signature(
            source_platform=row["source_platform"],
            match_strategy=row["match_strategy"],
            campaign_platform_id=row["campaign_platform_id"],
            campaign_name_normalized=row["campaign_name_normalized"],
            workspace_slug=row["workspace_slug"],
        )
        if sig in cloud_map_sigs:
            continue
        pending_maps.append({
            "id": row["id"],
            "label": row["campaign_name_normalized"] or row["campaign_platform_id"] or "rule",
            "match_strategy": row["match_strategy"],
        })

    conn2 = get_conn()
    local_lead_count = conn2.execute(
        f"SELECT COUNT(*) AS n FROM leads l WHERE {unsynced_lead_clause('l')}"
    ).fetchone()["n"]
    local_event_count = conn2.execute(
        f"SELECT COUNT(*) AS n FROM events e WHERE {unsynced_event_clause('e')}"
    ).fetchone()["n"]
    last_sync = get_last_sync()
    if last_sync:
        pending_lead_core_count = conn2.execute(
            f"""SELECT COUNT(*) AS n FROM leads l
                WHERE (l.updated_at > ? AND NOT {relay_bump_explained_clause('l.id', 'l.updated_at')})
                   OR {unsynced_lead_clause('l')}""",
            (last_sync,),
        ).fetchone()["n"]
        pending_workspace_count = conn2.execute(
            f"""SELECT COUNT(*) AS n FROM workspace_leads wl
                WHERE (wl.updated_at > ? AND NOT {relay_bump_explained_clause('wl.lead_id', 'wl.updated_at')})
                   OR {unsynced_workspace_lead_clause('wl')}""",
            (last_sync,),
        ).fetchone()["n"]
        pending_quarantine_count = conn2.execute(
            """SELECT COUNT(*) AS n FROM unmapped_campaign_queue
               WHERE resolved_at > ? AND status IN ('skipped', 'assigned')""", (last_sync,)
        ).fetchone()["n"]
    else:
        pending_lead_core_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM leads"
        ).fetchone()["n"]
        pending_workspace_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM workspace_leads"
        ).fetchone()["n"]
        pending_quarantine_count = conn2.execute(
            """SELECT COUNT(*) AS n FROM unmapped_campaign_queue
               WHERE status IN ('skipped', 'assigned')"""
        ).fetchone()["n"]
    conn2.close()

    pending_total = len(pending_ws) + len(pending_maps)
    snapshot_pending = (
        local_event_count
        + pending_lead_core_count
        + pending_workspace_count
        + pending_quarantine_count
    )
    return {
        "can_sync": True,
        "pending_workspaces": pending_ws,
        "pending_rules": pending_maps,
        "relay_untracked_leads": local_lead_count,
        "local_agent_events": local_event_count,
        "leads_pending": pending_lead_core_count,
        "workspace_leads_pending": pending_workspace_count,
        "pending_quarantine_resolutions": pending_quarantine_count,
        "pending_total": pending_total + snapshot_pending,
        "synced": pending_total == 0 and snapshot_pending == 0,
        "recommended_mode": (
            "bulk"
            if (pending_lead_core_count + pending_workspace_count) >= RELAY_BULK_THRESHOLD
            else "push"
        ),
    }


def format_sync_status(status: dict) -> str:
    """One-line sync status for display after operations."""
    if not status.get("can_sync"):
        return ""
    if status.get("synced"):
        return ""
    parts = []
    ws = status.get("pending_workspaces") or []
    rules = status.get("pending_rules") or []
    local_events = status.get("local_agent_events", 0)
    leads_pending = status.get("leads_pending", 0)
    ws_leads_pending = status.get("workspace_leads_pending", 0)
    relay_untracked = status.get("relay_untracked_leads", 0)
    if ws:
        names = ", ".join(w["name"] for w in ws[:3])
        suffix = f" (+{len(ws) - 3} more)" if len(ws) > 3 else ""
        parts.append(f"{len(ws)} workspace{'s' if len(ws) != 1 else ''} ({names}{suffix})")
    if rules:
        parts.append(f"{len(rules)} routing rule{'s' if len(rules) != 1 else ''}")
    if local_events:
        parts.append(f"{local_events} agent event{'s' if local_events != 1 else ''}")
    if leads_pending:
        parts.append(f"{leads_pending} lead snapshot{'s' if leads_pending != 1 else ''}")
    if ws_leads_pending:
        parts.append(
            f"{ws_leads_pending} workspace snapshot{'s' if ws_leads_pending != 1 else ''}"
        )
    out = ""
    if parts:
        out = f"\n⚠ Not synced to cloud: {', '.join(parts)}. Run: pipeline.py sync"
    if relay_untracked:
        out += (
            f"\nℹ relay_untracked_leads={relay_untracked}: imported/local leads with no relay "
            "pull history (normal after CSV). Data is in the shared DB — run pipeline.py paths."
        )
    return out


def _mark_workspace_synced(slug: str, org_id: str = DEFAULT_ORG_ID) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE workspaces SET cloud_synced = 1 WHERE org_id = ? AND slug = ?",
        (org_id, slug),
    )
    conn.commit()
    conn.close()


def get_local_pending_counts(org_id: str = DEFAULT_ORG_ID) -> dict:
    """Check local DB for unsynced items — no network calls."""
    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    ws_filter = " AND slug != 'default'" if config.mode == WORKSPACE_ROUTING_MULTI else ""
    unsynced_ws = conn.execute(
        f"SELECT COUNT(*) AS n FROM workspaces WHERE org_id = ? AND cloud_synced = 0{ws_filter}",
        (org_id,),
    ).fetchone()["n"]
    unsynced_rules = conn.execute(
        "SELECT COUNT(*) AS n FROM campaign_workspace_map WHERE org_id = ? AND is_active = 1 AND cloud_synced = 0",
        (org_id,),
    ).fetchone()["n"]
    local_events = conn.execute(
        f"SELECT COUNT(*) AS n FROM events e WHERE {unsynced_event_clause('e')}"
    ).fetchone()["n"]
    # Pending is now a fact we recorded, not a guess re-derived from a corrupt
    # updated_at cursor.
    dirty = outbox.count_dirty(conn)
    leads_pending = dirty.get("lead_core:upsert", 0)
    ws_pending = dirty.get("lead_workspace:upsert", 0)
    conn.close()
    return {
        "workspaces": unsynced_ws,
        "rules": unsynced_rules,
        "local_agent_events": local_events,
        "leads_pending": leads_pending,
        "workspace_leads_pending": ws_pending,
        "total": unsynced_ws + unsynced_rules + local_events + leads_pending + ws_pending,
    }


def format_local_sync_hint(counts: dict) -> str:
    """One-line hint about unsynced items. Pure local check, no network."""
    if counts["total"] == 0:
        return ""
    parts = []
    if counts["workspaces"]:
        parts.append(f"{counts['workspaces']} workspace{'s' if counts['workspaces'] != 1 else ''}")
    if counts["rules"]:
        parts.append(f"{counts['rules']} routing rule{'s' if counts['rules'] != 1 else ''}")
    if counts.get("local_agent_events"):
        n = counts["local_agent_events"]
        parts.append(f"{n} agent event{'s' if n != 1 else ''}")
    if counts.get("leads_pending"):
        n = counts["leads_pending"]
        parts.append(f"{n} lead snapshot{'s' if n != 1 else ''}")
    if counts.get("workspace_leads_pending"):
        n = counts["workspace_leads_pending"]
        parts.append(f"{n} workspace snapshot{'s' if n != 1 else ''}")
    return f"\n⚠ Not synced: {', '.join(parts)}. Run: pipeline.py sync"


def sync_all(
    org_id: str = DEFAULT_ORG_ID,
    *,
    no_health_report: bool = False,
    force_bulk: Optional[bool] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Push pending workspaces, rules, and lead snapshots to the cloud.

    When ``workspace`` is provided, only lead/workspace snapshots scoped to
    that workspace are pushed. Workspace and routing-rule syncs are unaffected
    (they are always global).

    Network push only runs when the user invokes `pipeline.py sync` (never on
    import, init, pull, or show). Requires a configured agent key.
    """
    from pipeline import (
        __version__,
        get_agent_key,
        get_sync_status,
        _push_agent_events_to_relay,
        _push_pending_company_updates,
        _push_pending_lead_snapshots,
        _push_pending_merge_deletes,
        _push_pending_quarantine_resolutions,
        _push_pending_sender_account_updates,
        _push_pending_sender_domain_updates,
    )

    _init_relay_sync_log()
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return {"status": "error", "error": "No cloud token configured."}

    api_base = routing_cloud.get_api_base(load_config)
    status = get_sync_status(org_id)
    _relay_log(
        "sync plan: "
        f"events={status.get('local_agent_events', 0):,}, "
        f"leads_pending={status.get('leads_pending', 0):,}, "
        f"workspace_leads_pending={status.get('workspace_leads_pending', 0):,}, "
        f"pending_workspaces={len(status.get('pending_workspaces') or [])}, "
        f"pending_rules={len(status.get('pending_rules') or [])}"
    )
    if not status.get("can_sync"):
        return {"status": "error", "error": status.get("reason", "Cannot sync.")}
    results: dict = {"workspaces_synced": [], "rules_synced": [], "errors": []}
    if status.get("synced"):
        results["status"] = "ok"
        results["message"] = "Workspaces and rules already synced."
        # Still fall through — lead snapshots may need push.

    snapshot_pending = (
        status.get("leads_pending", 0)
        + status.get("workspace_leads_pending", 0)
    )
    transport = _use_bulk_transport(snapshot_pending, force_bulk=force_bulk)
    if transport["bulk"]:
        print(
            f"Syncing to relay (bulk mode, {transport['push_batch_size']}/request) — "
            f"{snapshot_pending} snapshot(s) pending, "
            f"{status.get('local_agent_events', 0)} event(s) pending...",
            flush=True,
        )
    elif _sync_events_only() and status.get("local_agent_events", 0) >= RELAY_BULK_THRESHOLD:
        pending_ev = status.get("local_agent_events", 0)
        ev_batch = get_relay_push_settings(bulk=True)["batch_size"]
        _relay_log(
            f"Syncing to relay (events-only, {ev_batch}/request) — {pending_ev:,} event(s) pending ..."
        )
    else:
        print("Syncing to relay...", flush=True)

    for ws in status.get("pending_workspaces") or []:
        try:
            routing_cloud.push_workspace_create(api_base, tok, name=ws["name"], slug=ws["slug"])
            _mark_workspace_synced(ws["slug"], org_id)
            results["workspaces_synced"].append(ws["slug"])
        except RuntimeError as exc:
            if "already exists" in str(exc).lower() or "unique" in str(exc).lower():
                _mark_workspace_synced(ws["slug"], org_id)
                results["workspaces_synced"].append(ws["slug"])
            else:
                results["errors"].append({"type": "workspace", "slug": ws["slug"], "error": str(exc)})

    conn = get_conn()
    for rule in status.get("pending_rules") or []:
        row = conn.execute(
            """SELECT source_platform, campaign_platform_id, campaign_name_normalized, match_strategy, priority,
                      w.slug AS workspace_slug
               FROM campaign_workspace_map m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.id = ?""",
            (rule["id"],),
        ).fetchone()
        if not row:
            continue
        try:
            routing_cloud.push_campaign_map(
                api_base, tok,
                source_platform=row["source_platform"],
                workspace_slug=row["workspace_slug"],
                campaign_platform_id=row["campaign_platform_id"],
                campaign_name=row["campaign_name_normalized"],
                match_strategy=row["match_strategy"],
                priority=row["priority"],
            )
            conn.execute(
                "UPDATE campaign_workspace_map SET cloud_synced = 1 WHERE id = ?",
                (rule["id"],),
            )
            results["rules_synced"].append(rule["label"])
        except RuntimeError as exc:
            results["errors"].append({"type": "rule", "label": rule["label"], "error": str(exc)})
    conn.commit()
    conn.close()

    routing_pushed = bool(results["workspaces_synced"] or results["rules_synced"])
    if routing_pushed:
        conn = get_conn()
        try:
            routing_cloud.sync_routing_from_cloud(
                conn,
                api_base=api_base,
                token=tok,
                org_id=org_id,
                load_config_fn=load_config,
                save_config_fn=save_config,
                quiet=True,
            )
        finally:
            conn.close()

    total = len(results["workspaces_synced"]) + len(results["rules_synced"])
    results["status"] = "ok"

    local_events = status.get("local_agent_events", 0)

    parts = []
    if total:
        parts.append(f"Synced {total} item{'s' if total != 1 else ''} to cloud.")
    results["relay_push_settings"] = get_relay_push_settings(bulk=transport["bulk"])
    results["recommended_mode"] = status.get("recommended_mode", "push")
    agent_key = get_agent_key()
    if local_events and agent_key:
        agent_push = _push_agent_events_to_relay(agent_key)
        pushed = int(agent_push.get("pushed", 0) or 0)
        results["agent_events_pushed"] = pushed
        if agent_push.get("timeouts"):
            results["agent_events_timeouts"] = int(agent_push.get("timeouts", 0) or 0)
        if agent_push.get("error"):
            results["agent_events_error"] = agent_push["error"]
        if agent_push.get("throttled"):
            results["agent_events_throttled"] = True
        if agent_push.get("recommendation"):
            results["agent_events_recommendation"] = agent_push["recommendation"]
        if pushed > 0:
            parts.append(f"Pushed {pushed} agent event{'s' if pushed != 1 else ''} to relay.")
        elif local_events:
            parts.append(f"{local_events} agent event{'s' if local_events != 1 else ''} could not be pushed.")
    elif local_events:
        parts.append(
            f"{local_events} agent event{'s' if local_events != 1 else ''} pending — "
            f"no agent key configured to push them."
        )

    if agent_key:
        q_push = _push_pending_quarantine_resolutions(agent_key)
        q_synced = int(q_push.get("synced") or 0)
        results["quarantine_resolutions_synced"] = q_synced
        if q_push.get("errors"):
            results["quarantine_resolution_errors"] = q_push["errors"]
        if q_synced:
            parts.append(
                f"Synced {q_synced} quarantine resolution{'s' if q_synced != 1 else ''} to relay."
            )

        merge_delete_push = _push_pending_merge_deletes(agent_key, bulk=transport["bulk"])
        results["merge_deletes_pushed"] = int(merge_delete_push.get("pushed", 0) or 0)
        if merge_delete_push.get("error"):
            results["merge_deletes_error"] = merge_delete_push["error"]

        lead_push = _push_pending_lead_snapshots(agent_key, bulk=transport["bulk"], workspace=workspace)
        leads_pushed = int(lead_push.get("pushed", 0) or 0)
        results["lead_snapshots_pushed"] = leads_pushed
        if lead_push.get("timeouts"):
            results["lead_snapshots_timeouts"] = int(lead_push.get("timeouts", 0) or 0)
        if lead_push.get("error"):
            results["lead_snapshots_error"] = lead_push["error"]
        if lead_push.get("throttled"):
            results["lead_snapshots_throttled"] = True
        if lead_push.get("recommendation"):
            results["lead_snapshots_recommendation"] = lead_push["recommendation"]
        if leads_pushed > 0:
            parts.append(f"Pushed {leads_pushed} lead snapshot{'s' if leads_pushed != 1 else ''} to relay.")

        company_push = _push_pending_company_updates(agent_key)
        cos_pushed = int(company_push.get("pushed", 0) or 0)
        results["company_updates_pushed"] = cos_pushed
        if company_push.get("error"):
            results["company_updates_error"] = company_push["error"]
        if cos_pushed > 0:
            parts.append(f"Pushed {cos_pushed} company update{'s' if cos_pushed != 1 else ''} to relay.")

        sender_account_push = _push_pending_sender_account_updates(agent_key)
        sa_pushed = int(sender_account_push.get("pushed", 0) or 0)
        results["sender_account_updates_pushed"] = sa_pushed
        if sender_account_push.get("error"):
            results["sender_account_updates_error"] = sender_account_push["error"]
        if sa_pushed > 0:
            parts.append(f"Pushed {sa_pushed} sender account update{'s' if sa_pushed != 1 else ''} to relay.")

        sender_domain_push = _push_pending_sender_domain_updates(agent_key)
        sd_pushed = int(sender_domain_push.get("pushed", 0) or 0)
        results["sender_domain_updates_pushed"] = sd_pushed
        if sender_domain_push.get("error"):
            results["sender_domain_updates_error"] = sender_domain_push["error"]
        if sd_pushed > 0:
            parts.append(f"Pushed {sd_pushed} sender domain update{'s' if sd_pushed != 1 else ''} to relay.")

        if (
            lead_push.get("error") is None
            and company_push.get("error") is None
            and sender_account_push.get("error") is None
            and sender_domain_push.get("error") is None
        ):
            set_last_sync(datetime.now(timezone.utc).isoformat())

    results["message"] = " ".join(parts) or "Everything is already synced."

    conn = get_conn()
    try:
        health_result = db_health.maybe_report_db_health_to_cloud(
            conn,
            org_id=org_id,
            pipeline_version=__version__,
            get_agent_key_fn=get_agent_key,
            load_config_fn=load_config,
            save_config_fn=save_config,
            get_client_id_fn=get_or_create_client_id,
            cloud_routing_enabled_fn=routing_cloud.cloud_routing_enabled,
            get_api_base_fn=routing_cloud.get_api_base,
            push_db_health_fn=routing_cloud.push_db_health,
            fast=True,
            force=False,
            skip=no_health_report,
        )
        results.update(health_result)
    finally:
        conn.close()

    return results


def preview_sync(
    org_id: str = DEFAULT_ORG_ID,
    *,
    workspace: Optional[str] = None,
    sample_size: int = 3,
) -> dict:
    """Preview what `sync` would push, without waiting for a full build or sending anything.

    Reuses get_sync_status()'s cheap COUNT-based totals, and caps every
    underlying query at `sample_size` rows so this returns fast even when
    tens of thousands of rows are pending — it never builds the full export
    and then slices it.
    """
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return {"status": "error", "error": "No cloud token configured."}

    status = get_sync_status(org_id)
    if not status.get("can_sync"):
        return {"status": "error", "error": status.get("reason", "Cannot sync.")}

    events_export = export_local_changes(events_only=True, sample_limit=sample_size)
    lead_preview = _push_pending_lead_snapshots(
        tok, workspace=workspace, sample_limit=sample_size, dry_run=True,
    )
    merge_preview = _push_pending_merge_deletes(tok, sample_limit=sample_size, dry_run=True)
    company_preview = _push_pending_company_updates(tok, sample_limit=sample_size, dry_run=True)
    sender_account_preview = _push_pending_sender_account_updates(tok, sample_limit=sample_size, dry_run=True)
    sender_domain_preview = _push_pending_sender_domain_updates(tok, sample_limit=sample_size, dry_run=True)
    quarantine_preview = _push_pending_quarantine_resolutions(tok, sample_limit=sample_size, dry_run=True)

    return {
        "status": "dry_run",
        "sample_size": sample_size,
        "totals": {
            "events_pending": status.get("local_agent_events", 0),
            "leads_core_pending": status.get("leads_pending", 0),
            "leads_workspace_pending": status.get("workspace_leads_pending", 0),
            "merge_deletes_pending": merge_preview.get("total_pending", 0),
            "company_updates_pending": company_preview.get("total_pending", 0),
            "sender_account_updates_pending": sender_account_preview.get("total_pending", 0),
            "sender_domain_updates_pending": sender_domain_preview.get("total_pending", 0),
            "quarantine_resolutions_pending": quarantine_preview.get("total_pending", 0),
        },
        "samples": {
            "event_log": events_export.get("entries", []),
            "lead_core_update": lead_preview.get("sample_core_entries", []),
            "lead_workspace_update": lead_preview.get("sample_ws_entries", []),
            "lead_core_delete": merge_preview.get("sample_entries", []),
            "company_update": company_preview.get("sample_entries", []),
            "sender_account_update": sender_account_preview.get("sample_entries", []),
            "sender_domain_update": sender_domain_preview.get("sample_entries", []),
            "quarantine_resolution": quarantine_preview.get("sample_entries", []),
        },
    }


_SYNC_LOG_FILE: Optional[Path] = None


def _init_relay_sync_log() -> None:
    """Optional file mirror: OM_SYNC_LOG=/path/to/batch_sync.log"""
    global _SYNC_LOG_FILE
    raw = os.environ.get("OM_SYNC_LOG", "").strip()
    if raw:
        _SYNC_LOG_FILE = Path(raw).expanduser()
        _SYNC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _relay_log(msg: str) -> None:
    """Stderr + optional log file, always flushed (safe for tail -f).

    Uses stderr so callers parsing stdout (e.g. companion subprocess JSON
    readers) never see progress lines mixed into their JSON stream.
    """
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


def _audit_result(audit_ids, *, http_status=None, error=None) -> None:
    """Attach a push outcome to its pre-flight audit rows. Never raises."""
    if not audit_ids:
        return
    try:
        from sync_audit import record_push_result
        record_push_result(audit_ids, http_status=http_status, error=error)
    except Exception:
        pass  # auditing must never break a sync


def _relay_push_batches(
    agent_key: str,
    entries: list[dict],
    client_id: str,
    *,
    stream_label: str = "entries",
    bulk: bool = False,
    snapshot_bulk: bool = False,
    mark_ids: Optional[list] = None,
    on_mark_cleared=None,
    on_batch_pushed: Optional[Callable[[list, int], None]] = None,
) -> dict:
    """Push relay entries in batches and return diagnostics."""
    from pipeline import __version__, _relay_log, get_relay_push_settings

    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}

    settings = get_relay_push_settings(bulk=bulk, snapshot_bulk=snapshot_bulk)
    batch_size = settings["batch_size"]
    timeout_seconds = settings["timeout_seconds"]
    max_attempts = settings["max_attempts"]
    retry_base_seconds = settings["retry_base_seconds"]

    total_pushed = 0
    last_error: Optional[str] = None
    throttled = False
    timeout_failures = 0
    total_batches = (len(entries) + batch_size - 1) // batch_size

    batch_started = time.monotonic()
    _relay_log(_format_push_pending_banner(stream_label, len(entries), total_batches, batch_size))
    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        batch_num = i // batch_size + 1
        batch_mark = mark_ids[i : i + batch_size] if mark_ids else None
        body = json.dumps({"client_id": client_id, "entries": batch}).encode()
        body_kb = len(body) / 1024
        # Log what we are about to send BEFORE sending it. A push that fails, is
        # dropped, or silently no-ops then still leaves a trace -- which is the
        # exact failure mode that makes "why didn't this sync?" unanswerable today.
        try:
            from sync_audit import record_push
            audit_ids = record_push(
                batch, batch_label=f"{stream_label} {batch_num}/{total_batches}"
            )
        except Exception:
            audit_ids = []  # auditing must never break a sync
        _relay_log(
            f"{_ARROW_PUSH} {_stream_pad(stream_label)}: "
            f"sending {_page_label(batch_num, total_batches)} "
            f"({len(batch):,} entries, {body_kb:.0f} KB) ..."
        )
        batch_t0 = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            req = urllib.request.Request(
                f"{RELAY_URL}/push",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {agent_key}",
                    "User-Agent": f"Outreach Magic/{__version__}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    result = json.loads(resp.read())
                    count = int(result.get("pushed", 0) or 0)
                    total_pushed += count
                    last_error = None
                    _audit_result(audit_ids, http_status=resp.status)
                    written = int(result.get("snapshot_upserts", 0) or 0)
                    unchanged = int(result.get("snapshot_skipped_unchanged", 0) or 0)
                    elapsed = time.monotonic() - batch_t0
                    detail = ""
                    if written or unchanged:
                        detail = f", {written} written, {unchanged} unchanged"
                    _relay_log(
                        _format_push_progress(
                            stream_label,
                            page_n=batch_num,
                            total_pages=total_batches,
                            page_len=len(batch),
                            seen=total_pushed,
                            total=len(entries),
                            elapsed=elapsed,
                            extra=detail,
                        )
                    )
                    if on_batch_pushed and count > 0:
                        on_batch_pushed(batch, count)
                    if batch_mark and on_mark_cleared and count >= len(batch):
                        on_mark_cleared(batch_mark)
                    elif batch_mark and on_mark_cleared and count > 0:
                        print(
                            f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(stream_label)}: "
                            f"{_page_label(batch_num, total_batches)} — "
                            f"partial ({count}/{len(batch)}); remaining kept for retry",
                            flush=True,
                        )
                    if result.get("truncated"):
                        print(
                            f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(stream_label)}: "
                            f"{_page_label(batch_num, total_batches)} — "
                            "warning: relay capped request; retry sync for remainder",
                            flush=True,
                        )
                    break
            except urllib.error.HTTPError as exc:
                body_text = ""
                try:
                    body_text = (exc.read() or b"").decode("utf-8", errors="replace").strip()
                except Exception:
                    body_text = ""
                retry_after_raw = ""
                try:
                    retry_after_raw = (exc.headers.get("Retry-After") or "").strip()
                except Exception:
                    retry_after_raw = ""
                retry_after = 0
                if retry_after_raw.isdigit():
                    retry_after = int(retry_after_raw)
                throttled = exc.code == 429
                retryable = throttled or 500 <= exc.code <= 599
                if retryable and attempt < max_attempts:
                    wait_s = retry_after if retry_after > 0 else retry_base_seconds * attempt
                    time.sleep(wait_s)
                    continue
                hint = f" (retry_after={retry_after_raw}s)" if retry_after_raw else ""
                detail = f": {body_text}" if body_text else ""
                last_error = f"relay push HTTP {exc.code}{hint}{detail}"
                if throttled:
                    last_error += f" — buffer cap reached; events not stored. Upgrade at {BILLING_UPGRADE_URL}"
                break
            except urllib.error.URLError as exc:
                reason_text = str(exc.reason or exc).strip()
                timed_out = "timed out" in reason_text.lower()
                if timed_out:
                    timeout_failures += 1
                if timed_out and attempt < max_attempts:
                    time.sleep(retry_base_seconds * attempt)
                    continue
                last_error = f"relay push failed: {reason_text}"
                break
            except Exception as exc:
                err_text = str(exc).strip()
                timed_out = "timed out" in err_text.lower()
                if timed_out:
                    timeout_failures += 1
                if timed_out and attempt < max_attempts:
                    time.sleep(retry_base_seconds * attempt)
                    continue
                last_error = f"relay push failed: {exc}"
                break
        if last_error:
            _audit_result(audit_ids, error=last_error)
            break

    recommendation: Optional[str] = None
    if last_error and ("timed out" in last_error.lower() or throttled):
        suggestion = max(10, min(batch_size // 2, RELAY_PUSH_ROUTINE_MAX))
        recommendation = (
            "Try smaller sync batches and/or longer timeout: "
            f"OUTREACHMAGIC_SYNC_BATCH_SIZE={suggestion} "
            f"OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS={min(timeout_seconds + 30, 300)}"
        )
    elapsed_total = time.monotonic() - batch_started
    if not last_error and total_pushed > 0:
        _relay_log(_format_push_done(stream_label, total_pushed, total_batches, elapsed_total))
    elif last_error:
        partial = f" ({total_pushed:,} pushed before failure)" if total_pushed else ""
        _relay_log(
            f"{_ARROW_PUSH} {_stream_pad(stream_label)}: failed{partial} — {last_error}"
        )
    return {
        "pushed": total_pushed,
        "error": last_error,
        "throttled": throttled,
        "timeouts": timeout_failures,
        "recommendation": recommendation,
    }


def _push_agent_events_to_relay(agent_key: str) -> dict:
    """Push locally-created events to the cloud relay's /push endpoint.

    Lead core/workspace snapshots are always pushed separately by
    _push_pending_lead_snapshots (last_sync-cursor based, covers both the
    first full sync and incremental ones). Building them here too was
    redundant work — and for locally-created leads it never converged
    because unsynced_lead_clause only clears once a lead has come in FROM
    relay, so this path re-exported the same never-relay-seen leads on
    every single sync. This path stays events-only.
    """
    from pipeline import _relay_push_batches

    _relay_log(
        f"{_ARROW_PUSH} {_stream_pad(_RELAY_STREAM_EVENT)}: "
        "building export (events only) ..."
    )
    t0 = time.monotonic()
    export = export_local_changes(events_only=True)
    entries = export.get("entries") or []
    _relay_log(
        f"{_ARROW_PUSH} {_stream_pad(_RELAY_STREAM_EVENT)}: "
        f"export ready — {len(entries):,} entries in {time.monotonic() - t0:.1f}s"
    )
    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}
    client_id = export.get("client_id", "unknown")
    marked_event_ids: list[int] = []

    def _on_batch_pushed(batch: list[dict], count: int) -> None:
        if count >= len(batch):
            for entry in batch:
                eid = entry.get("event_id")
                if eid is not None:
                    marked_event_ids.append(int(eid))

    result = _relay_push_batches(
        agent_key,
        entries,
        client_id,
        stream_label=_RELAY_STREAM_EVENT,
        bulk=len(entries) >= RELAY_BULK_THRESHOLD,
        on_batch_pushed=_on_batch_pushed,
    )
    if marked_event_ids:
        from relay_ingest import mark_event_pushed_many

        conn = get_conn()
        mark_event_pushed_many(conn, marked_event_ids)
        conn.commit()
        conn.close()
    result["events_marked_pushed"] = len(marked_event_ids)
    result["events_exported"] = sum(1 for e in entries if e.get("event_id"))
    if result.get("pushed", 0) > 0 and len(marked_event_ids) < result["events_exported"]:
        print(
            f"[{_progress_clock()}] {_ARROW_PUSH} {_stream_pad(_RELAY_STREAM_EVENT)}: "
            f"marked {len(marked_event_ids)}/{result['events_exported']} pushed locally "
            f"({result.get('pushed', 0)} relay units); re-run sync to retry failed pages",
            flush=True,
        )
    return result


def build_merge_delete_sync_payload(merge_entity_key: str, *, timestamp: Optional[str] = None) -> dict:
    """The lead_core_delete relay entry for one merge tombstone.

    Shared by _push_pending_merge_deletes (bulk push) and inspect_sync_merge_delete
    (single-record inspect) so the wire format can't drift between the two.
    """
    return {
        "action": "lead_core_delete",
        "entity_key": merge_entity_key,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "payload": {"reason": "merge"},
    }


def inspect_sync_merge_delete(conn: sqlite3.Connection, identifier: str) -> dict:
    """Full lead_core_delete payload for one merge tombstone, for sync auditing/troubleshooting.

    `identifier` is either a lead_merges.id (int) or a merge_entity_key string.
    """
    row = None
    try:
        merge_row_id = int(identifier)
    except (TypeError, ValueError):
        merge_row_id = None
    if merge_row_id is not None:
        row = conn.execute(
            """SELECT id, keep_id, merge_id, reason, merge_entity_key, relay_delete_pushed
               FROM lead_merges WHERE id = ?""",
            (merge_row_id,),
        ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT id, keep_id, merge_id, reason, merge_entity_key, relay_delete_pushed
               FROM lead_merges WHERE merge_entity_key = ? ORDER BY merged_at DESC LIMIT 1""",
            (identifier,),
        ).fetchone()
    if not row:
        return {}
    return {
        "merge_id": row["id"],
        "keep_lead_id": row["keep_id"],
        "merged_lead_id": row["merge_id"],
        "reason": row["reason"],
        "relay_delete_pushed": bool(row["relay_delete_pushed"]),
        "full_sync_payload": build_merge_delete_sync_payload(row["merge_entity_key"]),
    }


def _push_pending_merge_deletes(
    agent_key: str,
    *,
    bulk: bool = False,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Push tombstones for merged leads so relay drops stale entity keys."""
    from pipeline import _relay_push_batches

    conn = get_conn()
    where_clause = """WHERE merge_entity_key IS NOT NULL AND TRIM(merge_entity_key) != ''
             AND COALESCE(relay_delete_pushed, 0) = 0"""
    total_pending = conn.execute(
        f"SELECT COUNT(*) AS n FROM lead_merges {where_clause}"
    ).fetchone()["n"]
    limit_clause = " LIMIT ?" if sample_limit else ""
    query_params = [sample_limit] if sample_limit else []
    rows = conn.execute(
        f"SELECT id, merge_entity_key FROM lead_merges {where_clause}{limit_clause}",
        query_params,
    ).fetchall()
    if not rows:
        conn.close()
        return {"pushed": 0, "error": None, "total_pending": total_pending, "sample_entries": []}

    now_ts = datetime.now(timezone.utc).isoformat()
    entries = [
        build_merge_delete_sync_payload(row["merge_entity_key"], timestamp=now_ts)
        for row in rows
    ]
    mark_ids = [row["id"] for row in rows]
    conn.close()

    if dry_run:
        return {
            "pushed": 0,
            "error": None,
            "total_pending": total_pending,
            "sample_entries": entries,
        }

    client_id = get_or_create_client_id()

    def clear_merge_ids(ids: list) -> None:
        if not ids:
            return
        mark_conn = get_conn()
        ph = ",".join("?" for _ in ids)
        mark_conn.execute(
            f"UPDATE lead_merges SET relay_delete_pushed = 1 WHERE id IN ({ph})",
            ids,
        )
        mark_conn.commit()
        mark_conn.close()

    result = _relay_push_batches(
        agent_key,
        entries,
        client_id,
        stream_label="merge_delete",
        bulk=bulk,
        snapshot_bulk=True,
        mark_ids=mark_ids,
        on_mark_cleared=clear_merge_ids,
    )
    result["total_pending"] = total_pending
    return result


def _log_selector_divergence(conn, outbox_core: int, outbox_ws: int) -> None:
    """Cutover step 2: run the old cursor alongside the outbox for one release.

    The old selector picking rows the outbox did not = a bug in the triggers.
    The outbox picking rows the old one did not = the ~40% the cursor was
    silently dropping, which is the entire point of this stage. Counts only --
    a full set-difference over 150k leads on every sync is not worth it.
    """
    last_sync = get_last_sync()
    if not last_sync:
        return
    try:
        old_core = conn.execute(
            f"""SELECT COUNT(*) AS n FROM leads
               WHERE (updated_at > ? AND NOT {relay_bump_explained_clause('leads.id', 'leads.updated_at')})
                  OR {unsynced_lead_clause('leads')}""",
            (last_sync,),
        ).fetchone()["n"]
        old_ws = conn.execute(
            f"""SELECT COUNT(*) AS n FROM workspace_leads wl
               WHERE (wl.updated_at > ? AND NOT {relay_bump_explained_clause('wl.lead_id', 'wl.updated_at')})
                  OR {unsynced_workspace_lead_clause('wl')}""",
            (last_sync,),
        ).fetchone()["n"]
    except sqlite3.Error:
        return
    if old_core != outbox_core or old_ws != outbox_ws:
        _relay_log(
            f"selector divergence: outbox core={outbox_core:,} ws={outbox_ws:,} | "
            f"old cursor core={old_core:,} ws={old_ws:,}"
        )


def _push_pending_lead_snapshots(
    agent_key: str,
    *,
    bulk: Optional[bool] = None,
    workspace: Optional[str] = None,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Push pending lead core + workspace snapshots to relay /push.

    When ``workspace`` is provided, only snapshots scoped to that workspace
    are pushed. Pass ``None`` to push everything (default).

    sample_limit caps how many core/workspace rows are fetched/built (used
    by preview_sync for a fast --dry-run spot check). dry_run builds the
    (capped) entries but skips the actual relay push.
    """
    from pipeline import _relay_push_batches

    conn = get_conn()

    ws_id = None
    if workspace:
        ws_row = resolve_workspace_identity(conn, workspace)
        ws_id = ws_row["id"] if ws_row else None
        if ws_id is None:
            conn.close()
            return {"pushed": 0, "error": f"workspace not found: {workspace}", "throttled": False}

    # Selection comes from the outbox, which the triggers maintain at write time.
    # The old `leads.updated_at > last_sync` cursor silently dropped every write
    # that did not touch the parent row (provider attempts, verification) and,
    # via relay_bump_explained_clause, every write to a lead that had since
    # received a webhook (tags). It is kept alongside only to log divergence for
    # one release -- see _log_selector_divergence.
    core_dirty = outbox.select_dirty(conn, "lead_core", limit=sample_limit)
    ws_dirty = outbox.select_dirty(conn, "lead_workspace", limit=sample_limit)

    core_rows = [{"id": int(r["entity_id"]), "dirty_at": r["dirty_at"]} for r in core_dirty]

    ws_rows = []
    for r in ws_dirty:
        lead_part, _, wsid = str(r["entity_id"]).partition(":")
        if not wsid:
            continue
        ws_rows.append({
            "lead_id": int(lead_part),
            "workspace_id": wsid,
            "dirty_at": r["dirty_at"],
            "entity_id": r["entity_id"],
        })

    if ws_id is not None:
        members = {
            row["lead_id"]
            for row in conn.execute(
                "SELECT lead_id FROM workspace_leads WHERE workspace_id = ?", (ws_id,)
            ).fetchall()
        }
        core_rows = [r for r in core_rows if r["id"] in members]
        ws_rows = [r for r in ws_rows if r["workspace_id"] == ws_id]

    slug_by_id = {
        row["id"]: row["slug"]
        for row in conn.execute("SELECT id, slug FROM workspaces").fetchall()
    }
    for r in ws_rows:
        r["slug"] = slug_by_id.get(r["workspace_id"])
    ws_rows = [r for r in ws_rows if r["slug"]]

    _log_selector_divergence(conn, len(core_rows), len(ws_rows))

    if not core_rows and not ws_rows:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False}

    core_shadow = outbox.load_shadow(conn, "lead_core")
    ws_shadow = outbox.load_shadow(conn, "lead_workspace")

    _relay_log(
        f"snapshots: {len(core_rows):,} lead core + {len(ws_rows):,} workspace rows pending"
    )
    lead_ids = sorted({r["id"] for r in core_rows} | {r["lead_id"] for r in ws_rows})
    t_prefetch = time.monotonic()
    prefetch = _load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, lead_ids)
    _relay_log(f"snapshots: prefetched {len(lead_ids):,} leads in {time.monotonic() - t_prefetch:.1f}s")
    client_id = get_or_create_client_id()

    core_entries: list[dict] = []
    core_synced: list[tuple] = []   # (entity_id, entity_key, ws_slug, payload) -> sync_shadow on ack
    core_drop: list[str] = []       # echoes / unbuildable -> clear the outbox row, push nothing
    t_core = time.monotonic()
    for n, row in enumerate(core_rows, start=1):
        lead_id = row["id"]
        entity_key = entity_key_from_prefetch(prefetch, lead_id) or lead_entity_key(
            conn, DEFAULT_ORG_ID, lead_id,
        )
        if not entity_key:
            # No matchable identity -- the relay has nowhere to file it. Clearing
            # the row stops it being rebuilt on every sync forever.
            core_drop.append(str(lead_id))
            continue
        payload = build_lead_core_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, prefetch=prefetch,
        )
        if not payload:
            core_drop.append(str(lead_id))
            continue
        if outbox.is_echo(core_shadow, entity_key, None, payload):
            core_drop.append(str(lead_id))
            continue
        core_entries.append({
            "action": "lead_core_update",
            "entity_key": entity_key,
            # dirty_at, not leads.updated_at: 0014 made the relay reject stale
            # writes on source_updated_at_ms, and 40.7% of updated_at is older
            # than its own created_at -- sending it gets the write rejected.
            "timestamp": normalize_relay_timestamp(row["dirty_at"]),
            "as_of": snapshot_as_of(),
            "payload": payload,
        })
        core_synced.append((str(lead_id), entity_key, None, payload))
        if n % 2500 == 0:
            _relay_log(f"snapshots: built {n:,}/{len(core_rows):,} lead_core payloads ...")
    _relay_log(
        f"snapshots: {len(core_entries):,} lead_core entries in {time.monotonic() - t_core:.1f}s"
    )

    ws_entries: list[dict] = []
    ws_synced: list[tuple] = []
    ws_drop: list[str] = []
    t_ws = time.monotonic()
    for n, row in enumerate(ws_rows, start=1):
        lead_id = row["lead_id"]
        entity_id = row["entity_id"]
        entity_key = entity_key_from_prefetch(prefetch, lead_id) or lead_entity_key(
            conn, DEFAULT_ORG_ID, lead_id,
        )
        if not entity_key:
            ws_drop.append(entity_id)
            continue
        ws_slug = row["slug"]
        payload = build_lead_workspace_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug=ws_slug, prefetch=prefetch,
        )
        if not payload:
            ws_drop.append(entity_id)
            continue
        if outbox.is_echo(ws_shadow, entity_key, ws_slug, payload):
            ws_drop.append(entity_id)
            continue
        ws_entries.append({
            "action": "lead_workspace_update",
            "entity_key": entity_key,
            "workspace": ws_slug,
            "timestamp": normalize_relay_timestamp(row["dirty_at"]),
            "as_of": snapshot_as_of(),
            "payload": payload,
        })
        ws_synced.append((entity_id, entity_key, ws_slug, payload))
        if n % 2500 == 0:
            _relay_log(f"snapshots: built {n:,}/{len(ws_rows):,} workspace payloads ...")
    _relay_log(
        f"snapshots: {len(ws_entries):,} workspace entries in {time.monotonic() - t_ws:.1f}s"
    )

    if dry_run:
        conn.close()
        return {
            "pushed": 0,
            "error": None,
            "sample_core_entries": core_entries,
            "sample_ws_entries": ws_entries,
        }

    # Echoes and unpushable rows never reach the relay, but they must leave the
    # outbox -- otherwise every sync rebuilds the same payloads forever.
    if core_drop or ws_drop:
        outbox.drop_clean(conn, "lead_core", core_drop)
        outbox.drop_clean(conn, "lead_workspace", ws_drop)
        conn.commit()
        _relay_log(
            f"snapshots: dropped {len(core_drop):,} core + {len(ws_drop):,} workspace "
            "rows as echoes/unpushable"
        )
    conn.close()

    pending_total = len(core_entries) + len(ws_entries)
    if bulk is None:
        bulk = pending_total >= RELAY_BULK_THRESHOLD
    batch_sz = get_relay_push_settings(bulk=bulk, snapshot_bulk=True)["batch_size"]
    _relay_log(
        f"snapshots: pushing {pending_total:,} entries to relay "
        f"(bulk={bulk}, batch_size={batch_sz}) ..."
    )
    total_pushed = 0
    last_result: dict = {"pushed": 0, "error": None, "throttled": False}

    def _settle(entity_type: str, synced: list[tuple], result: dict) -> None:
        """Outbox rows clear only on a relay ack. A failed push leaves them
        dirty (with backoff), so nothing is lost to a network blip.

        A long push dies part-way (a 149k-entry drain hit `Connection reset by
        peer` at page 321 of 747). The batches that *did* land must settle as
        synced -- marking all 149k failed re-pushes 64k rows that the relay
        already has. _relay_push_batches pushes entries in order, so the first
        `pushed` of them are the ones that made it.
        """
        pushed = int(result.get("pushed", 0) or 0)
        ok, failed = synced[:pushed], synced[pushed:]
        c = get_conn()
        try:
            if ok:
                outbox.record_synced(c, entity_type, ok)
            if failed and result.get("error"):
                outbox.record_failure(
                    c, entity_type, [r[0] for r in failed], str(result["error"])
                )
            c.commit()
        finally:
            c.close()

    if core_entries:
        last_result = _relay_push_batches(
            agent_key,
            core_entries,
            client_id,
            stream_label=_SNAPSHOT_KIND_STREAM["core"],
            bulk=bulk,
            snapshot_bulk=True,
        )
        total_pushed += int(last_result.get("pushed", 0) or 0)
        _settle("lead_core", core_synced, last_result)
        if last_result.get("error"):
            last_result["pushed"] = total_pushed
            return last_result

    if ws_entries:
        ws_result = _relay_push_batches(
            agent_key,
            ws_entries,
            client_id,
            stream_label=_SNAPSHOT_KIND_STREAM["workspace"],
            bulk=bulk,
            snapshot_bulk=True,
        )
        total_pushed += int(ws_result.get("pushed", 0) or 0)
        last_result = ws_result
        _settle("lead_workspace", ws_synced, ws_result)
        if ws_result.get("error"):
            last_result["pushed"] = total_pushed
            return last_result

    last_result["pushed"] = total_pushed
    return last_result


def _push_outbox_entity(
    agent_key: str,
    *,
    entity_type: str,
    action: str,
    stream_key: str,
    entity_key_fn,
    payload_fn,
    coerce_id=lambda v: v,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """One drain for every non-lead entity.

    Companies and senders had the same defect as leads, one level down:
    company_personalization writes bump no parent timestamp, so a
    `companies.updated_at > last_sync` cursor never selected them. The triggers
    record the dirt; this reads it. Same anti-echo, same ack-before-clear rule
    as the lead path.
    """
    from pipeline import _relay_push_batches

    conn = get_conn()
    # select_dirty caps at sample_limit for the preview/dry-run path, so total_pending
    # must come from its own uncapped COUNT -- otherwise a dry-run preview always
    # reports at most sample_limit pending regardless of the real backlog size.
    total_pending = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = ? AND op = 'upsert' "
        "AND dirty_at <= datetime('now')",
        (entity_type,),
    ).fetchone()["n"]
    dirty = outbox.select_dirty(conn, entity_type, limit=sample_limit)
    if not dirty:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False,
                "total_pending": total_pending, "sample_entries": []}

    shadow = outbox.load_shadow(conn, entity_type)
    entries: list[dict] = []
    synced: list[tuple] = []
    drop: list[str] = []

    for row in dirty:
        raw_id = row["entity_id"]
        try:
            eid = coerce_id(raw_id)
        except (TypeError, ValueError):
            drop.append(raw_id)
            continue
        entity_key = entity_key_fn(conn, eid)
        if not entity_key:
            drop.append(raw_id)
            continue
        payload = payload_fn(conn, eid)
        if not payload:
            drop.append(raw_id)
            continue
        if outbox.is_echo(shadow, entity_key, None, payload):
            drop.append(raw_id)
            continue
        entries.append({
            "action": action,
            "entity_key": entity_key,
            # dirty_at, not updated_at -- see outbox.py.
            "timestamp": normalize_relay_timestamp(row["dirty_at"]),
            "as_of": snapshot_as_of(),
            "payload": payload,
        })
        synced.append((raw_id, entity_key, None, payload))

    if dry_run:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False,
                "total_pending": total_pending, "sample_entries": entries}

    if drop:
        outbox.drop_clean(conn, entity_type, drop)
        conn.commit()
    conn.close()

    if not entries:
        return {"pushed": 0, "error": None, "throttled": False,
                "total_pending": total_pending, "sample_entries": []}

    client_id = get_or_create_client_id()
    bulk = len(entries) >= RELAY_BULK_THRESHOLD
    push_result = _relay_push_batches(
        agent_key,
        entries,
        client_id,
        stream_label=_SNAPSHOT_KIND_STREAM[stream_key],
        bulk=bulk,
        snapshot_bulk=True,
    )

    # Partial success is the normal case on a long drain -- settle what landed.
    pushed_n = int(push_result.get("pushed", 0) or 0)
    ok, failed = synced[:pushed_n], synced[pushed_n:]
    c = get_conn()
    try:
        if ok:
            outbox.record_synced(c, entity_type, ok)
        if failed and push_result.get("error"):
            outbox.record_failure(
                c, entity_type, [r[0] for r in failed], str(push_result["error"])
            )
        c.commit()
    finally:
        c.close()

    push_result.setdefault("total_pending", total_pending)
    return push_result


def _push_pending_company_updates(
    agent_key: str,
    *,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    return _push_outbox_entity(
        agent_key,
        entity_type="company",
        action="company_update",
        stream_key="company",
        entity_key_fn=company_entity_key,
        payload_fn=build_company_sync_payload,
        coerce_id=int,
        sample_limit=sample_limit,
        dry_run=dry_run,
    )


def _push_pending_sender_account_updates(
    agent_key: str,
    *,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    return _push_outbox_entity(
        agent_key,
        entity_type="sender_account",
        action="sender_account_update",
        stream_key="sender_account",
        entity_key_fn=sender_account_entity_key,
        payload_fn=build_sender_account_sync_payload,
        coerce_id=int,
        sample_limit=sample_limit,
        dry_run=dry_run,
    )


def _push_pending_sender_domain_updates(
    agent_key: str,
    *,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    # sender_domain_entity_key takes the domain alone -- no conn.
    return _push_outbox_entity(
        agent_key,
        entity_type="sender_domain",
        action="sender_domain_update",
        stream_key="sender_domain",
        entity_key_fn=lambda conn, domain: sender_domain_entity_key(domain),
        payload_fn=build_sender_domain_sync_payload,
        sample_limit=sample_limit,
        dry_run=dry_run,
    )


def list_campaign_maps(org_id: str = DEFAULT_ORG_ID) -> list[dict]:
    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    if config.mode == WORKSPACE_ROUTING_MULTI:
        rows = conn.execute(
            """SELECT m.*, w.name AS workspace_name FROM campaign_workspace_map m
               JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.org_id = ? AND w.slug != 'default'
               ORDER BY m.priority, m.campaign_name_normalized""",
            (org_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT m.*, w.name AS workspace_name FROM campaign_workspace_map m
               JOIN workspaces w ON w.id = m.workspace_id WHERE m.org_id = ?
               ORDER BY m.priority, m.campaign_name_normalized""",
            (org_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_campaign_map_cli(
    platform: str = "*",
    workspace_slug: str = "",
    *,
    campaign_platform_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: Optional[str] = None,
    priority: int = 100,
) -> dict:
    if not campaign_platform_id and not campaign_name:
        return {"status": "error", "error": "provide --campaign-platform-id or --campaign-name"}
    conn_check = get_conn()
    config = get_org_routing_config(conn_check, DEFAULT_ORG_ID)
    conn_check.close()
    if config.mode == WORKSPACE_ROUTING_MULTI and workspace_slug == "default":
        return {"status": "error", "error": "Cannot route to the default workspace in multi-workspace mode."}
    strategy = match_strategy or ("id_exact" if campaign_platform_id else "name_exact")
    tok = get_agent_key()
    cloud_ok = routing_cloud.cloud_routing_enabled(load_config, tok)
    cloud_synced = False
    cloud_warning: Optional[str] = None
    if cloud_ok:
        try:
            routing_cloud.push_campaign_map(
                routing_cloud.get_api_base(load_config),
                tok,
                source_platform=platform,
                workspace_slug=workspace_slug,
                campaign_platform_id=campaign_platform_id,
                campaign_name=campaign_name,
                match_strategy=strategy,
                priority=priority,
            )
            cloud_synced = True
        except RuntimeError as exc:
            cloud_warning = str(exc)
    conn = get_conn()
    ws = conn.execute(
        "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
        (DEFAULT_ORG_ID, workspace_slug),
    ).fetchone()
    if not ws:
        conn.close()
        return {"status": "error", "error": f"workspace not found: {workspace_slug}"}
    # Manual rows keep map_source='manual'; a manual add must never claim
    # single_mode_backfill provenance (only the one-shot migration does).
    map_id = assign_campaign_map(
        conn,
        DEFAULT_ORG_ID,
        source_platform=platform,
        workspace_id=ws["id"],
        campaign_platform_id=campaign_platform_id,
        campaign_name=campaign_name,
        match_strategy=strategy,
        priority=priority,
    )
    if cloud_synced:
        conn.execute("UPDATE campaign_workspace_map SET cloud_synced = 1 WHERE id = ?", (map_id,))
    result = {"status": "created", "map_id": map_id, "workspace_id": ws["id"]}
    if strategy in ("rule_contains", "rule_prefix", "rule_regex") and campaign_name:
        deactivated = deactivate_shadowed_backfill_rules(
            conn, DEFAULT_ORG_ID,
            source_platform=platform,
            match_strategy=strategy,
            pattern=campaign_name,
        )
        result["deactivated_shadowed_rules"] = deactivated
        # A manual name_exact row still shadows this new rule (undecidable to
        # auto-clear) -- surface it so the user can act via campaign-map deactivate.
        unresolved = [
            c for c in detect_shadow_conflicts(conn, DEFAULT_ORG_ID, source_platform=platform)
            if c["shadowing_rule_map_id"] == map_id
        ]
        if unresolved:
            result["unresolved_conflicts"] = unresolved
    conn.commit()
    conn.close()
    if cloud_warning:
        result["cloud_warning"] = cloud_warning
    return result


def _workspace_slug_lookup(conn: sqlite3.Connection, org_id: str = DEFAULT_ORG_ID) -> dict:
    rows = conn.execute(
        "SELECT id, slug FROM workspaces WHERE org_id = ?", (org_id,)
    ).fetchall()
    return {r["id"]: r["slug"] for r in rows}


def campaign_map_conflicts_cli(platform: Optional[str] = None) -> dict:
    """List active name_exact rows shadowed by a broader rule pointing elsewhere."""
    conn = get_conn()
    try:
        conflicts = detect_shadow_conflicts(conn, DEFAULT_ORG_ID, source_platform=platform)
        slugs = _workspace_slug_lookup(conn)
    finally:
        conn.close()
    for c in conflicts:
        c["name_exact_workspace_slug"] = slugs.get(c["name_exact_workspace_id"])
        c["shadowing_rule_workspace_slug"] = slugs.get(c["shadowing_rule_workspace_id"])
    return {"conflicts": conflicts, "count": len(conflicts)}


def deactivate_campaign_map_cli(map_id: str) -> dict:
    """Soft-deactivate one campaign_workspace_map row by id."""
    conn = get_conn()
    try:
        result = deactivate_campaign_map(conn, DEFAULT_ORG_ID, map_id)
        conn.commit()
    finally:
        conn.close()
    return result


def reconcile_campaign_routing_cli(
    *,
    platform: Optional[str] = None,
    workspace_slug: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 0,
) -> dict:
    """Re-apply routing rules to already-ingested workspace_lead_events.

    Operator flow for a database already carrying the shadowing bug:
      campaign-map conflicts  ->  campaign-map deactivate --id X  ->  campaign-map reconcile
    Reconcile reuses resolve_workspace() unchanged, so it keeps producing the
    stale target until the shadowing name_exact row is deactivated.
    """
    conn = get_conn()
    try:
        from_workspace_id = None
        if workspace_slug:
            ws = resolve_workspace_identity(conn, workspace_slug, org_id=DEFAULT_ORG_ID)
            if not ws:
                return {"status": "error", "error": f"workspace not found: {workspace_slug}"}
            from_workspace_id = ws["id"]
        result = reconcile_workspace_routing(
            conn, DEFAULT_ORG_ID,
            platform_filter=platform,
            from_workspace_id=from_workspace_id,
            dry_run=dry_run,
            limit=limit,
        )
        slugs = _workspace_slug_lookup(conn)
        for m in result.get("moves", []):
            m["from_workspace_slug"] = slugs.get(m["from_workspace"])
            m["to_workspace_slug"] = slugs.get(m["to_workspace"])
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    result["status"] = "ok"
    return result


def list_quarantine(
    org_id: str = DEFAULT_ORG_ID,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    conn = get_conn()
    if status == "all":
        rows = conn.execute(
            """SELECT id, source_platform, campaign_platform_id, campaign_name_raw,
                      campaign_name_normalized, external_event_id, reason, status,
                      assigned_workspace, received_at, resolved_at
               FROM unmapped_campaign_queue
               WHERE org_id = ?
               ORDER BY received_at DESC LIMIT ?""",
            (org_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, source_platform, campaign_platform_id, campaign_name_raw,
                      campaign_name_normalized, external_event_id, reason, status,
                      assigned_workspace, received_at, resolved_at
               FROM unmapped_campaign_queue
               WHERE org_id = ? AND status = ?
               ORDER BY received_at DESC LIMIT ?""",
            (org_id, status, limit),
        ).fetchall()
    conn.close()
    out = []
    for row in rows:
        item = dict(row)
        if item.get("reason") == "no_campaign_map":
            ctx = extract_campaign_context(
                item["source_platform"],
                {},
                {
                    "campaign_id": item.get("campaign_platform_id"),
                    "campaign_name": item.get("campaign_name_raw"),
                },
            )
            item["message"] = format_unmapped_campaign_message(ctx)
        elif item.get("reason") == "no_campaign_id":
            ctx = extract_campaign_context(item["source_platform"], {}, {})
            item["message"] = format_no_campaign_event_message(ctx)
        out.append(item)
    return out


def get_quarantine_campaign_summary(
    org_id: str = DEFAULT_ORG_ID,
    status: str = "pending",
) -> list[dict]:
    """Aggregate quarantine queue by platform + campaign label."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT
               source_platform,
               COALESCE(NULLIF(campaign_name_raw, ''), NULLIF(campaign_platform_id, ''), 'unknown') AS campaign,
               campaign_platform_id,
               COUNT(*) AS event_count,
               MIN(received_at) AS oldest_received_at,
               MAX(received_at) AS newest_received_at,
               GROUP_CONCAT(id) AS queue_ids_raw
           FROM unmapped_campaign_queue
           WHERE org_id = ? AND status = ?
           GROUP BY source_platform, campaign
           ORDER BY event_count DESC, source_platform ASC, campaign ASC""",
        (org_id, status),
    ).fetchall()
    conn.close()
    out = []
    for row in rows:
        item = dict(row)
        raw_ids = (item.pop("queue_ids_raw") or "").split(",")
        item["queue_ids"] = [x for x in raw_ids if x]
        out.append(item)
    return out


def _format_queue_id_sample(queue_ids: list[str], max_show: int = 3) -> str:
    if not queue_ids:
        return "—"
    shown = queue_ids[:max_show]
    text = ", ".join(shown)
    extra = len(queue_ids) - len(shown)
    if extra > 0:
        text += f" (+{extra} more)"
    return text


def format_quarantine_campaign_summary(
    campaigns: list[dict],
    *,
    include_steps: bool = True,
) -> str:
    if not campaigns:
        return "No pending quarantined events."

    platform_w = max(len("Platform"), *(len(str(r.get("source_platform") or "")) for r in campaigns))
    campaign_w = max(len("Campaign"), *(len(str(r.get("campaign") or "")) for r in campaigns))
    count_w = max(len("Events"), *(len(str(r.get("event_count") or 0)) for r in campaigns))
    id_samples = [_format_queue_id_sample(r.get("queue_ids") or []) for r in campaigns]
    ids_w = max(len("Queue IDs (sample)"), *(len(s) for s in id_samples))

    total_events = sum(int(r.get("event_count") or 0) for r in campaigns)
    lines = [
        f"Pending quarantine: {total_events} event(s) across {len(campaigns)} campaign(s).",
        "Use quarantine list --json for all queue IDs.",
        "",
        f"{'Platform':<{platform_w}}  {'Campaign':<{campaign_w}}  {'Events':>{count_w}}  {'Queue IDs (sample)':<{ids_w}}",
        "-" * (platform_w + campaign_w + count_w + ids_w + 6),
    ]
    for row, id_sample in zip(campaigns, id_samples):
        lines.append(
            f"{(row.get('source_platform') or ''):<{platform_w}}  "
            f"{(row.get('campaign') or 'unknown'):<{campaign_w}}  "
            f"{int(row.get('event_count') or 0):>{count_w}}  "
            f"{id_sample:<{ids_w}}"
        )

    if include_steps:
        from user_messages import quarantine_summary_steps

        lines.extend(quarantine_summary_steps())

    return "\n".join(lines)


def print_quarantine_guidance() -> None:
    routing = get_workspace_routing()
    pending = int(routing.get("pending_quarantine") or 0)
    if routing.get("mode") != WORKSPACE_ROUTING_MULTI or pending <= 0:
        return
    print(
        f"⚠ {pending} event(s) in quarantine queue "
        f"(`quarantine list` to inspect, `quarantine replay` to reprocess).",
        file=sys.stderr,
    )


def _quarantine_relay_id(row: dict) -> Optional[int]:
    raw = row.get("external_event_id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def skip_quarantine(queue_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, external_event_id FROM unmapped_campaign_queue WHERE id = ? AND status = 'pending'",
        (queue_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "error": "queue item not found or not pending"}
    relay_id = _quarantine_relay_id(dict(row))
    if not relay_id:
        conn.close()
        return {"status": "error", "error": "missing relay id on queue item"}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE unmapped_campaign_queue
           SET status = 'skipped', resolved_at = ?
           WHERE id = ?""",
        (now, queue_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "id": queue_id, "relay_id": relay_id}


def cleanup_stale_quarantine_for_reprocessed(
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Mark pending quarantine rows as 'reprocessed' for events that now have a campaign.

    Called after reprocess to clean up stale entries in unmapped_campaign_queue
    whose corresponding events now have valid campaign mapping.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    updated = conn.execute(
        """UPDATE unmapped_campaign_queue
           SET status = 'skipped', resolved_at = datetime('now')
           WHERE status = 'pending'
             AND external_event_id IS NOT NULL
             AND external_event_id != ''
             AND CAST(external_event_id AS INTEGER) IN (
                 SELECT relay_id FROM events
                 WHERE relay_id IS NOT NULL AND campaign_id IS NOT NULL
             )"""
    ).rowcount
    conn.commit()
    if own_conn:
        conn.close()
    return {"status": "ok", "cleaned": updated}


def skip_quarantine_bulk(
    *,
    campaign_platform_id: Optional[str] = None,
    platform: Optional[str] = None,
    reason: Optional[str] = None,
    all_pending: bool = False,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Skip multiple pending quarantine rows (by campaign platform id, reason, or all pending)."""
    if not all_pending and not campaign_platform_id and not reason:
        return {"status": "error", "error": "specify --campaign-platform-id, --reason, or --all"}
    conn = get_conn()
    sql = (
        "SELECT id, external_event_id FROM unmapped_campaign_queue "
        "WHERE org_id = ? AND status = 'pending'"
    )
    params: list = [org_id]
    if campaign_platform_id:
        sql += " AND campaign_platform_id = ?"
        params.append(campaign_platform_id)
    if reason:
        sql += " AND reason = ?"
        params.append(reason)
    if platform:
        sql += " AND source_platform = ?"
        params.append(platform)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        conn.close()
        return {"status": "ok", "skipped": 0, "ids": []}
    now = datetime.now(timezone.utc).isoformat()
    skipped_ids = []
    errors = []
    for row in rows:
        qid = row["id"]
        relay_id = _quarantine_relay_id(dict(row))
        if not relay_id:
            errors.append({"id": qid, "error": "missing relay id"})
            continue
        conn.execute(
            """UPDATE unmapped_campaign_queue
               SET status = 'skipped', resolved_at = ?
               WHERE id = ? AND status = 'pending'""",
            (now, qid),
        )
        skipped_ids.append(qid)
    conn.commit()
    conn.close()
    out: dict = {"status": "ok", "skipped": len(skipped_ids), "ids": skipped_ids}
    if campaign_platform_id:
        out["campaign_platform_id"] = campaign_platform_id
    if reason:
        out["reason"] = reason
    if platform:
        out["platform"] = platform
    if errors:
        out["errors"] = errors
    return out


def backfill_null_campaign_quarantine(
    *,
    org_id: str = DEFAULT_ORG_ID,
    auto_skip: bool = True,
    quiet: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Move historical events with campaign_id IS NULL into quarantine (skipped by default)."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    rows = conn.execute(
        """SELECT e.id, e.lead_id, e.event_type, e.direction, e.channel, e.subject,
                  e.body_preview, e.metadata_json, e.sender, e.created_at,
                  l.email
           FROM events e
           LEFT JOIN leads l ON l.id = e.lead_id
           WHERE e.campaign_id IS NULL
           ORDER BY e.id""",
    ).fetchall()
    if not rows:
        if own_conn:
            conn.close()
        return {"status": "ok", "found": 0, "quarantined": 0, "skipped": 0}

    now = datetime.now(timezone.utc).isoformat()
    quarantined = 0
    skipped = 0
    for row in rows:
        meta = {}
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        platform = str(meta.get("platform") or meta.get("source_platform") or "relay").strip()
        relay_id = meta.get("relay_id")
        external_id = str(relay_id) if relay_id not in (None, "") else f"local_event_{row['id']}"

        existing = conn.execute(
            """SELECT id, status FROM unmapped_campaign_queue
               WHERE org_id = ? AND external_event_id = ?""",
            (org_id, external_id),
        ).fetchone()
        if existing:
            if auto_skip and existing["status"] == "pending":
                conn.execute(
                    """UPDATE unmapped_campaign_queue
                       SET status = 'skipped', resolved_at = ?
                       WHERE id = ?""",
                    (now, existing["id"]),
                )
                skipped += 1
            continue

        payload = {
            "platform": platform,
            "event_type": row["event_type"],
            "lead": row["email"] or "",
            "received_at": row["created_at"],
            "relay_id": relay_id,
            "raw": meta,
            "backfill_event_id": row["id"],
        }
        ctx = extract_campaign_context(platform, {}, {})
        qid = quarantine_event(
            conn,
            org_id,
            ctx,
            reason="no_campaign_id",
            payload=payload,
            external_event_id=external_id,
        )
        quarantined += 1
        if auto_skip:
            conn.execute(
                """UPDATE unmapped_campaign_queue
                   SET status = 'skipped', resolved_at = ?
                   WHERE id = ?""",
                (now, qid),
            )
            skipped += 1

    if own_conn:
        conn.commit()
        conn.close()
    elif quarantined or skipped:
        pass  # caller owns transaction (e.g. migrate_db)
    result = {
        "status": "ok",
        "found": len(rows),
        "quarantined": quarantined,
        "skipped": skipped,
        "auto_skip": auto_skip,
    }
    if not quiet and quarantined:
        from user_messages import no_campaign_event_message

        print(
            f"Backfilled {quarantined} no-campaign event(s) into quarantine "
            f"({'skipped' if auto_skip else 'pending'}).",
            file=sys.stderr,
        )
        print(no_campaign_event_message(platform="relay"), file=sys.stderr)
    return result


def maybe_backfill_null_campaign_quarantine(
    *,
    quiet: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Run one-time backfill for legacy null-campaign events."""
    cfg = load_config()
    if cfg.get("null_campaign_backfill_at"):
        return {"status": "skipped", "reason": "already_backfilled"}
    result = backfill_null_campaign_quarantine(quiet=quiet, conn=conn)
    if result.get("quarantined", 0) > 0 or result.get("found", 0) == 0:
        cfg = load_config()
        cfg["null_campaign_backfill_at"] = datetime.now(timezone.utc).isoformat()
        save_config(cfg)
    return result


def assign_quarantine(queue_id: str, workspace_slug: str) -> dict:
    conn = get_conn()
    ws = conn.execute(
        "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
        (DEFAULT_ORG_ID, workspace_slug),
    ).fetchone()
    if not ws:
        conn.close()
        return {"status": "error", "error": f"workspace not found: {workspace_slug}"}
    row = conn.execute(
        "SELECT id, external_event_id FROM unmapped_campaign_queue WHERE id = ? AND status = 'pending'",
        (queue_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "error": "queue item not found or not pending"}
    relay_id = _quarantine_relay_id(dict(row))
    if not relay_id:
        conn.close()
        return {"status": "error", "error": "missing relay id on queue item"}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE unmapped_campaign_queue
           SET status = 'assigned', assigned_workspace = ?, resolved_at = ?
           WHERE id = ?""",
        (workspace_slug, now, queue_id),
    )
    conn.commit()
    conn.close()
    return {
        "status": "ok",
        "id": queue_id,
        "relay_id": relay_id,
        "workspace": workspace_slug,
    }


def build_quarantine_resolution_sync_payload(row: dict) -> Optional[dict]:
    """The quarantine-resolution relay entry for one resolved queue row, or None if unpushable.

    Shared by _push_pending_quarantine_resolutions (bulk push) and
    inspect_sync_quarantine_resolution (single-record inspect) so the wire
    format can't drift between the two.
    """
    relay_id = _quarantine_relay_id(row)
    if not relay_id:
        return None
    entry: dict = {
        "relay_id": relay_id,
        "status": row["status"],
        "resolved_at": row.get("resolved_at") or normalize_relay_timestamp(None),
    }
    if row["status"] == "assigned":
        entry["workspace_slug"] = row.get("assigned_workspace")
    return entry


def inspect_sync_quarantine_resolution(conn: sqlite3.Connection, identifier: str) -> dict:
    """Full quarantine-resolution payload for one queue item, for sync auditing/troubleshooting.

    `identifier` matches either unmapped_campaign_queue.id or external_event_id.
    """
    row = conn.execute(
        """SELECT id, external_event_id, status, assigned_workspace, resolved_at,
                  source_platform, campaign_platform_id, campaign_name_raw
           FROM unmapped_campaign_queue
           WHERE id = ? OR external_event_id = ?""",
        (str(identifier), str(identifier)),
    ).fetchone()
    if not row:
        return {}
    row_d = dict(row)
    payload = None
    note = None
    if row_d["status"] in ("skipped", "assigned"):
        payload = build_quarantine_resolution_sync_payload(row_d)
        if payload is None:
            note = "resolved, but missing a usable external_event_id — nothing would be pushed"
    else:
        note = f"not yet resolved (status={row_d['status']!r}) — nothing would be pushed"
    return {
        "queue_id": row_d["id"],
        "external_event_id": row_d["external_event_id"],
        "status": row_d["status"],
        "source_platform": row_d["source_platform"],
        "campaign_name": row_d["campaign_name_raw"],
        "full_sync_payload": payload,
        "note": note,
    }


def _push_pending_quarantine_resolutions(
    agent_key: str,
    *,
    sample_limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    from pipeline import __version__

    conn = get_conn()
    last_sync = get_last_sync()
    where_clause = "status IN ('skipped', 'assigned')"
    where_params: tuple = ()
    if last_sync:
        where_clause = f"resolved_at > ? AND {where_clause}"
        where_params = (last_sync,)
    total_pending = conn.execute(
        f"SELECT COUNT(*) AS n FROM unmapped_campaign_queue WHERE {where_clause}",
        where_params,
    ).fetchone()["n"]
    limit_clause = " LIMIT ?" if sample_limit else ""
    query_params = where_params + ((sample_limit,) if sample_limit else ())
    rows = conn.execute(
        f"""SELECT external_event_id, status, assigned_workspace, resolved_at
           FROM unmapped_campaign_queue WHERE {where_clause}{limit_clause}""",
        query_params,
    ).fetchall()
    resolves: list[dict] = []
    relay_ids_sent: list[int] = []
    for row in rows:
        entry = build_quarantine_resolution_sync_payload(dict(row))
        if not entry:
            continue
        relay_ids_sent.append(entry["relay_id"])
        resolves.append(entry)
    conn.close()

    if dry_run:
        return {"synced": 0, "errors": [], "total_pending": total_pending, "sample_entries": resolves}

    if not resolves:
        return {"synced": 0, "errors": []}

    result = qres.push_resolutions_to_relay(
        RELAY_URL, agent_key, resolves, version=__version__
    )
    if result.get("status") == "error":
        return {"synced": 0, "errors": [{"error": result.get("error")}]}

    errors = result.get("errors") or []
    failed: set[int] = set()
    for err in errors:
        try:
            failed.add(int(err["relay_id"]))
        except (KeyError, TypeError, ValueError):
            pass
    if errors and not failed:
        return {"synced": 0, "errors": errors}

    succeeded = [rid for rid in relay_ids_sent if rid not in failed]
    # Timestamp-based sync: no per-row clear needed; set_last_sync handles it.

    return {"synced": len(succeeded), "errors": errors}
