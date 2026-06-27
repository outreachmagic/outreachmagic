"""
Local SQLite health collection and optional portal telemetry (aggregates only).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from om_paths import get_db_path

# Size thresholds (bytes)
DB_SIZE_WARN_BYTES = 500 * 1024 * 1024
DB_SIZE_CRITICAL_BYTES = 2 * 1024 * 1024 * 1024
INTEGRITY_SKIP_FAST_PATH_BYTES = 500 * 1024 * 1024
TABLE_BREAKDOWN_SKIP_BYTES = 1024 * 1024 * 1024
RELAY_BLOAT_RATIO = 5.0
PENDING_SYNC_LEADS = 50
QUARANTINE_BACKLOG = 5
LOW_ACTIVITY_LEADS = 1000
LOW_ACTIVITY_RATIO = 0.05

HEALTH_REPORT_INTERVAL_SECONDS = 6 * 3600

CONFIG_LAST_HEALTH_REPORT_AT = "last_health_report_at"
CONFIG_LAST_HEALTH_STATUS = "last_health_status"

RULE_HINTS: dict[str, str] = {
    "integrity_fail": "Database integrity check failed. Back up the file; only run init + pull --full with explicit user consent.",
    "db_size_warn": "Local database is large. Ask Outreach Magic to archive this workspace (dry-run first).",
    "db_size_critical": "Local database is very large. Archive old workspaces and consider VACUUM after export.",
    "relay_bloat": "Many relay dedupe keys per lead (normal at scale). Archive inactive workspaces if disk is a concern.",
    "pending_sync": "Leads pending cloud sync. Ask Outreach Magic to sync.",
    "quarantine_backlog": "Unmapped campaigns in queue. Ask Outreach Magic to list quarantine items.",
    "low_activity_bulk": "Many leads with few events (typical after CSV import). Informational only.",
    "verification_gap": "Leads with email outnumber email verification records. If verification was previously run, the structured rows may not have synced to the relay before a fresh pull. Run `sync` before any future DB rebuild, then re-run verification.",
    "verification_status_incomplete": "Email verification rows exist but the consolidated `email_verification_status` is not populated on most leads. Run `sync` to push verification data to the relay, then `refresh --yes` to rebuild and re-import from the cloud with preserved timestamps.",
}

STATUS_RANK = {"ok": 0, "info": 1, "warn": 2, "critical": 3}


def _max_status(current: str, new: str) -> str:
    if STATUS_RANK.get(new, 0) > STATUS_RANK.get(current, 0):
        return new
    return current


def _table_breakdown(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH index_owner AS (
          SELECT name AS idx_name, tbl_name AS table_name
          FROM sqlite_master WHERE type = 'index'
        ),
        page_owner AS (
          SELECT d.pgsize, COALESCE(i.table_name, d.name) AS table_name
          FROM dbstat d
          LEFT JOIN index_owner i ON i.idx_name = d.name
        )
        SELECT table_name, SUM(pgsize) AS bytes
        FROM page_owner
        WHERE table_name NOT LIKE 'sqlite_%'
        GROUP BY table_name
        ORDER BY bytes DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"table": r["table_name"], "bytes": int(r["bytes"] or 0)} for r in rows]


def _workspace_breakdown(conn: sqlite3.Connection, org_id: str) -> list[dict[str, Any]]:
    mode_row = conn.execute(
        "SELECT workspace_routing_mode FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    mode = (mode_row["workspace_routing_mode"] if mode_row else "single") or "single"
    if mode == "multi":
        rows = conn.execute(
            """
            SELECT w.slug, COUNT(wl.lead_id) AS lead_count
            FROM workspace_leads wl
            JOIN workspaces w ON w.id = wl.workspace_id
            WHERE wl.org_id = ?
            GROUP BY w.slug
            ORDER BY lead_count DESC
            LIMIT 20
            """,
            (org_id,),
        ).fetchall()
        return [{"slug": r["slug"], "leadCount": int(r["lead_count"])} for r in rows]

    rows = conn.execute(
        """
        SELECT
          trim(substr(COALESCE(l.original_source_detail, l.latest_source_detail, ''), 1,
            CASE WHEN instr(COALESCE(l.original_source_detail, l.latest_source_detail, ''), '|') > 0
              THEN instr(COALESCE(l.original_source_detail, l.latest_source_detail, ''), '|') - 1
              ELSE length(COALESCE(l.original_source_detail, l.latest_source_detail, ''))
            END)) AS slug,
          COUNT(*) AS lead_count
        FROM leads l
        WHERE COALESCE(l.original_source_detail, l.latest_source_detail, '') LIKE '%|%'
        GROUP BY slug
        HAVING slug != ''
        ORDER BY lead_count DESC
        LIMIT 20
        """
    ).fetchall()
    return [{"slug": (r["slug"] or "").strip(), "leadCount": int(r["lead_count"])} for r in rows]


def evaluate_health_rules(
    *,
    file_bytes: int,
    integrity_ok: Optional[bool],
    row_counts: dict[str, int],
) -> tuple[str, list[dict[str, str]]]:
    """Return (health_status, rules_triggered with hints)."""
    status = "ok"
    triggered: list[dict[str, str]] = []

    def add(rule_id: str, rule_status: str):
        nonlocal status
        if rule_status == "info":
            pass
        else:
            status = _max_status(status, rule_status)
        triggered.append(
            {"id": rule_id, "status": rule_status, "hint": RULE_HINTS.get(rule_id, "")}
        )

    if integrity_ok is False:
        add("integrity_fail", "critical")
    if file_bytes >= DB_SIZE_CRITICAL_BYTES:
        add("db_size_critical", "critical")
    elif file_bytes >= DB_SIZE_WARN_BYTES:
        add("db_size_warn", "warn")

    leads = row_counts.get("leads") or 0
    relay = row_counts.get("relay_ingested") or 0
    events = row_counts.get("events") or 0
    if leads > 0 and relay / leads > RELAY_BLOAT_RATIO:
        add("relay_bloat", "warn")

    pending = row_counts.get("cloud_pending") or 0
    if pending > PENDING_SYNC_LEADS:
        add("pending_sync", "warn")

    quarantine = row_counts.get("unmapped_campaign_queue") or 0
    if quarantine > QUARANTINE_BACKLOG:
        add("quarantine_backlog", "warn")

    if leads > LOW_ACTIVITY_LEADS and events / max(leads, 1) < LOW_ACTIVITY_RATIO:
        add("low_activity_bulk", "info")

    leads_with_email = row_counts.get("leads_with_email") or 0
    verification_rows = row_counts.get("lead_email_verification") or 0
    status_populated = row_counts.get("email_verification_status_populated") or 0
    if leads_with_email >= 10 and verification_rows == 0:
        add("verification_gap", "warn")
    elif leads_with_email >= 10 and verification_rows < leads_with_email * 0.3:
        add("verification_gap", "info")
    if verification_rows > 0 and status_populated < verification_rows * 0.5:
        add("verification_status_incomplete", "warn")

    return status, triggered


def collect_db_health(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    fast: bool = False,
    pipeline_version: str = "",
) -> dict[str, Any]:
    """Aggregate local DB health (read-only). fast=True skips expensive checks (sync path)."""
    db_path = get_db_path()
    file_bytes = db_path.stat().st_size if db_path.exists() else 0

    page_count = conn.execute("SELECT page_count FROM pragma_page_count()").fetchone()[0]
    page_size = conn.execute("SELECT page_size FROM pragma_page_size()").fetchone()[0]
    freelist = conn.execute("SELECT freelist_count FROM pragma_freelist_count()").fetchone()[0]
    freelist_bytes = int(freelist) * int(page_size)

    row_counts = {
        "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "relay_ingested": conn.execute("SELECT COUNT(*) FROM relay_ingested").fetchone()[0],
        "lead_identities": conn.execute("SELECT COUNT(*) FROM lead_identities").fetchone()[0],
        "workspace_lead_events": conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0],
        "unmapped_campaign_queue": conn.execute(
            "SELECT COUNT(*) FROM unmapped_campaign_queue"
        ).fetchone()[0],
        "cloud_pending": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE cloud_pending = 1"
        ).fetchone()[0],
        "leads_with_email": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND TRIM(email) != ''"
        ).fetchone()[0],
        "lead_email_verification": conn.execute(
            "SELECT COUNT(*) FROM lead_email_verification"
        ).fetchone()[0],
        "email_verification_status_populated": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE email_verification_status IS NOT NULL"
        ).fetchone()[0],
    }

    integrity_ok: Optional[bool] = None
    run_integrity = not fast or file_bytes <= INTEGRITY_SKIP_FAST_PATH_BYTES
    if run_integrity and file_bytes > 0:
        try:
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
            integrity_ok = str(ic).lower() == "ok"
        except sqlite3.Error:
            integrity_ok = False

    table_breakdown: list[dict[str, Any]] = []
    if file_bytes <= TABLE_BREAKDOWN_SKIP_BYTES or not fast:
        try:
            table_breakdown = _table_breakdown(conn, limit=5)
        except sqlite3.Error:
            table_breakdown = []

    workspace_breakdown = _workspace_breakdown(conn, org_id)

    health_status, rules_triggered = evaluate_health_rules(
        file_bytes=file_bytes,
        integrity_ok=integrity_ok,
        row_counts=row_counts,
    )

    return {
        "fileBytes": file_bytes,
        "pageCount": int(page_count),
        "pageSize": int(page_size),
        "freelistBytes": freelist_bytes,
        "integrityOk": integrity_ok,
        "healthStatus": health_status,
        "rulesTriggered": rules_triggered,
        "rowCounts": row_counts,
        "tableBreakdown": table_breakdown,
        "workspaceBreakdown": workspace_breakdown,
        "pipelineVersion": pipeline_version,
        "collectedAt": datetime.now(timezone.utc).isoformat(),
    }


def health_to_cloud_payload(health: dict[str, Any], client_id: str) -> dict[str, Any]:
    rules = health.get("rulesTriggered") or []
    return {
        "clientId": client_id,
        "fileBytes": health["fileBytes"],
        "healthStatus": health["healthStatus"],
        "rulesTriggered": [r["id"] if isinstance(r, dict) else r for r in rules],
        "rowCounts": health.get("rowCounts") or {},
        "tableBreakdown": health.get("tableBreakdown") or [],
        "workspaceBreakdown": health.get("workspaceBreakdown") or [],
        "pipelineVersion": health.get("pipelineVersion"),
        "freelistBytes": health.get("freelistBytes"),
    }


def should_report_health(
    health_status: str,
    load_config_fn: Callable[[], dict],
    *,
    force: bool = False,
) -> bool:
    if force:
        return True
    cfg = load_config_fn()
    last_at = cfg.get(CONFIG_LAST_HEALTH_REPORT_AT)
    last_status = (cfg.get(CONFIG_LAST_HEALTH_STATUS) or "ok").lower()
    if last_status != health_status.lower() and STATUS_RANK.get(health_status, 0) > STATUS_RANK.get(
        last_status, 0
    ):
        return True
    if not last_at:
        return True
    try:
        prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    elapsed = (datetime.now(timezone.utc) - prev).total_seconds()
    return elapsed >= HEALTH_REPORT_INTERVAL_SECONDS


def mark_health_reported(health_status: str, save_config_fn: Callable[[dict], None], load_config_fn: Callable[[], dict]):
    cfg = load_config_fn()
    cfg[CONFIG_LAST_HEALTH_REPORT_AT] = datetime.now(timezone.utc).isoformat()
    cfg[CONFIG_LAST_HEALTH_STATUS] = health_status
    save_config_fn(cfg)


def maybe_report_db_health_to_cloud(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    pipeline_version: str,
    get_agent_key_fn: Callable[[], Optional[str]],
    load_config_fn: Callable[[], dict],
    save_config_fn: Callable[[dict], None],
    get_client_id_fn: Callable[[], str],
    cloud_routing_enabled_fn: Callable,
    get_api_base_fn: Callable,
    push_db_health_fn: Callable,
    fast: bool = True,
    force: bool = False,
    skip: bool = False,
) -> dict[str, str]:
    """Non-blocking health report; returns status dict for sync JSON."""
    if skip:
        return {"db_health_reported": "skipped_flag"}
    tok = get_agent_key_fn()
    if not tok or not cloud_routing_enabled_fn(load_config_fn, tok):
        return {"db_health_reported": "skipped_no_key"}
    try:
        health = collect_db_health(
            conn, org_id=org_id, fast=fast, pipeline_version=pipeline_version
        )
        if not should_report_health(health["healthStatus"], load_config_fn, force=force):
            return {
                "db_health_reported": "skipped_throttled",
                "health_status": health["healthStatus"],
            }
        payload = health_to_cloud_payload(health, get_client_id_fn())
        api_base = get_api_base_fn(load_config_fn)
        push_db_health_fn(api_base, tok, payload)
        mark_health_reported(health["healthStatus"], save_config_fn, load_config_fn)
        return {
            "db_health_reported": "reported",
            "health_status": health["healthStatus"],
            "rules_triggered": [r["id"] for r in health.get("rulesTriggered") or []],
        }
    except Exception as exc:
        return {"db_health_reported": "error", "db_health_error": str(exc)[:200]}
