#!/usr/bin/env python3
"""
Outreach Magic — Agent-First Lead Database for Hermes

One SQLite file. No MongoDB. No BigQuery. Just your leads, visible.

Architecture:
  ~/.hermes/skills/outreachmagic/databases/outreachmagic.db  — Local SQLite database
  api.outreachmagic.io           — Cloudflare Worker relay (optional)
  pipeline.py                    — CLI: show, pull, connect, log-event...

Usage:
  pipeline.py init                          # Create database
  pipeline.py login                         # Connect via browser (device auth)
  pipeline.py pull                          # Pull events from relay
  pipeline.py show                          # Print pipeline table
  pipeline.py lead-table                    # Print canonical lead info table
  pipeline.py add-lead --name "Jane" ...    # Add a lead
  pipeline.py import-profiles --file leads.csv  # Bulk enrich from CSV/JSON
  pipeline.py apply-email-find-results --json '[...]' --workspace W  # Fast batch (known lead ids)
  pipeline.py log-event --lead-id 1 ...     # Log outreach event
  pipeline.py history --id 1                # Show lead's event timeline
  pipeline.py history --email j@acme.com    # Look up by email
  pipeline.py history --name "Jane"         # Look up by name (partial)
  pipeline.py stats                         # Quick stats
  pipeline.py campaigns                   # Counts by campaign name
  pipeline.py query engagement --workspace popcam --since 48h --json
  pipeline.py update                        # Install latest release (user-triggered)
  pipeline.py update --check                # Check for newer release without installing
"""

from __future__ import annotations

import sys as _sys

_MIN_PYTHON = (3, 10)
if _sys.version_info < _MIN_PYTHON:
    _ver = ".".join(str(v) for v in _sys.version_info[:2])
    _need = ".".join(str(v) for v in _MIN_PYTHON)
    print(
        f"Outreach Magic requires Python {_need}+ (found {_ver}).\n"
        f"Install a newer Python from https://www.python.org/downloads/ or via "
        f"your package manager (brew install python).",
        file=_sys.stderr,
    )
    _sys.exit(1)
del _sys, _MIN_PYTHON

import ast
import sqlite3
import json
import os
import sys
import csv
import argparse
import hashlib
import concurrent.futures
import re
import shutil
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import subprocess

from relay_extractors import (
    build_display_name,
    extract_bounce_fields,
    extract_relay_fields,
    extract_relay_identity,
    name_from_email,
)
from workspace_routing import (
    CampaignContext,
    CampaignRoutingCache,
    DEFAULT_ORG_ID,
    OrgRoutingConfig,
    VALID_WORKSPACE_ROUTING_MODES,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    append_workspace_event,
    assign_campaign_map,
    build_import_identities,
    collect_identities_from_event,
    pick_external_id_from_raw,
    ensure_default_org_workspace,
    ensure_organization,
    extract_campaign_context,
    find_lead_by_identity,
    find_match_method_for_lead,
    format_no_campaign_event_message,
    format_unmapped_campaign_message,
    lead_entity_key,
    match_confidence_for_type,
    MULTI_WORKSPACE_HOLD_MESSAGE,
    get_org_routing_config,
    import_extra_from_entity_key,
    lead_external_id_value,
    parse_entity_key,
    quarantine_event,
    resolve_workspace,
    resolve_workspace_for_ingest,
    resolve_workspace_identity,
    upsert_all_identities,
    upsert_identity_alias,
    enqueue_identity_conflict_merge,
    normalize_linkedin,
    parse_linkedin_value,
    linkedin_url_field_conflict,
    linkedin_url_is_hash,
    should_replace_linkedin_url,
    upsert_linkedin_status,
    upsert_workspace_lead,
)

import routing_cloud
import agent_secrets_cloud
import connections_cloud
import db_health
import pipeline_dedup
import pipeline_lead_review
import review_cloud
import quarantine_resolutions as qres
import workspace_archive
import query_cli
from import_formats import (
    build_import_quality_warnings,
    preprocess_import_rows,
)
from data_freshness import (
    attach_freshness,
    freshness_from_last_pull,
    is_pull_fresh_enough,
    parse_duration,
    print_freshness_stderr,
)
from read_queries import LATEST_STATUS_CTE
from schema import SCHEMA_SQL

import bounces
from bounces import (
    backfill_bounce_events_from_events,
    bounce_stats,
    build_bounce_event_metadata,
    extract_bounce_payload as _extract_bounce_payload,
    is_bounce_event_type,
    list_bounce_events,
    normalize_bounce_event_type,
    record_bounce_event as _record_bounce_event,
    record_platform_bounce as _record_platform_bounce,
    verify_email,
    verify_email_batch,
    verify_pending,
    verify_status,
    leads_needing_verification,
)
from constants import (
    ATTRIBUTE_INSIGHT_FIELDS,
    BILLING_UPGRADE_URL,
    MAX_EVENT_BODY_STORAGE_CHARS,
    PIPELINE_STAGES,
    RELAY_BULK_THRESHOLD,
    RELAY_PULL_EVENT_MAX,
    RELAY_PULL_MAX,
    RELAY_PULL_PAGE_SIZE,
    RELAY_PULL_HARD_TIMEOUT_BUFFER,
    RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT,
    RELAY_PULL_SNAPSHOT_MAX,
    RELAY_PUSH_BATCH_SIZE,
    RELAY_PUSH_EVENTS_BULK,
    RELAY_PUSH_SNAPSHOT_BULK,
    RELAY_PUSH_MAX_ATTEMPTS,
    RELAY_PUSH_MAX_BULK,
    RELAY_PUSH_RETRY_BASE_SECONDS,
    RELAY_PUSH_ROUTINE_MAX,
    RELAY_PUSH_TIMEOUT_SECONDS,
    COMPANY_DOMAIN_SQL,
    SHARED_EMAIL_DOMAINS,
    STAGE_EMOJI,
    require_professional_domain_clause,
    USAGE_WARNING_PERCENT,
    USAGE_CRITICAL_PERCENT,
)
from db_conn import (
    apply_bulk_pull_pragmas,
    database_has_schema,
    end_bulk_pull_session,
    format_database_recovery_message,
    get_conn,
)
from formatters import (
    format_campaign_stats,
    format_copy_insights,
    format_event_timeline,
    format_lead_table,
    format_pipeline_table,
    format_segment_insights,
    format_stats,
)
from event_classification import normalize_campaign_event_type
from activity_sync import (
    ActivitySummary,
    compute_lead_activity_from_events,
    merge_activity_summary,
    refresh_lead_activity_for_lead,
    refresh_lead_activity_from_events,
    set_lead_activity_summary,
)
from lead_sync import (
    apply_agent_lead_core_payload,
    apply_agent_lead_workspace_payload,
    build_lead_core_sync_payload,
    build_lead_workspace_sync_payload,
    build_lead_sync_payload,
    entity_key_from_prefetch,
    inspect_sync_lead,
    resolve_lead_from_agent_sync,
    _load_lead_sync_prefetch,
)
from platform_registry import (
    LINKEDIN_PLATFORMS,
    PLATFORM_LABELS,
    PLATFORM_SETUP_HINTS,
    looks_like_html,
    normalize_event_body_for_storage,
    platform_map_json,
    reply_event_sql_condition,
    strip_html_reply,
)
from relay_ingest import (
    ingest_relay_event,
    mark_relay_ingested,
    mark_relay_ingested_many,
    prefetch_relay_ingested,
    prefetch_ws_idempotency_keys,
    normalize_lead_status_display,
    relay_already_ingested,
    relay_dedupe_key,
)

from pipeline_utils import (
    _dedupe_tags,
    _parse_tags,
    email_domain,
    furthest_stage,
    normalize_company_domain,
    normalize_email,
    normalize_event_sender,
    normalize_tag,
    parse_headcount_numeric,
    parse_tags_value,
)

from pipeline_migration import (
    backfill_workspace_routing,
    init_db,
    mark_all_lead_snapshots_pending,
    migrate_db,
    repair_malformed_tags,
)

from pipeline_update import (
    _chmod_best_effort,
    _cloud_snapshot_pending_count,
    _fetch_url,
    _load_json_dict,
    _sha256_hex,
    _skill_scripts_in_git_checkout,
    _sync_events_only,
    _use_bulk_transport,
    _warn_duplicate_installs,
    backup_scripts_for_rollback,
    check_skill_update,
    clear_snapshot_cursors,
    effective_update_target,
    fetch_latest_release,
    fetch_remote_version,
    fetch_update_manifest,
    get_agent_key,
    get_last_max_id,
    get_last_pull,
    get_last_sync,
    get_or_create_client_id,
    get_relay_push_settings,
    get_snapshot_cursor,
    get_workspace_routing_mode_from_config,
    load_config,
    normalize_relay_timestamp,
    notify_update_available,
    pull_if_stale_skip_result,
    record_install_source,
    resolve_update_source,
    rollback_skill,
    save_config,
    scripts_rollback_dir,
    set_last_max_id,
    set_last_pull,
    set_last_sync,
    set_snapshot_cursor,
    skill_md_url_for_repo,
    skill_scripts_dir,
    sync_skill_md_version,
    sync_workspace_routing_mode_from_config,
    update_download_names,
    update_skill,
)


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

from om_paths import (
    get_agent_secrets_path,
    get_config_path,
    get_data_root,
    get_db_path,
    get_export_dir,
    get_input_dir,
    get_install_dir,
    get_skill_home,
    get_working_root,
    check_duplicate_installs,
    hermes_profile_copy_warning,
    resolve_project_path,
    find_sheets_export_record,
    save_sheets_export_record,
    set_data_root_override,
    set_db_path_override,
    working_paths_payload,
)

SKILL_NAME = "outreachmagic"

# Agent directory lookup — user chooses one explicitly during setup
AGENT_DIR_MAP = {
    "cursor": "~/.cursor",
    "agents": "~/.agents",
    "claude": "~/.claude",
    "hermes": "~/.hermes",
}
AGENT_DIR_NAMES = list(AGENT_DIR_MAP.keys())
RELAY_URL = "https://api.outreachmagic.io"

SKILL_SCRIPTS_DIR = f"skills/{SKILL_NAME}/scripts"
# Every scripts/*.py — auto-discovered so new modules are not skipped by update.
UPDATE_SCRIPT_FILES = tuple(
    sorted(p.name for p in Path(__file__).resolve().parent.glob("*.py"))
)
UPDATE_MANIFEST_FILES = (*UPDATE_SCRIPT_FILES, "VERSION")
# Files at skill root (not in scripts/), handled separately by update_skill().
ROOT_SKILL_FILES = frozenset({"README.md", "install.sh", "SECURITY.md"})
# Unified public release repo (skills/outreachmagic layout).
SKILL_REPO_PATH = "skills/outreachmagic"
GITHUB_REPO = "outreachmagic/outreachmagic"


def _read_version_file(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    return "0.0.0"


__version__ = _read_version_file(Path(__file__).resolve().parent / "VERSION")




def ensure_company(
    conn: sqlite3.Connection,
    name: Optional[str] = None,
    domain: Optional[str] = None,
    industry: Optional[str] = None,
    headcount: Optional[str] = None,
    hq_city: Optional[str] = None,
    hq_state: Optional[str] = None,
    hq_country: Optional[str] = None,
    *,
    authoritative: bool = False,
) -> Optional[int]:
    """Find or create company row; match business domain first, then exact name."""
    domain = (domain or "").strip().lower() or None
    if domain and domain in SHARED_EMAIL_DOMAINS:
        domain = None
    name = (name or "").strip() or None
    if not name and not domain:
        return None
    if domain:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
        if row:
            cid = row["id"]
            _update_company_fields(conn, cid, name, industry, headcount,
                                   hq_city=hq_city, hq_state=hq_state, hq_country=hq_country,
                                   authoritative=authoritative)
            return cid
    if name:
        row = conn.execute(
            "SELECT id FROM companies WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            cid = row["id"]
            if domain:
                conn.execute(
                    """UPDATE companies SET domain = COALESCE(domain, ?),
                       updated_at = datetime('now') WHERE id = ?""",
                    (domain, cid),
                )
            _update_company_fields(conn, cid, None, industry, headcount,
                                   hq_city=hq_city, hq_state=hq_state, hq_country=hq_country,
                                   authoritative=authoritative)
            return cid
    display_name = name or (domain or "Unknown")
    cid = conn.execute(
        """INSERT INTO companies (name, domain, industry, headcount, headcount_numeric,
                                  hq_city, hq_state, hq_country)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (display_name, domain, industry, headcount, parse_headcount_numeric(headcount),
         hq_city, hq_state, hq_country),
    ).lastrowid
    return cid


def _update_company_fields(
    conn: sqlite3.Connection,
    company_id: int,
    name: Optional[str],
    industry: Optional[str],
    headcount: Optional[str],
    hq_city: Optional[str] = None,
    hq_state: Optional[str] = None,
    hq_country: Optional[str] = None,
    *,
    authoritative: bool = False,
):
    sets, params = [], []
    if name:
        sets.append("name = CASE WHEN trim(name) = '' THEN ? ELSE name END")
        params.append(name)
    if industry:
        if authoritative:
            sets.append("industry = ?")
        else:
            sets.append("industry = COALESCE(industry, ?)")
        params.append(industry)
    if headcount:
        if authoritative:
            sets.append("headcount = ?")
        else:
            sets.append("headcount = COALESCE(headcount, ?)")
        params.append(headcount)
        hc_num = parse_headcount_numeric(headcount)
        if hc_num is not None:
            if authoritative:
                sets.append("headcount_numeric = ?")
            else:
                sets.append("headcount_numeric = COALESCE(headcount_numeric, ?)")
            params.append(hc_num)
    if hq_city:
        sets.append(f"hq_city = {'?' if authoritative else 'COALESCE(hq_city, ?)'}")
        params.append(hq_city)
    if hq_state:
        sets.append(f"hq_state = {'?' if authoritative else 'COALESCE(hq_state, ?)'}")
        params.append(hq_state)
    if hq_country:
        sets.append(f"hq_country = {'?' if authoritative else 'COALESCE(hq_country, ?)'}")
        params.append(hq_country)
    if sets:
        sets.append("updated_at = datetime('now')")
        params.append(company_id)
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = ?", params)


def backfill_companies_from_leads(conn: sqlite3.Connection):
    """Create companies rows from existing lead company/domain data."""
    rows = conn.execute(
        """SELECT DISTINCT company, email_domain, industry, headcount FROM leads
           WHERE (company IS NOT NULL AND trim(company) != '')
              OR (email_domain IS NOT NULL AND email_domain != '')"""
    ).fetchall()
    for row in rows:
        domain = row["email_domain"]
        if domain and domain in SHARED_EMAIL_DOMAINS:
            domain = None
        name = (row["company"] or "").strip() or None
        if not name and not domain:
            continue
        cid = ensure_company(
            conn, name=name, domain=domain,
            industry=row["industry"], headcount=row["headcount"],
        )
        if not cid:
            continue
        if domain and domain not in SHARED_EMAIL_DOMAINS:
            conn.execute(
                """UPDATE leads SET company_id = ?
                   WHERE email_domain = ? AND (company_id IS NULL)""",
                (cid, domain),
            )
        if name:
            conn.execute(
                """UPDATE leads SET company_id = ?
                   WHERE lower(company) = lower(?) AND (company_id IS NULL)""",
                (cid, name),
            )


def link_lead_company(
    conn: sqlite3.Connection,
    lead_id: int,
    company: Optional[str] = None,
    email: Optional[str] = None,
    industry: Optional[str] = None,
    headcount: Optional[str] = None,
) -> Optional[int]:
    if email:
        domain = email_domain(email)
    else:
        row = conn.execute(
            "SELECT email_domain FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        domain = (row["email_domain"] or "").strip().lower() or None if row else None
    cid = ensure_company(conn, name=company, domain=domain, industry=industry, headcount=headcount)
    if cid:
        conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead_id))
    if company:
        conn.execute(
            """UPDATE leads SET company = CASE WHEN company IS NULL OR trim(company) = ''
               THEN ? ELSE company END WHERE id = ?""",
            (company, lead_id),
        )
    return cid


def ensure_lead_domain(
    lead_id: int,
    email: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
    *,
    commit: bool = True,
):
    domain = email_domain(email)
    if not domain:
        return
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.execute(
        "UPDATE leads SET email_domain = ? WHERE id = ? AND (email_domain IS NULL OR email_domain = '')",
        (domain, lead_id),
    )
    if commit and own_conn:
        conn.commit()
    if own_conn and conn is not None:
        conn.close()


def find_lead_by_email(conn: sqlite3.Connection, email: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM leads WHERE email = ?", (email,)).fetchone()
    return row["id"] if row else None


def find_lead_by_linkedin(conn: sqlite3.Connection, linkedin_norm: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM leads WHERE linkedin_url = ?", (linkedin_norm,)
    ).fetchone()
    return row["id"] if row else None


def find_lead(
    *,
    lead_id: Optional[int] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    name: Optional[str] = None,
    workspace: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    row = None
    workspace_row = resolve_workspace_identity(conn, workspace)
    if workspace and not workspace_row:
        if own_conn:
            conn.close()
        raise ValueError(f"workspace not found: {workspace}")
    workspace_join = ""
    workspace_params: list = []
    if workspace_row:
        workspace_join = (
            " INNER JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?"
        )
        workspace_params.append(workspace_row["id"])
    if lead_id:
        params = [*workspace_params, lead_id]
        row = conn.execute(
            f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
               FROM leads l
               LEFT JOIN companies c ON l.company_id = c.id
               {workspace_join}
               WHERE l.id = ?""",
            tuple(params),
        ).fetchone()
    elif email:
        em = normalize_email(email)
        if em:
            params = [*workspace_params, em]
            row = conn.execute(
                f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
                   FROM leads l
                   LEFT JOIN companies c ON l.company_id = c.id
                   {workspace_join}
                   WHERE l.email = ?""",
                tuple(params),
            ).fetchone()
            if not row:
                # Try finding by identity
                found_id = find_lead_by_identity(conn, DEFAULT_ORG_ID, "email", em)
                if found_id:
                    params = [*workspace_params, found_id]
                    row = conn.execute(
                        f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
                           FROM leads l
                           LEFT JOIN companies c ON l.company_id = c.id
                           {workspace_join}
                           WHERE l.id = ?""",
                        tuple(params),
                    ).fetchone()

    elif linkedin:
        norm = normalize_linkedin(linkedin)
        if norm:
            params = [*workspace_params, norm]
            row = conn.execute(
                f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
                   FROM leads l LEFT JOIN companies c ON l.company_id = c.id
                   {workspace_join}
                   WHERE l.linkedin_url = ?""",
                tuple(params),
            ).fetchone()
            if not row:
                # Try finding by identity (public url or member/salesnav)
                li_parsed = parse_linkedin_value(linkedin)
                for itype, val in li_parsed:
                    found_id = find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val)
                    if found_id:
                        params = [*workspace_params, found_id]
                        row = conn.execute(
                            f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
                               FROM leads l
                               LEFT JOIN companies c ON l.company_id = c.id
                               {workspace_join}
                               WHERE l.id = ?""",
                            tuple(params),
                        ).fetchone()
                        if row:
                            break
    elif name:
        params = [*workspace_params, f"%{name}%"]
        row = conn.execute(
            f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
               FROM leads l LEFT JOIN companies c ON l.company_id = c.id
               {workspace_join}
               WHERE l.name LIKE ? LIMIT 1""",
            tuple(params),
        ).fetchone()
    if own_conn:
        conn.close()
    return dict(row) if row else None


def batch_lead_lookup(
    items: list[dict],
    *,
    workspace: Optional[str] = None,
) -> dict:
    """Lookup many leads in one DB connection (outreachmagic email-finding / dedup)."""
    conn = get_conn()
    try:
        ws_row = resolve_workspace_identity(conn, workspace)
        if workspace and not ws_row:
            raise ValueError(f"workspace not found: {workspace}")
        ws_id = ws_row["id"] if ws_row else None
        results: list[dict] = []
        lead_ids: list[int] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"index": i, "status": "error", "error": "invalid item"})
                continue
            idx = item.get("index", i)
            lead = None
            try:
                if item.get("lead_id"):
                    lead = find_lead(
                        lead_id=int(item["lead_id"]),
                        workspace=workspace,
                        conn=conn,
                    )
                elif item.get("linkedin"):
                    lead = find_lead(
                        linkedin=str(item["linkedin"]),
                        workspace=workspace,
                        conn=conn,
                    )
                elif item.get("email"):
                    lead = find_lead(
                        email=str(item["email"]),
                        workspace=workspace,
                        conn=conn,
                    )
                elif item.get("name"):
                    lead = find_lead(
                        name=str(item["name"]),
                        workspace=workspace,
                        conn=conn,
                    )
            except ValueError as exc:
                results.append({"index": idx, "status": "error", "error": str(exc)})
                continue
            entry: dict = {"index": idx, "status": "not_found"}
            if lead:
                lid = int(lead["id"])
                lead_ids.append(lid)
                domain_row = conn.execute(
                    """SELECT c.domain AS company_domain
                       FROM leads l
                       LEFT JOIN companies c ON l.company_id = c.id
                       WHERE l.id = ?""",
                    (lid,),
                ).fetchone()
                company_domain = ""
                if domain_row and domain_row["company_domain"]:
                    company_domain = str(domain_row["company_domain"]).strip().lower().lstrip("@")
                if not company_domain:
                    company_domain = (lead.get("email_domain") or "").strip().lower().lstrip("@") or None
                entry = {
                    "index": idx,
                    "status": "found",
                    "lead_id": lid,
                    "email": (lead.get("email") or "").strip() or None,
                    "name": lead.get("name"),
                    "company": lead.get("company_display") or lead.get("company"),
                    "company_domain": company_domain,
                    "linkedin_url": lead.get("linkedin_url"),
                }
            results.append(entry)

        tags_by_lead: dict[int, list[str]] = {}
        if ws_id and lead_ids:
            placeholders = ",".join("?" * len(lead_ids))
            tag_rows = conn.execute(
                f"""SELECT lead_id, tag FROM workspace_lead_tags
                    WHERE workspace_id = ? AND lead_id IN ({placeholders})
                    ORDER BY created_at""",
                (ws_id, *lead_ids),
            ).fetchall()
            for tr in tag_rows:
                tags_by_lead.setdefault(int(tr["lead_id"]), []).append(str(tr["tag"]))
        for entry in results:
            lid = entry.get("lead_id")
            if lid:
                entry["tags"] = tags_by_lead.get(int(lid), [])
        return {
            "status": "ok",
            "workspace": workspace,
            "count": len(results),
            "results": results,
        }
    finally:
        conn.close()


def _pick_merge_keep_id(conn: sqlite3.Connection, id_a: int, id_b: int) -> tuple[int, int]:
    counts = conn.execute(
        """SELECT lead_id, COUNT(*) AS n FROM events
           WHERE lead_id IN (?, ?) GROUP BY lead_id""",
        (id_a, id_b),
    ).fetchall()
    by_id = {r["lead_id"]: r["n"] for r in counts}
    na, nb = by_id.get(id_a, 0), by_id.get(id_b, 0)
    if na > nb:
        return id_a, id_b
    if nb > na:
        return id_b, id_a
    ca = conn.execute("SELECT created_at FROM leads WHERE id = ?", (id_a,)).fetchone()
    cb = conn.execute("SELECT created_at FROM leads WHERE id = ?", (id_b,)).fetchone()
    if ca and cb and str(ca["created_at"]) <= str(cb["created_at"]):
        return id_a, id_b
    return id_b, id_a


def merge_leads(
    keep_id: int,
    merge_id: int,
    reason: str = "manual",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Combine two lead rows; merge_id is deleted after moving children."""
    if keep_id == merge_id:
        return {"status": "noop", "keep_id": keep_id}
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
        conn.execute("BEGIN")
    try:
        keep = conn.execute("SELECT * FROM leads WHERE id = ?", (keep_id,)).fetchone()
        other = conn.execute("SELECT * FROM leads WHERE id = ?", (merge_id,)).fetchone()
        if not keep or not other:
            if own_conn:
                conn.execute("ROLLBACK")
            return {"status": "error", "error": "lead not found"}

        events_moved = conn.execute(
            "SELECT COUNT(*) FROM events WHERE lead_id = ?", (merge_id,)
        ).fetchone()[0]
        conn.execute("UPDATE events SET lead_id = ? WHERE lead_id = ?", (keep_id, merge_id))

        for row in conn.execute(
            "SELECT campaign_id FROM campaign_leads WHERE lead_id = ?", (merge_id,)
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
                (row["campaign_id"], keep_id),
            )
        conn.execute("DELETE FROM campaign_leads WHERE lead_id = ?", (merge_id,))
        conn.execute(
            "UPDATE relay_ingested SET lead_id = ? WHERE lead_id = ?", (keep_id, merge_id)
        )
        conn.execute(
            "UPDATE lead_identities SET lead_id = ? WHERE lead_id = ?", (keep_id, merge_id)
        )
        for tbl in ("workspace_leads", "workspace_lead_events"):
            if tbl == "workspace_leads":
                for row in conn.execute(
                    "SELECT id, workspace_id FROM workspace_leads WHERE lead_id = ?", (merge_id,)
                ).fetchall():
                    existing = conn.execute(
                        "SELECT id FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
                        (row["workspace_id"], keep_id),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE workspace_lead_events SET workspace_lead_id = ? WHERE workspace_lead_id = ?",
                            (existing["id"], row["id"]),
                        )
                        conn.execute("DELETE FROM workspace_leads WHERE id = ?", (row["id"],))
                    else:
                        conn.execute(
                            """UPDATE workspace_leads SET lead_id = ?, updated_at = datetime('now')
                               WHERE id = ?""",
                            (keep_id, row["id"]),
                        )
            else:
                conn.execute(
                    f"UPDATE {tbl} SET lead_id = ? WHERE lead_id = ?", (keep_id, merge_id)
                )

        email = keep["email"] or other["email"]
        li_merged = (
            normalize_linkedin(keep["linkedin_url"])
            or normalize_linkedin(other["linkedin_url"])
            or keep["linkedin_url"]
            or other["linkedin_url"]
        )
        domain = email_domain(email)
        new_stage = furthest_stage(keep["stage"] or "prospecting", other["stage"] or "prospecting")
        company = (keep["company"] or "") or (other["company"] or "") or None
        title = (keep["title"] or "") or (other["title"] or "") or None
        industry = (keep["industry"] or "") or (other["industry"] or "") or None
        headcount = (keep["headcount"] or "") or (other["headcount"] or "") or None
        company_id = keep["company_id"] or other["company_id"]
        merge_entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, merge_id)
        conn.execute(
            """INSERT INTO lead_merges (keep_id, merge_id, reason, merge_entity_key, relay_delete_pushed)
               VALUES (?, ?, ?, ?, 0)""",
            (keep_id, merge_id, reason, merge_entity_key or None),
        )
        conn.execute("DELETE FROM leads WHERE id = ?", (merge_id,))

        if not company_id:
            company_id = link_lead_company(
                conn, keep_id, company=company, email=email,
                industry=industry, headcount=headcount,
            )

        conn.execute(
            """UPDATE leads SET
               email = COALESCE(email, ?),
               email_domain = COALESCE(email_domain, ?),
               linkedin_url = COALESCE(linkedin_url, ?),
               company_id = COALESCE(company_id, ?),
               company = COALESCE(NULLIF(trim(company), ''), ?),
               title = COALESCE(NULLIF(trim(title), ''), ?),
               industry = COALESCE(NULLIF(trim(industry), ''), ?),
               headcount = COALESCE(NULLIF(trim(headcount), ''), ?),
               stage = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (
                email, domain, li_merged, company_id,
                company, title, industry, headcount, new_stage, keep_id,
            ),
        )
        if own_conn:
            conn.execute("COMMIT")
    except Exception:
        if own_conn:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        if own_conn and conn is not None:
            conn.close()
    return {
        "status": "merged",
        "keep_id": keep_id,
        "merge_id": merge_id,
        "events_moved": events_moved,
        "reason": reason,
    }


def resolve_lead(
    *,
    email: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    name: str = "Unknown",
    company: Optional[str] = None,
    title: Optional[str] = None,
    industry: Optional[str] = None,
    headcount: Optional[str] = None,
    channel: str = "email",
    stage: str = "prospecting",
    notes: Optional[str] = None,
    enrich_name: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    auto_merge: bool = True,
    company_domain: Optional[str] = None,
    source: Optional[str] = None,
    source_detail: Optional[str] = None,
    source_platform: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    location_city: Optional[str] = None,
    location_state: Optional[str] = None,
    location_country: Optional[str] = None,
    hq_city: Optional[str] = None,
    hq_state: Optional[str] = None,
    hq_country: Optional[str] = None,
    identities: Optional[list[tuple[str, str]]] = None,
    import_batch: Optional[str] = None,
    import_extra: Optional[dict[str, str]] = None,
    force_lead_id: Optional[int] = None,
) -> dict:
    """Match or create lead by tiered identities (email, external_id, name+company, etc.)."""
    email_norm = normalize_email(email)
    li_parsed = parse_linkedin_value(linkedin_url) if linkedin_url else []
    li_public = next((v for t, v in li_parsed if t == "linkedin_url"), None)

    if identities is None:
        profile = {
            k: v for k, v in {
                "email": email, "linkedin": linkedin_url, "name": name,
                "company": company, "title": title,
            }.items() if v
        }
        extra = dict(import_extra or {})
        identities = build_import_identities(
            profile, extra,
            import_batch=import_batch,
            company_domain=company_domain,
        )
        if not identities and (email_norm or li_parsed):
            identities = []
            if email_norm:
                identities.append(("email", email_norm))
            identities.extend(li_parsed)

    if not identities and force_lead_id is None:
        return {"status": "error", "error": "no identity: need email, linkedin, external_id, or name+company"}

    own_conn = conn is None
    if own_conn:
        conn = get_conn()

    by_email = find_lead_by_email(conn, email_norm) if email_norm else None
    by_li = find_lead_by_linkedin(conn, li_public) if li_public else None

    lead_id: Optional[int] = None
    created = True
    match_method: Optional[str] = None

    if force_lead_id is not None:
        row = conn.execute("SELECT id FROM leads WHERE id = ?", (int(force_lead_id),)).fetchone()
        if not row:
            if own_conn:
                conn.close()
            return {"status": "error", "error": f"lead not found: {force_lead_id}"}
        lead_id = int(force_lead_id)
        created = False
        match_method = "lead_id"
        if (
            email_norm
            and by_email
            and int(by_email) != lead_id
            and auto_merge
            and not dry_run
        ):
            keep_id, merge_id = lead_id, int(by_email)
            if own_conn:
                conn.close()
            merge_leads(
                keep_id, merge_id, reason="force_lead_id_email_conflict",
                conn=None if own_conn else conn,
            )
            if own_conn:
                conn = get_conn()
        elif email_norm and by_email and int(by_email) != lead_id and dry_run:
            if own_conn:
                conn.close()
            return {
                "status": "error",
                "error": f"email already on lead {by_email}, conflicts with lead_id {lead_id}",
                "dry_run": True,
            }
    elif by_email and by_li and by_email != by_li and auto_merge and not dry_run:
        keep_id, merge_id = _pick_merge_keep_id(conn, by_email, by_li)
        if own_conn:
            conn.close()
        merge_leads(
            keep_id, merge_id, reason="auto_dual_identifier",
            conn=None if own_conn else conn,
        )
        if own_conn:
            conn = get_conn()
        lead_id = keep_id
        created = False

    # Only use confirmed identity types for lead matching — fuzzy composites
    # (name_company_domain, name_company, import_key, etc.) create false positives
    # when used as persistent aliases, especially for webhook event ingest.
    STRONG_IDENTITY_TYPES = frozenset({
        "email", "linkedin_url", "linkedin_sales_nav_id",
        "linkedin_member_id", "external_id",
    })
    if force_lead_id is None:
        for itype, val in identities:
            if itype not in STRONG_IDENTITY_TYPES:
                continue
            found = find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val)
            if found:
                if lead_id is None:
                    lead_id = found
                    match_method = itype
                    created = False
                elif lead_id != found and itype in STRONG_IDENTITY_TYPES:
                    pass
                elif lead_id != found:
                    break

    if lead_id is None:
        created = True
    elif match_method is None:
        match_method = find_match_method_for_lead(conn, DEFAULT_ORG_ID, lead_id, identities)

    if dry_run:
        if own_conn:
            conn.close()
        conf = match_confidence_for_type(match_method or identities[0][0])
        base = {
            "email": email_norm, "linkedin": li_public, "dry_run": True,
            "match_method": match_method or (identities[0][0] if identities else None),
            "match_confidence": conf if not created else None,
        }
        if created:
            return {"status": "created", "id": None, **base}
        return {"status": "matched", "id": lead_id, **base}

    domain_explicit = normalize_company_domain(company_domain)
    domain_from_email = email_domain(email_norm)
    effective_domain = domain_explicit or domain_from_email
    now_ts = datetime.now(timezone.utc).isoformat()
    linkedin_url_conflicts: list[dict] = []
    insert_li_public = li_public
    if li_public and created:
        conflict = linkedin_url_field_conflict(conn, 0, li_public)
        if conflict:
            linkedin_url_conflicts.append(conflict)
            insert_li_public = None

    if created:
        company_id = ensure_company(
            conn, name=company, domain=effective_domain, industry=industry, headcount=headcount,
            hq_city=hq_city, hq_state=hq_state, hq_country=hq_country,
        )
        cur = conn.execute(
            """INSERT INTO leads (name, company_id, company, title, industry, headcount, headcount_numeric,
               email, email_domain, linkedin_url,
               location_city, location_state, location_country,
               channel, stage, notes,
               original_source, original_source_detail, original_source_platform, original_source_at,
               latest_source, latest_source_detail, latest_source_platform, latest_source_at)
               VALUES (?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                name, company_id, company, title, industry, headcount, parse_headcount_numeric(headcount),
                email_norm, domain_from_email, insert_li_public,
                location_city, location_state, location_country,
                channel, stage, notes,
                source, source_detail, source_platform, now_ts,
                source, source_detail, source_platform, now_ts,
            ),
        )
        lead_id = int(cur.lastrowid)
        match_method = match_method or identities[0][0]
        if own_conn:
            conn.commit()
    else:
        sets, params = [], []
        if email_norm:
            sets.extend(["email = COALESCE(email, ?)", "email_domain = COALESCE(email_domain, ?)"])
            params.extend([email_norm, domain_from_email])
        if li_public:
            cur_li = conn.execute(
                "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
            ).fetchone()
            current_li = cur_li["linkedin_url"] if cur_li else None
            conflict = linkedin_url_field_conflict(conn, lead_id, li_public)
            if conflict:
                linkedin_url_conflicts.append(conflict)
            elif overwrite or should_replace_linkedin_url(current_li, li_public):
                sets.append("linkedin_url = ?")
                params.append(li_public)
            else:
                sets.append("linkedin_url = COALESCE(linkedin_url, ?)")
                params.append(li_public)
        if source:
            sets.extend([
                "latest_source = ?",
                "latest_source_detail = ?",
                "latest_source_platform = ?",
                "latest_source_at = ?",
                "original_source = COALESCE(original_source, ?)",
                "original_source_detail = COALESCE(original_source_detail, ?)",
                "original_source_platform = COALESCE(original_source_platform, ?)",
                "original_source_at = COALESCE(original_source_at, ?)",
            ])
            params.extend([
                source, source_detail, source_platform, now_ts,
                source, source_detail, source_platform, now_ts,
            ])
        if notes is not None:
            # Persist import notes when provided.
            # - overwrite=False: only fill if notes is currently empty
            # - overwrite=True: replace notes
            if overwrite:
                sets.append("notes = ?")
                params.append(notes)
            else:
                sets.append("notes = CASE WHEN notes IS NULL OR notes = '' THEN ? ELSE notes END")
                params.append(notes)
        sets.append("updated_at = datetime('now')")
        params.append(lead_id)
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", params)
        if own_conn:
            conn.commit()

    id_conflicts, promote_conflicts = upsert_all_identities(
        conn, DEFAULT_ORG_ID, int(lead_id), identities, source=source_platform,
    )
    linkedin_url_conflicts.extend(promote_conflicts)

    name_for_enrich = enrich_name if enrich_name is not None else name
    filled = enrich_lead(
        lead_id, name=name_for_enrich, title=title, industry=industry,
        company=company, headcount=headcount, overwrite=overwrite,
        conn=conn,
    )
    if email_norm:
        ensure_lead_domain(lead_id, email_norm, conn=conn, commit=False)
    link_lead_company(conn, lead_id, company=company, email=email_norm,
                      industry=industry, headcount=headcount)
    if domain_explicit:
        ensure_company(conn, name=company, domain=domain_explicit,
                       industry=industry, headcount=headcount,
                       hq_city=hq_city, hq_state=hq_state, hq_country=hq_country)
    if own_conn:
        conn.commit()
        conn.close()

    method = match_method or identities[0][0]
    return {
        "status": "created" if created else "matched",
        "id": lead_id,
        "email": email_norm,
        "linkedin": li_public,
        "filled": filled,
        "match_method": method,
        "match_confidence": match_confidence_for_type(method),
        "identity_conflicts": id_conflicts,
        "linkedin_url_conflicts": linkedin_url_conflicts,
    }


def db_exists():
    return get_db_path().exists()

def add_lead(name, company=None, title=None, industry=None, headcount=None,
             email=None, linkedin_url=None,
             channel="email", stage="prospecting", notes=None):
    matched_lead_id = None
    if not email and not linkedin_url and name and company:
        conn = get_conn()
        row = conn.execute(
            """
            SELECT id
            FROM leads
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
              AND LOWER(TRIM(company)) = LOWER(TRIM(?))
            ORDER BY id
            LIMIT 1
            """,
            (name, company),
        ).fetchone()
        conn.close()
        if row:
            matched_lead_id = row["id"]

    if matched_lead_id is not None:
        enrich_lead(
            matched_lead_id,
            name=name,
            title=title,
            industry=industry,
            company=company,
            headcount=headcount,
            overwrite=False,
        )
        conn = get_conn()
        if notes is not None:
            conn.execute(
                """
                UPDATE leads
                SET notes = CASE WHEN notes IS NULL OR notes = '' THEN ? ELSE notes END,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (notes, matched_lead_id),
            )
            conn.commit()
        row = conn.execute(
            "SELECT email, linkedin_url FROM leads WHERE id = ?",
            (matched_lead_id,),
        ).fetchone()
        conn.close()
        return {
            "status": "exists",
            "id": matched_lead_id,
            "name": name,
            "email": row["email"] if row else None,
            "linkedin": row["linkedin_url"] if row else None,
        }

    result = resolve_lead(
        email=email,
        linkedin_url=linkedin_url,
        name=name,
        company=company,
        title=title,
        industry=industry,
        headcount=headcount,
        channel=channel,
        stage=stage,
        notes=notes,
        source="manual_add",
        source_platform="manual",
    )
    if result.get("status") == "error":
        return result
    status = "exists" if result["status"] == "matched" else "created"
    return {
        "status": status,
        "id": result["id"],
        "name": name,
        "email": result.get("email"),
        "linkedin": result.get("linkedin"),
    }


# Canonical profile keys (CSV, JSON, relay → leads table)
PROFILE_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "lead_email", "work_email"),
    "linkedin": ("linkedin url", "linkedin_url", "linkedin", "lead_linkedin_url", "profile_url"),
    "name": ("name", "full_name", "display_name"),
    "title": ("title", "job_title", "role", "job title"),
    "company": ("company", "company_name", "organization", "org"),
    "industry": ("industry", "linkedin industry", "linkedin_industry"),
    "headcount": (
        "headcount", "company_size", "employees", "employee_count", "company_headcount",
        "linkedin employees", "linkedin_employees", "linkedin company employee count",
        "linkedin_company_employee_count",
    ),
    "location_city": ("location_city", "city", "lead_city"),
    "location_state": ("location_state", "state", "region", "lead_state"),
    "location_country": ("location_country", "country", "lead_country"),
}


def _pick_profile_field(row: dict, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _best_linkedin_from_row(row: dict) -> Optional[str]:
    """Prefer a public LinkedIn URL over a Sales Nav hash when multiple columns are present."""
    public = None
    fallback = None
    for key in PROFILE_ALIASES["linkedin"]:
        val = row.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if not text:
            continue
        norm = normalize_linkedin(text)
        if norm and not linkedin_url_is_hash(norm):
            return text
    return None


def normalize_profile_row(row: dict) -> dict[str, str]:
    """Map CSV/JSON/webhook-shaped dicts to canonical profile fields."""
    out: dict[str, str] = {}
    for canonical, aliases in PROFILE_ALIASES.items():
        if canonical == "linkedin":
            val = _best_linkedin_from_row(row)
        else:
            val = _pick_profile_field(row, aliases)
        if val:
            out[canonical] = val
    first = _pick_profile_field(row, ("first_name", "first name"))
    last = _pick_profile_field(row, ("last_name", "last name"))
    if first and "name" not in out:
        out["name"] = f"{first} {last}".strip() if last else first
    return out


def profile_from_relay_lead(
    lead_fields: dict[str, str],
    identity: dict[str, str],
    display_name: str,
) -> dict[str, str]:
    """Build a canonical profile dict from relay extractor output."""
    row = {
        "email": identity.get("email"),
        "linkedin": identity.get("linkedin_url"),
        "name": display_name,
        "job_title": lead_fields.get("job_title"),
        "company_name": lead_fields.get("company_name"),
        "industry": lead_fields.get("industry"),
        "headcount": lead_fields.get("headcount"),
    }
    return normalize_profile_row(row)


def enrich_lead(
    lead_id,
    name=None,
    title=None,
    industry=None,
    company=None,
    headcount=None,
    overwrite: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    """Fill empty lead profile fields (won't overwrite non-empty unless overwrite=True)."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    row = conn.execute(
        "SELECT name, email, title, industry, company, headcount FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if not row:
        if own_conn:
            conn.close()
        return []
    updates, params, filled = [], [], []
    email = row["email"] or ""
    if name:
        current = (row["name"] or "").strip()
        derived = name_from_email(email) if email else ""
        if overwrite or not current or current == derived:
            updates.append("name = ?")
            params.append(name)
            filled.append("name")
    for col, val in [
        ("title", title),
        ("industry", industry),
        ("company", company),
        ("headcount", headcount),
    ]:
        if not val:
            continue
        if overwrite or not (row[col] or "").strip():
            updates.append(f"{col} = ?")
            params.append(val)
            filled.append(col)
    if updates:
        updates.append("updated_at = datetime('now')")
        conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", (*params, lead_id))
        if own_conn:
            conn.commit()
    if own_conn:
        conn.close()
    return filled


def _preview_enrich_fields(row, name, title, industry, company, headcount, overwrite) -> list[str]:
    """Dry-run: which columns would enrich_lead update?"""
    if not row:
        return list(filter(None, [name and "name", title and "title", industry and "industry",
                                  company and "company", headcount and "headcount"]))
    filled = []
    email = row["email"] or ""
    if name:
        current = (row["name"] or "").strip()
        derived = name_from_email(email) if email else ""
        if overwrite or not current or current == derived:
            filled.append("name")
    for col, val in [
        ("title", title),
        ("industry", industry),
        ("company", company),
        ("headcount", headcount),
    ]:
        if val and (overwrite or not (row[col] or "").strip()):
            filled.append(col)
    return filled


def upsert_lead_profile(
    profile: dict[str, str],
    *,
    channel: str = "email",
    stage: str = "prospecting",
    notes: Optional[str] = None,
    enrich_name: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    company_domain: Optional[str] = None,
    source: Optional[str] = None,
    source_detail: Optional[str] = None,
    source_platform: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    hq_city: Optional[str] = None,
    hq_state: Optional[str] = None,
    hq_country: Optional[str] = None,
    import_batch: Optional[str] = None,
    import_extra: Optional[dict[str, str]] = None,
    force_lead_id: Optional[int] = None,
) -> dict:
    """Match or create by tiered identities; enrich profile and company link."""
    extra = dict(import_extra or {})
    if company_domain and "company_domain" not in extra:
        extra["company_domain"] = company_domain

    name = profile.get("name")
    if not name:
        em = normalize_email(profile.get("email"))
        name = name_from_email(em) if em else "Unknown"

    idents = build_import_identities(
        profile, extra, import_batch=import_batch, company_domain=company_domain,
    )
    if not idents and force_lead_id is None:
        return {"status": "error", "error": "no identity: need email, linkedin, external_id, or name+company"}

    return resolve_lead(
        email=profile.get("email"),
        linkedin_url=profile.get("linkedin"),
        name=name,
        company=profile.get("company"),
        title=profile.get("title"),
        industry=profile.get("industry"),
        headcount=profile.get("headcount"),
        channel=channel,
        stage=stage,
        notes=notes,
        enrich_name=enrich_name,
        dry_run=dry_run,
        overwrite=overwrite,
        company_domain=company_domain,
        source=source,
        source_detail=source_detail,
        source_platform=source_platform,
        conn=conn,
        location_city=profile.get("location_city"),
        location_state=profile.get("location_state"),
        location_country=profile.get("location_country"),
        hq_city=hq_city,
        hq_state=hq_state,
        hq_country=hq_country,
        identities=idents,
        import_batch=import_batch,
        import_extra=extra,
        force_lead_id=force_lead_id,
    )


IMPORT_EXTRA_FIELDS = (
    "company_domain", "personalized_first_name", "personalized_company_name",
    "is_connected_linkedin", "is_linkedin_request_pending",
    "lead_status", "lead_sentiment", "import_name", "list_source",
    "tags", "contact_order",
    "hq_city", "hq_state", "hq_country",
    "external_id", "notes",
    "last_message_sent", "last_message_received",
    "member linkedin sales nav id", "linkedin_sales_nav_id", "sales_nav_id",
)

# Canonical → alias mapping applied before _extract_extra_import_fields.
# Keys in this dict are checked first; if the canonical key is absent from
# the raw row and an alias exists, the value is copied to the canonical key.
_EXTRA_FIELD_ALIASES: dict[str, str] = {
    "domain": "company_domain",
}

RESERVED_IMPORT_FIELDS = frozenset([
    "company_domain", "is_connected_linkedin", "is_linkedin_request_pending",
    "lead_status", "lead_sentiment", "import_name", "list_source",
    "tags", "contact_order", "hq_city", "hq_state", "hq_country",
    "external_id", "notes", "last_message_sent", "last_message_received",
    "member linkedin sales nav id", "linkedin_sales_nav_id", "sales_nav_id",
])

def csv_import_source_fields(
    extra: dict[str, str],
    *,
    default_source: str,
    default_source_detail: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Map CSV columns: list_source -> source, import_name -> source_detail."""
    list_src = (extra.get("list_source") or "").strip()
    import_name = (extra.get("import_name") or "").strip()
    lead_source = list_src or default_source
    lead_source_detail = import_name or default_source_detail
    return lead_source, lead_source_detail


def _extract_extra_import_fields(raw: dict) -> dict[str, str]:
    """Extract non-PROFILE_ALIASES fields from the raw CSV/JSON row."""
    out: dict[str, str] = {}
    # Normalise common aliases before the direct-lookup loop.
    for alias, canonical in _EXTRA_FIELD_ALIASES.items():
        if canonical not in raw and alias in raw:
            raw[canonical] = raw[alias]
    for key in IMPORT_EXTRA_FIELDS:
        val = raw.get(key)
        if val is not None:
            if key == "tags":
                parsed = parse_tags_value(val)
                if parsed:
                    out[key] = ";".join(parsed)
                continue

            if key == "notes":
                # Notes should be a single string blob (not a list).
                if isinstance(val, str):
                    text = val.strip()
                    if text:
                        out[key] = text
                else:
                    text = str(val).strip()
                    if text:
                        out[key] = text
                continue

            text = str(val).strip()
            if text:
                out[key] = text
    for key, val in raw.items():
        if not key.startswith("personalized_"):
            continue
        text = str(val).strip() if val is not None else ""
        if text:
            out[key] = text
    if not out.get("external_id"):
        ext = pick_external_id_from_raw(raw)
        if ext:
            out["external_id"] = ext
    return out



def _parse_cli_tags(raw: str) -> list[str]:
    """Parse --tags CLI argument (comma-separated and/or JSON/list literals)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if "," in raw and not (raw.startswith("[") and raw.endswith("]")):
        out: list[str] = []
        for part in raw.split(","):
            out.extend(parse_tags_value(part.strip()))
        return _dedupe_tags(out)
    return parse_tags_value(raw)


def _lead_id_hint_from_raw(raw: dict) -> Optional[int]:
    for key in ("lead_id", "id"):
        val = raw.get(key)
        if val is not None and str(val).strip().isdigit():
            return int(str(val).strip())
    return None


def import_rows_all_have_lead_id(rows: list[dict]) -> bool:
    if not rows:
        return False
    return all(_lead_id_hint_from_raw(row) is not None for row in rows)


def _tags_from_import_row(raw: dict, extra: dict[str, str]) -> list[str]:
    tags_val = raw.get("tags") if raw.get("tags") is not None else extra.get("tags")
    if not tags_val:
        return []
    if isinstance(tags_val, list):
        out: list[str] = []
        seen: set[str] = set()
        for item in tags_val:
            norm = normalize_tag(str(item))
            if norm and norm not in seen:
                out.append(norm)
                seen.add(norm)
        return out
    return _parse_tags(str(tags_val))


def _validity_to_verify_status(validity: str, *, provider: str) -> str:
    # NOTE: This function mirrors waterfall.validity_to_verify_status() from the
    # consolidated email-finder provider layer. It exists here because pipeline.py
    # processes batch results independently of the email-finder CLI. If the
    # verification logic in waterfall.py changes, keep this in sync.
    v = (validity or "").strip().lower()
    prov = (provider or "").strip().lower()
    if prov == "icypeas":
        if v in ("ultra_sure", "sure", "valid"):
            return "valid"
        if v in ("probable", "risky", "valid-risky"):
            return "catch_all"
        return "unknown"
    if v == "valid":
        return "valid"
    if v in ("valid-risky", "risky"):
        return "catch_all"
    if v == "invalid":
        return "invalid"
    return "unknown"


def _conflicting_email_owner(
    conn: sqlite3.Connection,
    email: str,
    lead_id: int,
) -> Optional[int]:
    """Return lead id that already owns this email, if different from lead_id."""
    owner = find_lead_by_email(conn, email)
    if owner is None or int(owner) == int(lead_id):
        return None
    return int(owner)


def _apply_email_find_email_sets(
    *,
    overwrite: bool,
    email_norm: str,
    domain_from_email: str,
) -> tuple[list[str], list]:
    if overwrite:
        return (
            ["email = ?", "email_domain = ?"],
            [email_norm, domain_from_email],
        )
    return (
        [
            "email = COALESCE(NULLIF(TRIM(email), ''), ?)",
            "email_domain = COALESCE(NULLIF(TRIM(email_domain), ''), ?)",
        ],
        [email_norm, domain_from_email],
    )


def apply_email_find_results(
    rows: list[dict],
    *,
    workspace: str,
    dry_run: bool = False,
    overwrite: bool = False,
    source: Optional[str] = None,
    source_detail: Optional[str] = None,
) -> dict:
    """Fast batch save when every row has a known lead id (outreachmagic email-finding batch tail).
    
    NOTE: This function processes the output shape produced by
    email_finder.py / waterfall.run_find_with_fallback(). The expected fields
    (email, validity, validSMTP, jobId, provider, credits_used, etc.) are defined
    in the provider modules (trykitt.py, icypeas.py, waterfall.py). If those
    output shapes change, update this function accordingly.
    """
    default_source = source if source is not None else "email_finder"
    summary: dict = {
        "processed": 0,
        "created": 0,
        "matched": 0,
        "enriched": 0,
        "personalized": 0,
        "tagged": 0,
        "recorded": 0,
        "email_conflicts": 0,
        "errors": [],
        "results": [],
        "mode": "apply_email_find_results",
    }

    ws_conn = get_conn()
    ws_row = resolve_workspace_identity(ws_conn, workspace)
    if not ws_row:
        ws_conn.close()
        summary["errors"].append({"error": f"Workspace not found: {workspace}"})
        return summary
    workspace_id = ws_row["id"]
    ensure_organization(ws_conn)

    if dry_run:
        for i, raw in enumerate(rows):
            lid = _lead_id_hint_from_raw(raw)
            if lid is None:
                summary["errors"].append({"row": i + 1, "error": "missing lead id"})
                continue
            summary["processed"] += 1
            summary["matched"] += 1
            summary["results"].append({
                "lead_id": lid,
                "id": lid,
                "status": "matched",
                "dry_run": True,
            })
        ws_conn.close()
        return summary

    now_ts = datetime.now(timezone.utc).isoformat()
    ws_tag_pending: list[tuple[int, list[str]]] = []
    verify_pending: list[dict] = []

    try:
        for i, raw in enumerate(rows):
            lid = _lead_id_hint_from_raw(raw)
            if lid is None:
                summary["errors"].append({"row": i + 1, "error": "missing lead id"})
                continue
            exists = ws_conn.execute("SELECT id FROM leads WHERE id = ?", (lid,)).fetchone()
            if not exists:
                summary["errors"].append({"row": i + 1, "lead_id": lid, "error": "lead not found"})
                continue

            profile = normalize_profile_row(raw)
            extra = _extract_extra_import_fields(raw)
            email_norm = normalize_email(profile.get("email"))
            row_source, row_source_detail = csv_import_source_fields(
                extra,
                default_source=default_source,
                default_source_detail=source_detail,
            )
            notes = extra.get("notes")

            email_conflict_id: Optional[int] = None
            email_sets: list[str] = []
            email_params: list = []
            if email_norm:
                email_conflict_id = _conflicting_email_owner(ws_conn, email_norm, lid)
                if not email_conflict_id:
                    email_sets, email_params = _apply_email_find_email_sets(
                        overwrite=overwrite,
                        email_norm=email_norm,
                        domain_from_email=email_domain(email_norm),
                    )

            meta_sets: list[str] = []
            meta_params: list = []
            if row_source:
                meta_sets.extend([
                    "latest_source = ?",
                    "latest_source_detail = ?",
                    "latest_source_platform = ?",
                    "latest_source_at = ?",
                    "original_source = COALESCE(original_source, ?)",
                    "original_source_detail = COALESCE(original_source_detail, ?)",
                    "original_source_platform = COALESCE(original_source_platform, ?)",
                    "original_source_at = COALESCE(original_source_at, ?)",
                ])
                meta_params.extend([
                    row_source, row_source_detail, "csv", now_ts,
                    row_source, row_source_detail, "csv", now_ts,
                ])
            if notes:
                if overwrite:
                    meta_sets.append("notes = ?")
                    meta_params.append(notes)
                else:
                    meta_sets.append(
                        "notes = CASE WHEN notes IS NULL OR notes = '' THEN ? ELSE notes END",
                    )
                    meta_params.append(notes)

            enriched = False
            email_skipped = bool(email_conflict_id)
            update_sets = [*email_sets, *meta_sets]
            update_params = [*email_params, *meta_params]
            if update_sets:
                update_sets.append("updated_at = datetime('now')")
                update_params.append(lid)
                try:
                    ws_conn.execute(
                        f"UPDATE leads SET {', '.join(update_sets)} WHERE id = ?",
                        update_params,
                    )
                    enriched = True
                except sqlite3.IntegrityError:
                    if email_sets:
                        email_skipped = True
                        retry_sets = [*meta_sets]
                        retry_params = [*meta_params]
                        if retry_sets:
                            retry_sets.append("updated_at = datetime('now')")
                            retry_params.append(lid)
                            try:
                                ws_conn.execute(
                                    f"UPDATE leads SET {', '.join(retry_sets)} WHERE id = ?",
                                    retry_params,
                                )
                                enriched = True
                            except sqlite3.IntegrityError as exc:
                                summary["errors"].append({
                                    "row": i + 1,
                                    "lead_id": lid,
                                    "error": str(exc),
                                })
                                continue
                    else:
                        summary["errors"].append({
                            "row": i + 1,
                            "lead_id": lid,
                            "error": "integrity constraint on lead update",
                        })
                        continue

            if email_skipped:
                summary["email_conflicts"] += 1

            summary["processed"] += 1
            summary["matched"] += 1
            if enriched:
                summary["enriched"] += 1
            row_result: dict = {
                "lead_id": lid,
                "id": lid,
                "status": "matched",
                "filled": enriched,
                "match_method": "lead_id",
            }
            if email_skipped:
                row_result["email_skipped"] = True
                if email_conflict_id:
                    row_result["email_conflict_lead_id"] = email_conflict_id
            summary["results"].append(row_result)

            tags = _tags_from_import_row(raw, extra)
            if tags:
                ws_tag_pending.append((lid, tags))

            provider = str(
                raw.get("_verify_provider") or extra.get("_verify_provider") or row_source or "",
            ).strip()
            validity = str(raw.get("_verify_validity") or extra.get("_verify_validity") or "").strip()
            if email_norm and provider:
                verify_pending.append({
                    "lead_id": lid,
                    "email": email_norm,
                    "status": _validity_to_verify_status(validity, provider=provider),
                    "source": provider,
                    "source_detail": source_detail or "email-finder/batch",
                })

        for lead_id, tags in ws_tag_pending:
            upsert_workspace_lead(
                ws_conn, DEFAULT_ORG_ID, workspace_id, lead_id,
                status="prospecting",
            )
            for tag in tags:
                tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
                ws_conn.execute(
                    """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                       VALUES (?, ?, ?, ?)""",
                    (tag_id, workspace_id, lead_id, tag),
                )
            summary["tagged"] += 1

        for item in verify_pending:
            out = verify_email(
                int(item["lead_id"]),
                item["status"],
                item["source"],
                email_override=item.get("email"),
                source_detail=item.get("source_detail"),
                conn=ws_conn,
                commit=False,
            )
            if out.get("status") == "recorded":
                summary["recorded"] += 1
            else:
                summary["errors"].append(out)

        ws_conn.commit()
    finally:
        ws_conn.close()

    return summary


def import_profiles(
    rows: list[dict],
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    channel: str = "email",
    stage: str = "prospecting",
    notes: Optional[str] = None,
    workspace: Optional[str] = None,
    sender_profile: Optional[str] = None,
    source: Optional[str] = None,
    source_detail: Optional[str] = None,
    import_batch_id: Optional[str] = None,
    import_format: Optional[str] = None,
) -> dict:
    """Import many profile rows (CSV dicts or JSON objects). Tiered identity match keys."""
    rows, import_meta = preprocess_import_rows(rows, import_format=import_format)
    default_source = source if source is not None else "csv_import"
    if default_source == "csv_import" and import_meta.get("detected_format") == "sales_navigator":
        default_source = "sales_navigator"
    summary: dict = {
        "processed": 0,
        "created": 0,
        "matched": 0,
        "enriched": 0,
        "personalized": 0,
        "tagged": 0,
        "weak_identity_count": 0,
        "import_key_only_count": 0,
        "skipped_no_identity": 0,
        "identity_conflicts": [],
        "linkedin_url_conflicts": [],
        "errors": [],
        "results": [],
        "skipped_features": [],
        "import_format": import_meta.get("detected_format"),
        "import_format_confidence": import_meta.get("confidence"),
    }
    if dry_run:
        summary["fields_mapped"] = import_meta.get("fields_mapped") or []
        summary["fields_dropped"] = import_meta.get("fields_dropped") or []
        summary["sample_preview"] = import_meta.get("sample_preview") or {}

    workspace_id = None
    if workspace:
        ws_conn = get_conn()
        ws_row = resolve_workspace_identity(ws_conn, workspace)
        ws_conn.close()
        if ws_row:
            workspace_id = ws_row["id"]
        else:
            summary["errors"].append({"error": f"Workspace not found: {workspace}"})
            return summary

    sender_normalized = normalize_linkedin(sender_profile) if sender_profile else None

    if not workspace_id:
        skip_features = []
        if any(r.get("tags") for r in rows[:5]):
            skip_features.append("tags (requires --workspace)")
        if any(r.get("lead_status") or r.get("lead_sentiment") for r in rows[:5]):
            skip_features.append("lead_status/lead_sentiment (requires --workspace)")
        if any(r.get("contact_order") for r in rows[:5]):
            skip_features.append("contact_order (requires --workspace)")
        if any(r.get("is_connected_linkedin") or r.get("is_linkedin_request_pending") for r in rows[:5]):
            skip_features.append("linkedin_status (requires --workspace and --sender-profile)")
        summary["skipped_features"] = skip_features

    ws_pending: list[tuple[int, dict]] = []

    personalize_columns_detected: list[str] = []
    if rows:
        for key in sorted(rows[0].keys()):
            if key.startswith("personalized_") and str(rows[0].get(key) or "").strip():
                field = key[len("personalized_"):]
                personalize_columns_detected.append(f"{key} -> {field}")
    if personalize_columns_detected and dry_run:
        summary["personalization_detected"] = personalize_columns_detected

    use_shared_conn = (
        not dry_run
        and import_rows_all_have_lead_id(rows)
    )
    shared_conn: Optional[sqlite3.Connection] = get_conn() if use_shared_conn else None

    for i, raw in enumerate(rows):
        profile = normalize_profile_row(raw)
        extra = _extract_extra_import_fields(raw)
        row_company_domain = normalize_company_domain(extra.get("company_domain"))
        row_notes = extra.get("notes") or notes
        lead_id_hint = _lead_id_hint_from_raw(raw)
        idents = build_import_identities(
            profile, extra, import_batch=import_batch_id, company_domain=row_company_domain,
        )
        if not idents and not lead_id_hint:
            summary["skipped_no_identity"] += 1
            summary["errors"].append({"row": i + 1, "error": "no identity"})
            continue
        summary["processed"] += 1

        row_source, row_source_detail = csv_import_source_fields(
            extra,
            default_source=default_source,
            default_source_detail=source_detail,
        )
        row_hq_city = extra.get("hq_city")
        row_hq_state = extra.get("hq_state")
        row_hq_country = extra.get("hq_country")

        try:
            result = upsert_lead_profile(
                profile,
                channel=channel,
                stage=stage,
                notes=row_notes,
                dry_run=dry_run,
                overwrite=overwrite,
                company_domain=row_company_domain,
                source=row_source,
                source_detail=row_source_detail,
                source_platform="csv",
                hq_city=row_hq_city,
                hq_state=row_hq_state,
                hq_country=row_hq_country,
                import_batch=import_batch_id,
                import_extra=extra,
                force_lead_id=lead_id_hint,
                conn=shared_conn,
            )
        except Exception as e:
            summary["errors"].append({"row": i + 1, "email": profile.get("email"), "error": str(e)})
            continue
        if result.get("status") == "error":
            summary["errors"].append({"row": i + 1, "email": profile.get("email"), "error": result.get("error")})
            continue
        summary["results"].append(result)
        if result["status"] == "created":
            summary["created"] += 1
        else:
            summary["matched"] += 1
        if result.get("filled"):
            summary["enriched"] += 1
        conf = result.get("match_confidence")
        if conf in ("medium", "low"):
            summary["weak_identity_count"] += 1
        if result.get("match_method") == "import_key":
            summary["import_key_only_count"] += 1
        for ic in result.get("identity_conflicts") or []:
            summary["identity_conflicts"].append({"row": i + 1, **ic})
        for lc in result.get("linkedin_url_conflicts") or []:
            summary["linkedin_url_conflicts"].append({
                "row": i + 1,
                "lead_id": result.get("id"),
                **lc,
            })

        if dry_run:
            continue

        lead_id = result["id"]

        lead_items = []
        co_items = []
        for key, val in extra.items():
            if not val:
                continue
            
            field = None
            if key.startswith("personalized_"):
                field = key[len("personalized_"):]
            elif key not in RESERVED_IMPORT_FIELDS:
                field = key
            
            if not field:
                continue

            item = {"field": field, "value": val}
            if is_company_personalization_field(field):
                co_items.append(item)
            else:
                lead_items.append({"lead_id": lead_id, **item})
        if lead_items:
            personalize_set_batch(lead_items)
        if co_items:
            lid_conn = get_conn()
            cid_row = lid_conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
            lid_conn.close()
            if cid_row and cid_row["company_id"]:
                for item in co_items:
                    company_personalize_set(
                        item["field"], item["value"], company_id=cid_row["company_id"],
                    )
        if lead_items or co_items:
            summary["personalized"] += 1

        if workspace_id:
            ws_pending.append((lead_id, extra))

    if shared_conn is not None:
        shared_conn.commit()
        shared_conn.close()

    # Batch workspace operations after all leads are resolved (avoids SQLite lock contention)
    if workspace_id and ws_pending:
        ws_conn = get_conn()
        ensure_organization(ws_conn)
        for lead_id, extra in ws_pending:
            status_label = (extra.get("lead_status") or "").strip().lower().replace("_", " ") or None
            status_sentiment = (extra.get("lead_sentiment") or "").strip().lower() or None
            contact_pri = None
            if extra.get("contact_order"):
                try:
                    contact_pri = int(extra["contact_order"])
                except (ValueError, TypeError):
                    pass

            upsert_workspace_lead(
                ws_conn, DEFAULT_ORG_ID, workspace_id, lead_id,
                status=stage,
                current_status_label=status_label,
                current_status_sentiment=status_sentiment,
                contact_priority=contact_pri,
            )

            raw_tags = extra.get("tags")
            if raw_tags:
                parsed_tags = _parse_tags(raw_tags)
                for tag in parsed_tags:
                    tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
                    ws_conn.execute(
                        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                           VALUES (?, ?, ?, ?)""",
                        (tag_id, workspace_id, lead_id, tag),
                    )
                summary["tagged"] += 1

            if sender_normalized:
                is_connected = extra.get("is_connected_linkedin", "").lower() in ("true", "1", "yes")
                is_pending = extra.get("is_linkedin_request_pending", "").lower() in ("true", "1", "yes")
                if is_connected or is_pending:
                    now_ts = datetime.now(timezone.utc).isoformat()
                    li_id = f"lis_{workspace_id}_{lead_id}_{sender_normalized[:20]}"
                    ws_conn.execute(
                        """INSERT INTO workspace_lead_linkedin_status
                           (id, workspace_id, lead_id, sender_profile, is_connected,
                            is_request_pending, connected_at, request_sent_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (workspace_id, lead_id, sender_profile) DO UPDATE SET
                               is_connected = excluded.is_connected,
                               is_request_pending = excluded.is_request_pending,
                               connected_at = CASE WHEN excluded.is_connected = 1
                                   THEN COALESCE(excluded.connected_at, connected_at) ELSE connected_at END,
                               updated_at = datetime('now')""",
                        (li_id, workspace_id, lead_id, sender_normalized,
                         1 if is_connected else 0, 1 if is_pending else 0,
                         now_ts if is_connected else None,
                         now_ts if is_pending else None),
                    )

        ws_conn.commit()
        ws_conn.close()

    warnings = build_import_quality_warnings(summary)
    if warnings:
        summary["warnings"] = warnings
        if not dry_run:
            for w in warnings:
                print(w, file=sys.stderr)

    if not dry_run and (summary["created"] or summary["matched"]):
        counts = get_local_pending_counts()
        if counts.get("leads_pending") or counts.get("workspace_leads_pending"):
            summary["sync_hint"] = "Run: pipeline.py sync to push imported leads to the relay."

    return summary


from pipeline_tags import (
    tag_add, tag_remove, tag_set, tag_list, tag_bulk,
    get_workspace_summary, format_workspace_summary, update_lead_stage,
    log_event, get_lead_events, export_leads, get_pipeline, get_stats,
    get_campaign_stats, get_stage_counts, get_lead_by_email,
    load_json_array_from_cli, load_profile_rows_from_file,
    enrich_lead_rows, query_leads_for_export, get_copy_insights,
    get_segment_insights,
    # Re-exports for lazy-import callers (pipeline_migration, workspace routing):
    backfill_campaigns_from_events, backfill_plusvibe_status_metadata,
    ensure_campaign, _decode_event_metadata, cap_event_body,
    _update_lead_sender,
)


# ──────────────────────────────────────────────────────────────────────
# Workspace routing (org lead + workspace-scoped events)
# ──────────────────────────────────────────────────────────────────────

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
        """SELECT COUNT(*) AS n FROM leads
           WHERE id NOT IN (
               SELECT DISTINCT lead_id FROM relay_ingested WHERE lead_id IS NOT NULL
           )"""
    ).fetchone()["n"]
    local_event_count = conn2.execute(
        """SELECT COUNT(*) AS n FROM events
           WHERE id NOT IN (
               SELECT CAST(SUBSTR(dedupe_key, 7) AS INTEGER)
               FROM relay_ingested
               WHERE dedupe_key LIKE 'event:%'
           )
             AND metadata_json NOT LIKE '%"source": "relay"%'
             AND metadata_json NOT LIKE '%"source":"relay"%'
             AND metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND metadata_json NOT LIKE '%"source":"agent_sync"%'"""
    ).fetchone()["n"]
    last_sync = get_last_sync()
    if last_sync:
        pending_lead_core_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE updated_at > ?", (last_sync,)
        ).fetchone()["n"]
        pending_workspace_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM workspace_leads WHERE updated_at > ?", (last_sync,)
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
        """SELECT COUNT(*) AS n FROM events
           WHERE id NOT IN (
               SELECT CAST(SUBSTR(dedupe_key, 7) AS INTEGER)
               FROM relay_ingested
               WHERE dedupe_key LIKE 'event:%'
           )
             AND metadata_json NOT LIKE '%"source": "relay"%'
             AND metadata_json NOT LIKE '%"source":"relay"%'
             AND metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND metadata_json NOT LIKE '%"source":"agent_sync"%'"""
    ).fetchone()["n"]
    last_sync = get_last_sync()
    if last_sync:
        leads_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE updated_at > ?", (last_sync,)
        ).fetchone()["n"]
        ws_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_leads WHERE updated_at > ?", (last_sync,)
        ).fetchone()["n"]
    else:
        leads_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM leads"
        ).fetchone()["n"]
        ws_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_leads"
        ).fetchone()["n"]
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

        if lead_push.get("error") is None and company_push.get("error") is None:
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
    """Push locally-created events to the Cloudflare relay /push endpoint."""
    events_only = _sync_events_only()
    if events_only:
        _relay_log(
            f"{_ARROW_PUSH} {_stream_pad(_RELAY_STREAM_EVENT)}: "
            "building export (events only, skipping Lead/Workspace) ..."
        )
    t0 = time.monotonic()
    export = export_local_changes(events_only=events_only)
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
        conn = get_conn()
        now_ts = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(marked_event_ids), 100):
            for eid in marked_event_ids[i : i + 100]:
                conn.execute(
                    "INSERT OR IGNORE INTO relay_ingested (dedupe_key, ingested_at) VALUES (?, ?)",
                    (f"event:{eid}", now_ts),
                )
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


def _push_pending_merge_deletes(agent_key: str, *, bulk: bool = False) -> dict:
    """Push tombstones for merged leads so relay drops stale entity keys."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, merge_entity_key FROM lead_merges
           WHERE merge_entity_key IS NOT NULL AND TRIM(merge_entity_key) != ''
             AND COALESCE(relay_delete_pushed, 0) = 0"""
    ).fetchall()
    if not rows:
        conn.close()
        return {"pushed": 0, "error": None}

    client_id = get_or_create_client_id()
    now_ts = datetime.now(timezone.utc).isoformat()
    entries = [
        {
            "action": "lead_core_delete",
            "entity_key": row["merge_entity_key"],
            "timestamp": now_ts,
            "payload": {"reason": "merge"},
        }
        for row in rows
    ]
    mark_ids = [row["id"] for row in rows]
    conn.close()

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

    return _relay_push_batches(
        agent_key,
        entries,
        client_id,
        stream_label="merge_delete",
        bulk=bulk,
        snapshot_bulk=True,
        mark_ids=mark_ids,
        on_mark_cleared=clear_merge_ids,
    )


def _push_pending_lead_snapshots(agent_key: str, *, bulk: Optional[bool] = None,
                                 workspace: Optional[str] = None) -> dict:
    """Push pending lead core + workspace snapshots to relay /push.

    When ``workspace`` is provided, only snapshots scoped to that workspace
    are pushed. Pass ``None`` to push everything (default).
    """
    conn = get_conn()
    last_sync = get_last_sync()

    if workspace:
        ws_row = resolve_workspace_identity(conn, workspace)
        ws_id = ws_row["id"] if ws_row else None
        if ws_id is None:
            conn.close()
            return {"pushed": 0, "error": f"workspace not found: {workspace}", "throttled": False}
        if last_sync:
            core_rows = conn.execute(
                """SELECT DISTINCT l.id, l.updated_at
                   FROM leads l
                   JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
                   WHERE l.updated_at > ?""",
                (ws_id, last_sync),
            ).fetchall()
            ws_rows = conn.execute(
                """SELECT wl.lead_id, wl.workspace_id, wl.updated_at, w.slug
                   FROM workspace_leads wl
                   JOIN workspaces w ON w.id = wl.workspace_id
                   WHERE wl.updated_at > ? AND wl.workspace_id = ?""",
                (last_sync, ws_id),
            ).fetchall()
        else:
            core_rows = conn.execute(
                """SELECT DISTINCT l.id, l.updated_at
                   FROM leads l
                   JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?""",
                (ws_id,),
            ).fetchall()
            ws_rows = conn.execute(
                """SELECT wl.lead_id, wl.workspace_id, wl.updated_at, w.slug
                   FROM workspace_leads wl
                   JOIN workspaces w ON w.id = wl.workspace_id
                   WHERE wl.workspace_id = ?""",
                (ws_id,),
            ).fetchall()
    else:
        if last_sync:
            core_rows = conn.execute(
                "SELECT id, updated_at FROM leads WHERE updated_at > ?", (last_sync,)
            ).fetchall()
            ws_rows = conn.execute(
                """SELECT wl.lead_id, wl.workspace_id, wl.updated_at, w.slug
                   FROM workspace_leads wl
                   JOIN workspaces w ON w.id = wl.workspace_id
                   WHERE wl.updated_at > ?""",
                (last_sync,),
            ).fetchall()
        else:
            core_rows = conn.execute(
                "SELECT id, updated_at FROM leads"
            ).fetchall()
            ws_rows = conn.execute(
                """SELECT wl.lead_id, wl.workspace_id, wl.updated_at, w.slug
                   FROM workspace_leads wl
                   JOIN workspaces w ON w.id = wl.workspace_id"""
            ).fetchall()
    if not core_rows and not ws_rows:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False}

    _relay_log(
        f"snapshots: {len(core_rows):,} lead core + {len(ws_rows):,} workspace rows pending"
    )
    lead_ids = sorted({r["id"] for r in core_rows} | {r["lead_id"] for r in ws_rows})
    t_prefetch = time.monotonic()
    prefetch = _load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, lead_ids)
    _relay_log(f"snapshots: prefetched {len(lead_ids):,} leads in {time.monotonic() - t_prefetch:.1f}s")
    client_id = get_or_create_client_id()

    core_entries: list[dict] = []
    t_core = time.monotonic()
    for n, row in enumerate(core_rows, start=1):
        lead_id = row["id"]
        entity_key = entity_key_from_prefetch(prefetch, lead_id) or lead_entity_key(
            conn, DEFAULT_ORG_ID, lead_id,
        )
        if not entity_key:
            continue
        payload = build_lead_core_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, prefetch=prefetch,
        )
        if not payload:
            continue
        core_entries.append({
            "action": "lead_core_update",
            "entity_key": entity_key,
            "timestamp": normalize_relay_timestamp(row["updated_at"]),
            "payload": payload,
        })
        if n % 2500 == 0:
            _relay_log(f"snapshots: built {n:,}/{len(core_rows):,} lead_core payloads ...")
    _relay_log(
        f"snapshots: {len(core_entries):,} lead_core entries in {time.monotonic() - t_core:.1f}s"
    )

    ws_entries: list[dict] = []
    t_ws = time.monotonic()
    for n, row in enumerate(ws_rows, start=1):
        lead_id = row["lead_id"]
        entity_key = entity_key_from_prefetch(prefetch, lead_id) or lead_entity_key(
            conn, DEFAULT_ORG_ID, lead_id,
        )
        if not entity_key:
            continue
        ws_slug = row["slug"]
        payload = build_lead_workspace_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug=ws_slug, prefetch=prefetch,
        )
        if not payload:
            continue
        ws_entries.append({
            "action": "lead_workspace_update",
            "entity_key": entity_key,
            "workspace": ws_slug,
            "timestamp": normalize_relay_timestamp(row["updated_at"]),
            "payload": payload,
        })
        if n % 2500 == 0:
            _relay_log(f"snapshots: built {n:,}/{len(ws_rows):,} workspace payloads ...")
    _relay_log(
        f"snapshots: {len(ws_entries):,} workspace entries in {time.monotonic() - t_ws:.1f}s"
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
        if ws_result.get("error"):
            last_result["pushed"] = total_pushed
            return last_result

    last_result["pushed"] = total_pushed
    return last_result


def _push_pending_company_updates(agent_key: str) -> dict:
    conn = get_conn()
    last_sync = get_last_sync()
    if last_sync:
        rows = conn.execute("SELECT id, updated_at FROM companies WHERE updated_at > ?", (last_sync,)).fetchall()
    else:
        rows = conn.execute("SELECT id, updated_at FROM companies").fetchall()
    if not rows:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False}

    client_id = get_or_create_client_id()
    entries = []
    for row in rows:
        entity_key = company_entity_key(conn, row["id"])
        if not entity_key:
            continue
        payload = build_company_sync_payload(conn, row["id"])
        entries.append({
            "action": "company_update",
            "entity_key": entity_key,
            "timestamp": normalize_relay_timestamp(row["updated_at"]),
            "payload": payload,
        })
    conn.close()
    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}

    bulk = len(entries) >= RELAY_BULK_THRESHOLD

    push_result = _relay_push_batches(
        agent_key,
        entries,
        client_id,
        stream_label=_SNAPSHOT_KIND_STREAM["company"],
        bulk=bulk,
        snapshot_bulk=True,
    )
    return push_result


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
    conn.commit()
    conn.close()
    result = {"status": "created", "map_id": map_id, "workspace_id": ws["id"]}
    if cloud_warning:
        result["cloud_warning"] = cloud_warning
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


def _push_pending_quarantine_resolutions(agent_key: str) -> dict:
    conn = get_conn()
    last_sync = get_last_sync()
    if last_sync:
        rows = conn.execute(
            """SELECT external_event_id, status, assigned_workspace, resolved_at
               FROM unmapped_campaign_queue
               WHERE resolved_at > ? AND status IN ('skipped', 'assigned')""",
            (last_sync,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT external_event_id, status, assigned_workspace, resolved_at
               FROM unmapped_campaign_queue
               WHERE status IN ('skipped', 'assigned')"""
        ).fetchall()
    resolves: list[dict] = []
    relay_ids_sent: list[int] = []
    for row in rows:
        relay_id = _quarantine_relay_id(dict(row))
        if not relay_id:
            continue
        relay_ids_sent.append(relay_id)
        entry: dict = {
            "relay_id": relay_id,
            "status": row["status"],
            "resolved_at": row["resolved_at"] or normalize_relay_timestamp(None),
        }
        if row["status"] == "assigned":
            entry["workspace_slug"] = row["assigned_workspace"]
        resolves.append(entry)
    conn.close()

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



from pipeline_sync import (
    _account_access_revoked,
    _agent_sync_payload_from_entity_key,
    _ARROW_PUSH,
    _check_account_access_revoked,
    _DB_OPTIONAL_COMMANDS,
    _estimate_relay_pages,
    _format_pull_pending_banner,
    _format_pull_progress,
    _format_push_pending_banner,
    _format_push_progress,
    _ingest_relay_page,
    _page_label,
    _progress_clock,
    _progress_eta_seconds,
    _pull_diagnostics_verdict,
    _pull_failure_message,
    _pull_workspace_slug_map,
    _refresh_staging_path,
    _relay_pull_phases,
    _RELAY_STREAM_EVENT,
    _require_agent_key,
    _save_agent_key_and_validate,
    _snapshot_pending_count,
    _snapshot_pull_limit_for_kind,
    _stream_pad,
    cmd_connect_platform,
    cmd_connections,
    cmd_disconnect_platform,
    cmd_platform_map,
    cmd_refresh,
    cmd_restore,
    cmd_status,
    cmd_whoami,
    export_local_changes,
    find_lead_by_identifier,
    format_pull_summary,
    ingest_agent_entry,
    list_database_backups,
    login,
    logout,
    parse_pull_kinds,
    print_relay_probe,
    probe_relay_backlog,
    pull_events_org,
    refresh_local_database,
    replay_pending_quarantine,
    require_share_email_for_export,
    resolve_share_email,
    resolve_sheets_export_access,
    restore_local_database,
    sync_from_relay_org,
)

from pipeline_personalize import (
    _apply_personalization_payload,
    _company_personalization_dict,
    _COMPANY_SOURCE_FIELDS,
    _company_source_hash,
    _lead_personalization_dict,
    _LEAD_SOURCE_FIELDS,
    _lead_source_hash,
    _personalization_sync_payload,
    apply_agent_company_sync_payload,
    build_company_sync_payload,
    cleanup_campaign_rules,
    company_entity_key,
    company_personalize_get,
    company_personalize_pending,
    company_personalize_set,
    company_personalize_set_batch,
    company_personalize_status,
    is_company_personalization_field,
    personalize_clear,
    personalize_get,
    personalize_pending,
    personalize_set,
    personalize_set_batch,
    personalize_status,
    resolve_company_from_entity_key,
    resolve_company_id,
    resolve_personalization,
)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def _remap_to_lead_review_export(args) -> None:
    """Route sheets export (or export --format sheets) to review export lead-review."""
    args.command = "review"
    args.review_command = "export"
    args.template = "lead-review"
    if not getattr(args, "title", None):
        ws = getattr(args, "workspace", None) or "leads"
        args.title = f"{ws} leads"
    if not getattr(args, "detail", None):
        args.detail = "standard"
    for name, default in (
        ("limit", 5000),
        ("never_contacted", False),
        ("no_email", False),
        ("require_domain", False),
        ("share_email", None),
        ("public", False),
        ("anyone_with_link", False),
        ("sheet_id", None),
        ("parent_sheet_id", None),
        ("tab_name", None),
        ("fields", None),
        ("tag", None),
        ("stage", None),
        ("since", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)


def _cmd_sheets_campaign_stats(args) -> None:
    """Handler for `sheets campaign-stats` — build payload and POST to backend."""
    ws = getattr(args, "workspace", None)
    if not ws:
        print(json.dumps({"error": "--workspace required for sheets campaign-stats"}))
        sys.exit(1)

    from campaign_stats import build_campaign_stats_payload
    from db_conn import get_conn

    conn = get_conn()
    try:
        payload = build_campaign_stats_payload(
            conn,
            workspace=ws,
            since=getattr(args, "since", None),
        )
    finally:
        conn.close()

    # Dry-run or JSON preview: print payload and exit
    if getattr(args, "dry_run", False) or getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return

    tok = get_agent_key()
    if not tok:
        print(json.dumps({"error": "login required — ask Outreach Magic to log in"}))
        sys.exit(1)

    api_base = review_cloud.get_api_base(load_config)

    share_email, public_link = resolve_sheets_export_access(args)
    sheet_id = getattr(args, "sheet_id", None)
    update_mode = getattr(args, "update", False)

    # --update: look up the last sheet_id for this workspace
    if update_mode and not sheet_id:
        from om_paths import get_sheets_dir
        safe_ws = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws)[:40]
        latest_path = get_sheets_dir() / f"campaign-stats-{safe_ws}.latest.json"
        if latest_path.exists():
            try:
                saved = json.loads(latest_path.read_text(encoding="utf-8"))
                sid = saved.get("sheet_id")
                if sid:
                    sheet_id = str(sid).strip()
                    print(f"Reusing saved sheet {sheet_id}", file=sys.stderr)
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        if not sheet_id:
            print("No saved sheet found for this workspace. Creating a new one.", file=sys.stderr)

    result = review_cloud.export_review(
        api_base,
        tok,
        template=payload["template"],
        title=payload["title"],
        sheets=payload["sheets"],
        workspace=ws,
        share_email=share_email,
        public_link=public_link,
        sheet_id=str(sheet_id).strip() if sheet_id else None,
        freeze_header=True,
    )

    if isinstance(result, dict) and result.get("sheet_id"):
        try:
            meta_path = save_sheets_export_record(
                workspace=ws,
                title=payload["title"],
                sheet_id=str(result["sheet_id"]),
                url=str(result.get("url") or result.get("spreadsheet_url") or ""),
                detail="campaign-stats",
            )
            result = dict(result)
            result["metadata_path"] = str(meta_path)

            # Save or update the .latest file so --update can find it later
            from om_paths import get_sheets_dir
            safe_ws = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws)[:40]
            latest_path = get_sheets_dir() / f"campaign-stats-{safe_ws}.latest.json"
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            latest_path.write_text(json.dumps({
                "workspace": ws,
                "sheet_id": str(result["sheet_id"]),
                "url": str(result.get("url") or result.get("spreadsheet_url") or ""),
                "title": payload["title"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# CRM sync subprocess hook
# ---------------------------------------------------------------------------

CRM_SYNCABLE_STATUSES = frozenset({"interested", "replied", "scheduled", "won", "not_interested", "lost"})


def _maybe_trigger_crm_sync(
    *,
    lead_id: int,
    stage: str | None = None,
    workspace_slug: str | None = None,
) -> None:
    """If --crm-sync was passed and conditions are met, fire crm_sync.py as a subprocess."""
    if stage is not None and stage not in CRM_SYNCABLE_STATUSES:
        return
    if not workspace_slug:
        return
    crm_sync_path = Path(__file__).parent / "crm_sync.py"
    if not crm_sync_path.exists():
        return
    args = [sys.executable, str(crm_sync_path), "sync", "--lead-id", str(lead_id), "--workspace", workspace_slug]
    subprocess.Popen(args)
    print(f"CRM sync triggered for lead {lead_id} in workspace {workspace_slug}", file=sys.stderr)


def _cmd_crm_sync(args) -> None:
    """Dispatch pipeline.py crm-sync to the standalone crm_sync.py subprocess."""
    crm_sync_path = Path(__file__).parent / "crm_sync.py"
    if not crm_sync_path.exists():
        print(json.dumps({"error": "crm_sync.py not found"}))
        sys.exit(1)

    action = getattr(args, "action", None) or "status"
    cmd = [sys.executable, str(crm_sync_path), action]

    if action == "sync":
        if getattr(args, "workspace", None):
            cmd.extend(["--workspace", args.workspace])
        elif getattr(args, "all", False):
            cmd.append("--all")
        else:
            print(json.dumps({"error": "Use --workspace=<slug> or --all for sync"}))
            sys.exit(1)
        if getattr(args, "dry_run", False):
            cmd.append("--dry-run")
        if getattr(args, "lead_id", None):
            cmd.extend(["--lead-id", str(args.lead_id)])
        if getattr(args, "skip_events", False):
            cmd.append("--skip-events")
        if getattr(args, "platform", None):
            cmd.extend(["--platform", args.platform])
    elif action == "discover":
        ws = getattr(args, "workspace", None)
        platform = getattr(args, "platform", None)
        if not ws:
            print(json.dumps({"error": "--workspace is required for discover"}))
            sys.exit(1)
        cmd.extend(["--workspace", ws])
        if platform:
            cmd.extend(["--platform", platform])

    # Fire and forget — crm_sync.py prints its own output
    result = subprocess.run(cmd, capture_output=False, text=True)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Outreach Magic — Pipeline visibility for Hermes")
    sub = parser.add_subparsers(dest="command", help="Commands")

    init_p = sub.add_parser("init", help="Initialize the database")
    init_p.add_argument(
        "--from-tag",
        dest="from_tag",
        help=argparse.SUPPRESS,
    )
    init_p.add_argument(
        "--agent",
        choices=AGENT_DIR_NAMES,
        help="Which agent tool you use (cursor, agents, claude, hermes). "
        "Sets data_root so all skill copies share one database.",
    )
    sub.add_parser("version", help="Print installed outreachmagic version")
    sub.add_parser(
        "paths",
        help="Print resolved install, config, and database paths (JSON)",
    )

    update_p = sub.add_parser(
        "update",
        help="Install skill scripts from the latest GitHub release (user-triggered)",
    )
    update_p.add_argument("--check", action="store_true", help="Only check for updates, do not install")
    update_p.add_argument("--tag", help="Install a specific release tag (e.g. v1.4.5)")
    update_p.add_argument(
        "--channel",
        choices=("release", "main"),
        default="release",
        help="release (default): latest GitHub tag; main: moving main branch (power users)",
    )

    rollback_p = sub.add_parser(
        "rollback",
        help="Restore skill scripts from backup taken before the last pipeline.py update",
    )

    show_p = sub.add_parser("show", help="Show pipeline")
    show_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    show_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    show_p.add_argument("--stage")
    show_p.add_argument("--sentiment", choices=("positive", "negative", "autoreply", "invalid"),
                        help="Filter by current lead status sentiment (latest status event)")
    show_p.add_argument("--auto-reply", dest="auto_reply", choices=("true", "false"),
                        help="Filter by current auto-reply flag (OOO, etc.)")
    show_p.add_argument("--lead-status", dest="lead_status",
                        help="Filter by current lead status label (e.g. interested, not_interested)")
    show_p.add_argument("--sort", choices=("updated_at", "sentiment", "auto_reply", "status_at"),
                        default="updated_at")
    show_p.add_argument("--order", choices=("asc", "desc"), default="desc")
    show_p.add_argument("--limit", type=int, default=50)
    show_p.add_argument("--workspace", help="Filter by workspace name or slug")
    show_p.add_argument("--since", help="Show leads created or updated on/after this date (YYYY-MM-DD or 'today')")
    show_p.add_argument("--email", help="Filter by exact email")
    show_p.add_argument("--name", help="Filter by name (partial match)")
    show_p.add_argument("--json", action="store_true")

    lead_table_p = sub.add_parser("lead-table", help="Show canonical lead information table")
    lead_table_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    lead_table_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    lead_table_p.add_argument("--stage")
    lead_table_p.add_argument("--sentiment", choices=("positive", "negative", "autoreply", "invalid"),
                              help="Filter by current lead status sentiment (latest status event)")
    lead_table_p.add_argument("--auto-reply", dest="auto_reply", choices=("true", "false"),
                              help="Filter by current auto-reply flag (OOO, etc.)")
    lead_table_p.add_argument("--lead-status", dest="lead_status",
                              help="Filter by current lead status label (e.g. interested, not_interested)")
    lead_table_p.add_argument("--sort", choices=("updated_at", "sentiment", "auto_reply", "status_at"),
                              default="updated_at")
    lead_table_p.add_argument("--order", choices=("asc", "desc"), default="desc")
    lead_table_p.add_argument("--limit", type=int, default=50)
    lead_table_p.add_argument("--workspace", help="Filter by workspace name or slug")
    lead_table_p.add_argument("--since", help="Show leads created or updated on/after this date (YYYY-MM-DD or 'today')")
    lead_table_p.add_argument("--email", help="Filter by exact email")
    lead_table_p.add_argument("--name", help="Filter by name (partial match)")
    lead_table_p.add_argument("--markdown", action="store_true", help="Render as markdown table")
    lead_table_p.add_argument("--json", action="store_true")

    stats_p = sub.add_parser("stats", help="Pipeline statistics")
    stats_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    stats_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    stats_p.add_argument("--json", action="store_true")

    camp_p = sub.add_parser("campaigns", help="Event and lead counts by campaign name")
    camp_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    camp_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    camp_p.add_argument("--json", action="store_true")

    summary_p = sub.add_parser(
        "summary",
        help="Lightweight daily digest (counts and reply highlights)",
    )
    summary_p.add_argument("--since", help="Date or window: today, YYYY-MM-DD, 48h, 7d")
    summary_p.add_argument("--workspace", help="Workspace slug (campaign prefix filter)")
    summary_p.add_argument("--campaign-prefix", help="Override campaign name LIKE prefix")
    summary_p.add_argument("--pull", action="store_true", help="Pull before summarizing")
    summary_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    summary_p.add_argument("--json", action="store_true")

    pmap_p = sub.add_parser(
        "platform-map",
        help="Show platform/event type mappings (use --json for agents)",
    )
    pmap_p.add_argument("--platform", help="Filter to one platform id (e.g. prosp)")
    pmap_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add-lead", help="Add a lead")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--company"); add_p.add_argument("--title")
    add_p.add_argument("--industry"); add_p.add_argument("--headcount")
    add_p.add_argument("--email"); add_p.add_argument("--linkedin")
    add_p.add_argument("--channel", default="email"); add_p.add_argument("--stage", default="prospecting")
    add_p.add_argument("--notes")
    add_p.add_argument("--workspace", help="Optional: associate lead with a workspace")

    imp_p = sub.add_parser(
        "import-profiles",
        help="Bulk import/enrich leads from CSV or JSON (tiered identity match)",
    )
    imp_p.add_argument("--file", help="Path to .csv, .json, or .jsonl file")
    imp_p.add_argument(
        "--json",
        dest="json_data",
        help='JSON array string, or "-" to read JSON array from stdin',
    )
    imp_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    imp_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt (no-op: import-profiles is non-destructive)")
    imp_p.add_argument("--overwrite", action="store_true", help="Overwrite non-empty profile fields")
    imp_p.add_argument("--channel", default="email")
    imp_p.add_argument("--stage", default="prospecting")
    imp_p.add_argument("--notes")
    imp_p.add_argument("--workspace", help="Workspace slug/ID to associate imported leads with")
    imp_p.add_argument("--sender-profile", dest="sender_profile", help="LinkedIn sender profile URL for connection status tracking")
    imp_p.add_argument(
        "--source",
        dest="source",
        help="Attribution source (e.g. sales_navigator, csv_import, trykitt, icypeas, lead_enrich)",
    )
    imp_p.add_argument("--source-detail", dest="source_detail", help="Attribution source detail (e.g. list name)")
    imp_p.add_argument(
        "--import-batch-id",
        dest="import_batch_id",
        help="Stable batch id for name-only rows (import_key dedupe within batch)",
    )

    imp_p.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip automatic pipeline.py sync after import (default: auto-sync when logged in)",
    )
    imp_p.add_argument(
        "--import-format",
        dest="import_format",
        choices=("auto", "generic", "sales_navigator"),
        default="auto",
        help="CSV format preset (auto-detect Sales Nav / Vayne exports by default)",
    )

    aef_p = sub.add_parser(
        "apply-email-find-results",
        help="Fast batch save when every row has lead id (outreachmagic email-finding)",
    )
    aef_p.add_argument("--file", help="Path to .json file")
    aef_p.add_argument("--json", dest="json_data", help='JSON array string, or "-" for stdin')
    aef_p.add_argument("--workspace", required=True, help="Workspace slug/ID (required for tags)")
    aef_p.add_argument("--dry-run", action="store_true")
    aef_p.add_argument("--overwrite", action="store_true")
    aef_p.add_argument("--source", dest="source", help="Attribution source (trykitt, icypeas, …)")
    aef_p.add_argument("--source-detail", dest="source_detail")

    tag_p = sub.add_parser("tag", help="Manage workspace-scoped lead tags")
    tag_sub = tag_p.add_subparsers(dest="tag_action")
    tag_add_p = tag_sub.add_parser("add", help="Add a tag to a lead")
    tag_add_p.add_argument("--workspace", required=True)
    tag_add_p.add_argument("--lead-id", type=int, required=True)
    tag_add_p.add_argument("--tag", required=True)
    tag_rm_p = tag_sub.add_parser("remove", help="Remove a tag from a lead")
    tag_rm_p.add_argument("--workspace", required=True)
    tag_rm_p.add_argument("--lead-id", type=int, required=True)
    tag_rm_p.add_argument("--tag", required=True)
    tag_set_p = tag_sub.add_parser("set", help="Replace all tags for a lead")
    tag_set_p.add_argument("--workspace", required=True)
    tag_set_p.add_argument("--lead-id", type=int, required=True)
    tag_set_p.add_argument("--tags", required=True, help="Comma-separated tags")
    tag_list_p = tag_sub.add_parser("list", help="List tags in a workspace")
    tag_list_p.add_argument("--workspace", required=True)
    tag_list_p.add_argument("--lead-id", type=int, help="Optional: filter to one lead")
    tag_bulk_p = tag_sub.add_parser("bulk", help="Add/remove tags across multiple leads")
    tag_bulk_p.add_argument("--workspace", required=True)
    tag_bulk_p.add_argument("--lead-ids", required=True, help="Comma-separated lead IDs")
    tag_bulk_p.add_argument("--tags", required=True, help="Comma-separated tags")
    tag_bulk_p.add_argument("--remove", action="store_true", help="Remove instead of add")
    tag_repair_p = tag_sub.add_parser(
        "repair",
        help="Fix malformed workspace tags (e.g. \"['nace']\" -> nace)",
    )
    tag_repair_p.add_argument("--dry-run", action="store_true", help="Preview fixes without writing")

    ver_p = sub.add_parser("verify-email", help="Record email verification result")
    ver_p.add_argument("--lead-id", type=int, help="Lead ID (single mode)")
    ver_p.add_argument("--status", help="Verification status (valid, invalid, catch-all, unknown, risky, etc.)")
    ver_p.add_argument("--source", help="Verification source (zerobounce, neverbounce, etc.)")
    ver_p.add_argument("--sub-status", dest="sub_status", help="Sub-status detail")
    ver_p.add_argument("--source-detail", dest="source_detail")
    ver_p.add_argument("--smtp-provider", dest="smtp_provider")
    ver_p.add_argument("--batch", action="store_true", help="Read JSON array from --json or --file")
    ver_p.add_argument("--json", dest="json_input", help="JSON array for batch mode")
    ver_p.add_argument("--file", help="JSON file path for batch mode")

    vers_p = sub.add_parser("verify-status", help="Check verification status for a lead")
    vers_p.add_argument("--lead-id", type=int)
    vers_p.add_argument("--email")

    verp_p = sub.add_parser("verify-pending", help="List leads needing email verification")
    verp_p.add_argument("--limit", type=int, default=50)
    verp_p.add_argument("--json", action="store_true")

    vc_p = sub.add_parser(
        "verification-candidates",
        help="List workspace leads due for MillionVerifier (or similar) re-check",
    )
    vc_p.add_argument("--workspace", required=True)
    vc_p.add_argument("--max-age", type=int, default=30, dest="max_age")
    vc_p.add_argument("--skip-mv-days", type=int, default=7, dest="skip_mv_days")
    vc_p.add_argument("--limit", type=int, default=5000)
    vc_p.add_argument(
        "--never-contacted",
        action="store_true",
        help="Exclude leads with any outreach activity",
    )
    vc_p.add_argument(
        "--include-mv-attempted",
        action="store_true",
        help="Include leads already tagged mv_attempted",
    )

    bounce_p = sub.add_parser("bounce-list", help="List deduplicated platform bounce records")
    bounce_p.add_argument("--platform", help="Filter by platform (plusvibe, smartlead, etc.)")
    bounce_p.add_argument("--bounce-type", dest="bounce_type", choices=("hard", "soft", "unknown"))
    bounce_p.add_argument("--sender", help="Filter by sending mailbox")
    bounce_p.add_argument("--since", help="Last seen on/after date (YYYY-MM-DD or today)")
    bounce_p.add_argument("--limit", type=int, default=100)
    bounce_p.add_argument("--json", action="store_true")

    bounce_stats_p = sub.add_parser("bounce-stats", help="Deliverability bounce analytics summary")
    bounce_stats_p.add_argument("--since", help="Last seen on/after date (YYYY-MM-DD or today)")
    bounce_stats_p.add_argument("--json", action="store_true")

    export_p = sub.add_parser(
        "export",
        help="Export leads to local CSV or JSON (use sheets export for Google Sheets)",
    )
    export_p.add_argument("--workspace", required=True, help="Workspace slug")
    export_p.add_argument("--tag", help="Filter by workspace tag")
    export_p.add_argument("--stage", help="Filter by workspace stage")
    export_p.add_argument("--since", help="Created/updated on or after date (YYYY-MM-DD or today)")
    export_p.add_argument("--limit", type=int, default=5000)
    export_p.add_argument(
        "--format",
        choices=("csv", "json", "sheets"),
        default="csv",
        help="csv/json write local files; sheets opens a hosted Google Sheet (see: pipeline.py sheets export)",
    )
    export_p.add_argument("--file", help="Output path under outreachmagic/exports/ (default auto-named)")
    export_p.add_argument(
        "--never-contacted",
        action="store_true",
        help="Only leads with no email/LinkedIn sends, replies, or events",
    )
    export_p.add_argument("--no-email", action="store_true", help="Only leads missing email")
    export_p.add_argument(
        "--require-domain",
        action="store_true",
        help="Only leads with companies.domain set (never fall back to company name)",
    )

    efc_p = sub.add_parser(
        "email-finding-candidates",
        help="List workspace leads shaped for email-finding (real domains only)",
    )
    efc_p.add_argument("--workspace", required=True)
    efc_p.add_argument("--tag")
    efc_p.add_argument("--stage")
    efc_p.add_argument("--since")
    efc_p.add_argument("--limit", type=int, default=5000)
    efc_p.add_argument("--never-contacted", action="store_true")
    efc_p.add_argument("--no-email", action="store_true", default=True)
    efc_p.add_argument("--require-domain", action="store_true", default=True)
    efc_p.add_argument(
        "--lead-ids",
        help="Comma-separated lead ids to scope candidates (e.g. from a CSV batch)",
    )
    efc_p.add_argument(
        "--file",
        help="JSON batch file; lead_id from each row scopes candidates",
    )

    agent_export_p = sub.add_parser("agent-changes", help="Show agent-created leads and events not yet synced")
    agent_export_p.add_argument("--json", action="store_true", help="Output as JSON (default)")
    agent_export_p.add_argument("--file", help="Write CSV to file (import-profiles compatible)")
    agent_export_p.add_argument("--all", action="store_true", help="Include all leads, not just locally-created")
    agent_export_p.add_argument("--workspace", help="Filter export to a specific workspace")

    up_p = sub.add_parser("update-stage", help="Update lead stage")
    up_p.add_argument("--id", type=int, required=True); up_p.add_argument("--stage", required=True)
    up_p.add_argument("--next-action")
    up_p.add_argument("--sentiment", choices=["positive", "negative", "autoreply", "invalid"],
                      help="Qualitative sentiment for this stage change")
    up_p.add_argument("--label", help="Human-readable status label (e.g. 'not interested', 'meeting booked')")
    up_p.add_argument("--workspace", help="Workspace for this stage change (required in multi-workspace mode)")
    up_p.add_argument("--crm-sync", action="store_true", help="Trigger CRM sync after stage update")

    log_p = sub.add_parser("log-event", help="Log an outreach event")
    log_p.add_argument("--lead-id", type=int, required=True)
    log_p.add_argument("--type", dest="event_type", required=True)
    log_p.add_argument("--direction", default="outbound"); log_p.add_argument("--channel", default="email")
    log_p.add_argument("--subject"); log_p.add_argument("--body")
    log_p.add_argument("--metadata", help='JSON metadata string e.g. \'{"lead_status_raw":"not_interested","lead_status_sentiment":"negative"}\'')
    log_p.add_argument("--workspace", help="Workspace for this event (required in multi-workspace mode)")
    log_p.add_argument("--crm-sync", action="store_true", help="Trigger CRM sync after logging event")

    # ── Setup & relay commands ──
    login_p = sub.add_parser("login", help="Connect this machine via browser (device authorization)")
    login_p.add_argument(
        "--platform",
        choices=["hermes", "cursor", "claude-code"],
        help="Host app (auto-detected from skill install path if omitted)",
    )
    login_p.add_argument("--generate-url", action="store_true", help="Generate device URL/code and exit")
    login_p.add_argument("--claim-token", action="store_true", help="Claim token for an existing device code")
    login_p.add_argument("--device-code", help="Device code returned from --generate-url")
    login_p.add_argument(
        "--wait",
        type=int,
        default=30,
        help="Seconds to wait while polling in --claim-token mode (0 = single attempt)",
    )
    login_p.add_argument(
        "--force",
        action="store_true",
        help="Run browser device login even when a valid agent key is already configured",
    )
    sub.add_parser("logout", help="Clear local agent credentials")

    sync_secrets_p = sub.add_parser(
        "sync-secrets",
        help="Sync org API keys from dashboard vault to local agent_secrets.env",
    )
    sync_secrets_p.add_argument("--check", action="store_true", help="Report local key status only (no network)")
    sync_secrets_p.add_argument("--json", action="store_true", help="Emit JSON")
    sync_secrets_p.add_argument("--cron", action="store_true", help=argparse.SUPPRESS)

    api_keys_p = sub.add_parser(
        "api-keys",
        help="Show runtime API key status (last use, errors, failover)",
    )
    api_keys_p.add_argument("--json", action="store_true", help="Emit JSON")
    api_keys_p.add_argument(
        "--push",
        action="store_true",
        help="Report runtime status to dashboard (no secret values)",
    )

    pull_p = sub.add_parser("pull", help="Pull events from relay to local database")
    pull_p.add_argument("--cron", action="store_true", help="Silent mode for cron")
    pull_p.add_argument("--full", action="store_true", help="Re-import all webhook events (after DB reset)")
    pull_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt for pull --full",
    )
    pull_p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print pull cursor and dedupe diagnostics",
    )
    pull_p.add_argument(
        "--debug-sentiment",
        action="store_true",
        help="Print raw vs normalized sentiment mapping during ingest",
    )
    pull_p.add_argument(
        "--skip-routing-sync",
        action="store_true",
        help="Skip cloud routing config sync (events pull only; use if routing API times out)",
    )
    pull_p.add_argument(
        "--probe",
        action="store_true",
        help="Read-only backlog check (limit=1/relay stream, no ingest)",
    )
    pull_p.add_argument(
        "--kind",
        metavar="KINDS",
        help="Comma-separated streams: events,core,workspace (default: all)",
    )
    pull_p.add_argument(
        "--skip-snapshots",
        action="store_true",
        help="Only pull webhook events (skip lead/workspace snapshots)",
    )
    pull_p.add_argument(
        "--reset-snapshot-cursors",
        action="store_true",
        help="Zero core/workspace snapshot cursors before pull (fix desync after hung partial pulls)",
    )
    pull_p.add_argument(
        "--if-stale",
        metavar="DURATION",
        help="Skip pull when last_pull is within DURATION (e.g. 5m, 1h)",
    )
    pull_p.add_argument(
        "--force",
        action="store_true",
        help="Always pull from relay (ignore --if-stale)",
    )

    refresh_p = sub.add_parser(
        "refresh",
        help="DANGER: sync, backup, staging pull --full, then swap DB (rare; keeps old DB until pull succeeds)",
    )
    refresh_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive refresh after reading the warning",
    )
    refresh_p.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip pre-refresh sync (not recommended — may lose unsynced local data)",
    )
    refresh_p.add_argument(
        "--backup",
        help="Backup path for the current database (default: outreachmagic.db.backup-<timestamp>.db)",
    )

    restore_p = sub.add_parser(
        "restore",
        help="Restore local database from a refresh backup",
    )
    restore_p.add_argument(
        "--list",
        action="store_true",
        help="List available backup files (newest first)",
    )
    restore_p.add_argument(
        "--latest",
        action="store_true",
        help="Restore the newest backup in the databases folder",
    )
    restore_p.add_argument(
        "--from",
        dest="from_path",
        help="Path to a specific backup .db file",
    )
    restore_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore (replaces the live database)",
    )

    status_p = sub.add_parser("status", help="Dashboard-style status: plan, connections, usage, routing")
    status_p.add_argument("--json", action="store_true", help="JSON output for agents")

    whoami_p = sub.add_parser("whoami", help="Show connected account email, org, and plan")
    whoami_p.add_argument("--json", action="store_true", help="JSON output for agents")
    sync_p = sub.add_parser("sync", help="Push pending workspaces and routing rules to the webapp")
    sync_p.add_argument("--status", action="store_true", help="Show what needs syncing without pushing")
    sync_p.add_argument("--json", action="store_true", help="JSON-only output (use with --status)")
    sync_p.add_argument(
        "--inspect",
        metavar="EMAIL",
        help="Compare local activity vs sync payload for one lead (requires --workspace)",
    )
    sync_p.add_argument(
        "--workspace",
        help="Scope push/status to a single workspace slug (default: all workspaces)",
    )
    sync_p.add_argument(
        "--no-health-report",
        action="store_true",
        help="Skip aggregate local DB health POST to portal (lead sync still runs)",
    )
    sync_p.add_argument(
        "--full-snapshot-v2",
        action="store_true",
        help="Mark all leads and workspace memberships pending, then push snapshot v2 to relay",
    )
    sync_p.add_argument(
        "--bulk",
        action="store_true",
        help="Force large snapshot batch sizes regardless of pending count",
    )
    sync_p.add_argument(
        "--no-bulk",
        action="store_true",
        help="Force routine (smaller) snapshot batch sizes",
    )

    activity_p = sub.add_parser("activity", help="Lead activity summary (last contacted, counts)")
    activity_sub = activity_p.add_subparsers(dest="activity_command", required=True)
    activity_show_p = activity_sub.add_parser("show", help="Show stored/computed/sync activity for a lead")
    activity_show_p.add_argument("--lead-id", type=int)
    activity_show_p.add_argument("--email")
    activity_show_p.add_argument("--workspace", help="Workspace slug (recommended)")
    activity_show_p.add_argument("--json", action="store_true", default=True)
    activity_recompute_p = activity_sub.add_parser(
        "recompute", help="Recompute activity from events for a lead"
    )
    activity_recompute_p.add_argument("--lead-id", type=int, required=True)
    activity_recompute_p.add_argument("--workspace", help="Limit to one workspace")
    activity_recompute_p.add_argument("--json", action="store_true", default=True)
    db_health_p = sub.add_parser("db-health", help="Local SQLite health (aggregates only)")
    db_health_p.add_argument("--json", action="store_true", help="Print JSON")
    db_health_p.add_argument("--full", action="store_true", help="Run full integrity_check (slower on large DBs)")
    db_health_p.add_argument("--push", action="store_true", help="POST health to portal (debug)")
    db_health_p.add_argument("--verbose", action="store_true", help="Include internal diagnostics (page counts, table breakdown)")
    archive_p = sub.add_parser("archive", help="Export workspace data to a separate SQLite file")
    archive_p.add_argument("--workspace", required=True, help="Workspace slug")
    archive_p.add_argument("--output", help="Output .db path (required unless --dry-run)")
    archive_p.add_argument("--dry-run", action="store_true", help="Show counts only")
    archive_p.add_argument("--purge", action="store_true", help="Remove exported data from main DB (requires --output)")
    archive_p.add_argument("--vacuum", action="store_true", help="Run VACUUM after --purge")

    crm_sync_p = sub.add_parser(
        "crm-sync",
        help="Push leads to CRM (GHL / HubSpot). Delegates to crm_sync.py.",
    )
    crm_sync_p.add_argument(
        "action",
        nargs="?",
        choices=["sync", "status", "discover"],
        default="status",
        help="Action: sync leads, show status, or discover pipelines (default: status)",
    )
    crm_sync_p.add_argument("--workspace", help="Workspace slug (required for sync/discover)")
    crm_sync_p.add_argument("--all", action="store_true", help="Sync all enabled CRM workspaces")
    crm_sync_p.add_argument("--dry-run", action="store_true", help="Preview without API calls")
    crm_sync_p.add_argument("--lead-id", type=int, help="Sync a single lead by ID")
    crm_sync_p.add_argument("--skip-events", action="store_true", help="Skip event history push")
    crm_sync_p.add_argument("--platform", choices=["ghl", "hubspot"], help="Filter by platform")

    conn_p = sub.add_parser("connections", help="List connected platforms with webhook URLs and stats")
    conn_p.add_argument("--json", action="store_true")

    cp_p = sub.add_parser("connect-platform", help="Generate a webhook URL for a platform")
    cp_p.add_argument("--platform", required=True,
                       help="Platform id (smartlead, instantly, heyreach, plusvibe, emailbison, etc.)")

    dp_p = sub.add_parser("disconnect-platform", help="Delete a platform webhook token (URL stops working)")
    dp_p.add_argument("--platform", required=True,
                       help="Platform id to disconnect")
    dp_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    query_cli.register_query_parser(sub)

    bulk_lookup_p = sub.add_parser(
        "batch-lead-lookup",
        help="Lookup many leads in one DB pass (companion dedup)",
    )
    bulk_lookup_p.add_argument("--json", dest="json_input", help="JSON array of lookup keys")
    bulk_lookup_p.add_argument("--file", help="JSON file path (array of lookup keys)")
    bulk_lookup_p.add_argument("--workspace", help="Workspace slug or name")

    hist_p = sub.add_parser("history", help="Show event history for a lead")
    hist_p.add_argument("--id", type=int, help="Lead ID")
    hist_p.add_argument("--email", help="Find lead by email")
    hist_p.add_argument("--linkedin", help="Find lead by LinkedIn URL or profile slug")
    hist_p.add_argument("--name", help="Find lead by name (partial match)")
    hist_p.add_argument("--workspace", help="Filter lead lookup by workspace name or slug")

    dedup_p = sub.add_parser("dedup", help="Find and batch-merge duplicate leads")
    dedup_sub = dedup_p.add_subparsers(dest="dedup_command", required=True)

    dedup_find_p = dedup_sub.add_parser("find", help="Scan workspace for duplicate leads")
    dedup_find_p.add_argument("--workspace", required=True, help="Workspace slug")
    dedup_find_p.add_argument("--tag", help="Tag filter (supports %% wildcards)")
    dedup_find_p.add_argument("--output", help="Write candidates JSON (default stdout)")
    dedup_find_p.add_argument(
        "--min-confidence",
        choices=pipeline_dedup.CONFIDENCE_ORDER,
        default=pipeline_dedup.MIN_CONFIDENCE_DEFAULT_FIND,
    )

    dedup_merge_p = dedup_sub.add_parser("merge", help="Batch-merge from candidates JSON")
    dedup_merge_p.add_argument("--candidates", required=True, help="JSON from dedup find")
    dedup_merge_p.add_argument("--commit", action="store_true", help="Perform merges (default dry-run)")
    dedup_merge_p.add_argument(
        "--min-confidence",
        choices=pipeline_dedup.CONFIDENCE_ORDER,
        default=pipeline_dedup.MIN_CONFIDENCE_DEFAULT_MERGE,
    )
    dedup_merge_p.add_argument("--reason", default="dedup", help="Reason stored in lead_merges")

    review_p = sub.add_parser(
        "review",
        help="Dedup review and two-way Google Sheet sync workflows (not the same as sheets export)",
    )
    review_sub = review_p.add_subparsers(dest="review_command", required=True)

    review_templates_p = review_sub.add_parser("templates", help="List review templates")
    review_templates_sub = review_templates_p.add_subparsers(dest="templates_command", required=True)
    review_templates_sub.add_parser("list", help="List available templates")

    review_export_p = review_sub.add_parser(
        "export",
        help="Export leads to a Google Sheet via OM API (dedup-review or lead-review)",
    )
    review_export_p.add_argument("--template", default="dedup-review")
    review_export_p.add_argument("--input", help="candidates.json from dedup find (dedup-review)")
    review_export_p.add_argument("--title", default="Outreach Dedup Review")
    review_export_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    review_export_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    review_export_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    review_export_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    review_export_p.add_argument(
        "--parent-sheet-id",
        help="Add as a new tab in an existing spreadsheet (requires --tab-name)",
    )
    review_export_p.add_argument(
        "--tab-name",
        help="Tab name when using --parent-sheet-id (default: stage or title)",
    )
    review_export_p.add_argument("--workspace", help="Workspace slug (lead-review)")
    review_export_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
        help="Column set for lead-review template",
    )
    review_export_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    review_export_p.add_argument("--tag", help="Filter workspace tag (lead-review)")
    review_export_p.add_argument("--stage", help="Filter workspace stage (lead-review)")
    review_export_p.add_argument("--since", help="Created/updated since (lead-review)")
    review_export_p.add_argument("--limit", type=int, default=5000)
    review_export_p.add_argument("--never-contacted", action="store_true")
    review_export_p.add_argument("--no-email", action="store_true")
    review_export_p.add_argument("--require-domain", action="store_true")
    review_export_p.add_argument("--original-source", dest="original_source")
    review_export_p.add_argument("--original-source-detail", dest="original_source_detail")
    review_export_p.add_argument("--latest-source", dest="latest_source")
    review_export_p.add_argument("--latest-source-detail", dest="latest_source_detail")
    review_export_p.add_argument("--industry")
    review_export_p.add_argument("--headcount-min", dest="headcount_min", type=int)
    review_export_p.add_argument("--headcount-max", dest="headcount_max", type=int)
    review_export_p.add_argument("--location-city", dest="location_city")
    review_export_p.add_argument("--location-state", dest="location_state")
    review_export_p.add_argument("--email-domain", dest="email_domain")
    review_export_p.add_argument("--email-verified", dest="email_verification_status")

    review_sync_p = review_sub.add_parser("sync", help="Read sheet approvals and merge locally")
    review_sync_p.add_argument("--sheet-id", required=True)
    review_sync_p.add_argument("--template", default="dedup-review")
    review_sync_p.add_argument("--workspace", help="Workspace slug (required for lead-review sync)")
    review_sync_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        help="Rebuild field_keys for lead-review sync (default: standard)",
    )
    review_sync_p.add_argument("--fields", help="Comma-separated columns used at export (--detail custom)")
    review_sync_p.add_argument("--tag", help="Filter workspace tag (override stored export metadata)")
    review_sync_p.add_argument("--stage", help="Filter workspace stage (override stored export metadata)")
    review_sync_p.add_argument("--since", help="Created/updated since (override stored export metadata)")
    review_sync_p.add_argument("--limit", type=int, help="Max leads for baseline (override stored export metadata)")
    review_sync_p.add_argument("--never-contacted", action="store_true", dest="sync_never_contacted",
                               help="Override stored export: never-contacted filter")
    review_sync_p.add_argument("--no-email", action="store_true", dest="sync_no_email",
                               help="Override stored export: no-email filter")
    review_sync_p.add_argument("--require-domain", action="store_true", dest="sync_require_domain",
                               help="Override stored export: require-domain filter")
    review_sync_p.add_argument("--dry-run", action="store_true", help="Report approved rows only")
    review_sync_p.add_argument("--commit", action="store_true", help="Merge approved rows and write results")

    review_payload_p = review_sub.add_parser(
        "export-payload",
        help="Build lead-review export payload locally (no API call)",
    )
    review_payload_p.add_argument("--workspace", required=True)
    review_payload_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
    )
    review_payload_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    review_payload_p.add_argument("--tag")
    review_payload_p.add_argument("--stage")
    review_payload_p.add_argument("--since")
    review_payload_p.add_argument("--limit", type=int, default=5000)
    review_payload_p.add_argument("--never-contacted", action="store_true")
    review_payload_p.add_argument("--no-email", action="store_true")
    review_payload_p.add_argument("--require-domain", action="store_true")
    review_payload_p.add_argument("--original-source", dest="original_source")
    review_payload_p.add_argument("--original-source-detail", dest="original_source_detail")
    review_payload_p.add_argument("--latest-source", dest="latest_source")
    review_payload_p.add_argument("--latest-source-detail", dest="latest_source_detail")
    review_payload_p.add_argument("--industry")
    review_payload_p.add_argument("--headcount-min", dest="headcount_min", type=int)
    review_payload_p.add_argument("--headcount-max", dest="headcount_max", type=int)
    review_payload_p.add_argument("--location-city", dest="location_city")
    review_payload_p.add_argument("--location-state", dest="location_state")
    review_payload_p.add_argument("--email-domain", dest="email_domain")
    review_payload_p.add_argument("--email-verified", dest="email_verification_status")
    review_payload_p.add_argument("--title", default="Lead Review")

    review_apply_p = review_sub.add_parser(
        "apply-sync",
        help="Apply lead-review sync from local JSON sheet rows (no API call)",
    )
    review_apply_p.add_argument("--workspace", required=True)
    review_apply_p.add_argument("--input", required=True, help="JSON file: array of sheet row dicts")
    review_apply_p.add_argument("--dry-run", action="store_true")
    review_apply_p.add_argument("--commit", action="store_true")

    review_presets_p = review_sub.add_parser("presets", help="List lead-review field presets and groups")
    review_presets_p.add_argument("--template", default="lead-review")

    sheets_p = sub.add_parser(
        "sheets",
        help="Export workspace leads to a hosted Google Sheet (no local Google credentials)",
        description="Export workspace leads to a hosted Google Sheet via Outreach Magic (no local Google credentials).",
    )
    sheets_sub = sheets_p.add_subparsers(dest="sheets_command", required=True)
    sheets_export_p = sheets_sub.add_parser(
        "export",
        help="Create a Google Sheet from workspace leads (no local Google credentials)",
    )
    sheets_export_p.add_argument("--workspace", required=True, help="Workspace slug")
    sheets_export_p.add_argument("--title", help="Sheet title (default: <workspace> leads)")
    sheets_export_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    sheets_export_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    sheets_export_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sheets_export_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    sheets_export_p.add_argument(
        "--parent-sheet-id",
        help="Add as a new tab in an existing spreadsheet (requires --tab-name)",
    )
    sheets_export_p.add_argument(
        "--tab-name",
        help="Tab name when using --parent-sheet-id (default: stage or title)",
    )
    sheets_export_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
    )
    sheets_export_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    sheets_export_p.add_argument("--tag")
    sheets_export_p.add_argument("--stage")
    sheets_export_p.add_argument("--since")
    sheets_export_p.add_argument("--limit", type=int, default=5000)
    sheets_export_p.add_argument("--never-contacted", action="store_true")
    sheets_export_p.add_argument("--no-email", action="store_true")
    sheets_export_p.add_argument("--require-domain", action="store_true")

    sheets_cs_p = sheets_sub.add_parser(
        "campaign-stats",
        help="Export campaign performance data to a multi-sheet Google Workbook",
    )
    sheets_cs_p.add_argument("--workspace", required=True, help="Workspace slug")
    sheets_cs_p.add_argument(
        "--since", default="14d",
        help="Time window: 14d, 30d, 7d, all, or YYYY-MM-DD",
    )
    sheets_cs_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    sheets_cs_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    sheets_cs_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sheets_cs_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    sheets_cs_p.add_argument("--dry-run", action="store_true", help="Print payload without uploading")
    sheets_cs_p.add_argument("--json", action="store_true", help="Print payload as JSON (implies --dry-run)")
    sheets_cs_p.add_argument(
        "--update",
        action="store_true",
        help="Reuse the last sheet for this workspace instead of creating a new one. Stores the sheet ID at sheets/<workspace>-campaign-stats.latest.json for cron.",
    )

    merge_p = sub.add_parser("merge-leads", help="Merge two lead records into one")
    merge_p.add_argument("--keep", type=int, help="Lead ID to keep")
    merge_p.add_argument("--merge", type=int, help="Lead ID to merge into --keep and delete")
    merge_p.add_argument("--email", help="Keep lead matched by email (with --linkedin)")
    merge_p.add_argument("--linkedin", help="Merge lead matched by LinkedIn into email lead")
    hist_p.add_argument("--limit", type=int, default=50, help="Max events to show")
    hist_p.add_argument("--json", action="store_true")

    copy_p = sub.add_parser(
        "copy-insights",
        help="Show full copy for positive leads and rank best-performing templates",
    )
    copy_p.add_argument(
        "--lead-status",
        default="interested",
        help="Current lead status to treat as positive (default: interested)",
    )
    copy_p.add_argument("--limit", type=int, default=200, help="Max positive leads to include")
    copy_p.add_argument("--workspace", help="Filter by workspace name or slug")
    copy_p.add_argument("--json", action="store_true")

    segment_p = sub.add_parser(
        "segment-insights",
        help="Rank best converting title/industry/headcount segments from positive leads",
    )
    segment_p.add_argument(
        "--positive-lead-status",
        default="interested",
        help="Current lead status to treat as positive (default: interested)",
    )
    segment_p.add_argument(
        "--positive-sentiment",
        choices=("positive", "negative", "autoreply", "invalid"),
        help="Optional sentiment to combine with --positive-lead-status",
    )
    segment_p.add_argument(
        "--fields",
        default="title,industry,headcount",
        help="Comma-separated segment fields (title,industry,headcount)",
    )
    segment_p.add_argument("--min-sent", type=int, default=2, help="Minimum sent leads per value")
    segment_p.add_argument("--top", type=int, default=12, help="Top values per field")
    segment_p.add_argument("--workspace", help="Filter by workspace name or slug")
    segment_p.add_argument("--json", action="store_true")

    ws_p = sub.add_parser("workspace", help="List or create workspaces")
    ws_sub = ws_p.add_subparsers(dest="workspace_cmd")
    ws_list = ws_sub.add_parser("list", help="List workspaces")
    ws_list.add_argument("--json", action="store_true", help="JSON output for agents")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("--name", required=True)
    ws_create.add_argument("--slug")
    ws_create.add_argument("--sync", action="store_true", help="Sync to webapp immediately")
    ws_sub.add_parser("sync", help="Sync all local workspaces to the webapp")
    ws_routing = ws_sub.add_parser("routing", help="Single vs multi-workspace routing mode")
    ws_routing_sub = ws_routing.add_subparsers(dest="workspace_routing_cmd")
    ws_routing_sub.add_parser("show", help="Show current routing mode")
    ws_routing_set = ws_routing_sub.add_parser("set", help="Set routing mode")
    ws_routing_set.add_argument(
        "--mode",
        required=True,
        choices=VALID_WORKSPACE_ROUTING_MODES,
        help="single: all events to one workspace; multi: require campaign maps",
    )
    ws_routing_set.add_argument(
        "--workspace",
        help="Workspace slug (required for single mode)",
    )
    ws_summary = ws_sub.add_parser(
        "summary",
        help="Workspace inventory: lead count, tags, LinkedIn connection counts by sender",
    )
    ws_summary.add_argument("--workspace", required=True, help="Workspace slug or name")
    ws_summary.add_argument("--json", action="store_true", help="JSON output (recommended for agents)")
    ws_summary.add_argument(
        "--tags-only",
        action="store_true",
        help="Skip LinkedIn sender aggregates (faster on large workspaces)",
    )

    cmap_p = sub.add_parser("campaign-map", help="Campaign to workspace routing")
    cmap_sub = cmap_p.add_subparsers(dest="campaign_map_cmd")
    cmap_sub.add_parser("list", help="List campaign maps")
    cmap_add = cmap_sub.add_parser("add", help="Add campaign map")
    cmap_add.add_argument("--platform", default="*")
    cmap_add.add_argument("--workspace", required=True, help="Workspace slug")
    cmap_add.add_argument("--campaign-platform-id", help="Platform campaign ID (e.g. Smartlead numeric ID, Prosp UUID)")
    cmap_add.add_argument("--campaign-name")
    cmap_add.add_argument(
        "--match-strategy",
        choices=("id_exact", "name_exact", "rule_contains", "rule_prefix", "rule_regex"),
    )
    cmap_add.add_argument("--priority", type=int, default=100)

    q_p = sub.add_parser("quarantine", help="Unmapped campaign queue")
    q_sub = q_p.add_subparsers(dest="quarantine_cmd")
    q_list = q_sub.add_parser("list", help="List quarantined events")
    q_list.add_argument(
        "--status",
        default="pending",
        choices=("pending", "skipped", "assigned", "replayed", "all"),
        help="Filter by queue status (default: pending)",
    )
    q_list.add_argument("--limit", type=int, default=0, help="Limit rows in JSON mode (0 = all)")
    q_list.add_argument("--json", action="store_true", help="Output raw queue rows as JSON")
    q_skip = q_sub.add_parser("skip", help="Skip quarantined event(s) (syncs to relay on next sync)")
    q_skip.add_argument("--id", help="Single queue item id")
    q_skip.add_argument("--campaign-platform-id", help="Skip all pending rows for this campaign platform id")
    q_skip.add_argument("--platform", help="With --campaign-platform-id, filter by source platform")
    q_skip.add_argument(
        "--all",
        action="store_true",
        help="Skip all pending quarantine rows",
    )
    q_skip.add_argument(
        "--reason",
        metavar="REASON",
        help="Skip all pending rows with this reason (e.g. no_campaign_id); run sync after",
    )
    q_backfill = q_sub.add_parser(
        "backfill-no-campaign",
        help="Move legacy events with no campaign into quarantine (skipped by default)",
    )
    q_backfill.add_argument(
        "--keep-pending",
        action="store_true",
        help="Add to quarantine as pending instead of auto-skipping",
    )
    q_assign = q_sub.add_parser("assign", help="Assign workspace (syncs to relay; ingested on next pull)")
    q_assign.add_argument("--id", required=True, help="Queue item id")
    q_assign.add_argument("--workspace", required=True, help="Workspace slug")
    q_replay = q_sub.add_parser("replay", help="Replay pending items locally after campaign-map rules")
    q_replay.add_argument("--workspace")
    q_replay.add_argument("--limit", type=int, default=100)

    reprocess_p = sub.add_parser("reprocess", help="Re-apply current extractors to ingested data")
    reprocess_p.add_argument(
        "--kind", nargs="*",
        choices=("events", "core", "workspace", "company", "all"),
        default=["events"],
        help="Data kinds to reprocess (default: events; use 'all' for everything)",
    )
    reprocess_p.add_argument("--from", dest="from_id", type=int, default=None,
                             help="Start relay/snapshot ID (default: 0)")
    reprocess_p.add_argument("--to", dest="to_id", type=int, default=None,
                             help="End relay/snapshot ID (default: cursor value for kind)")
    reprocess_p.add_argument("--platform", default=None,
                             help="Platform filter (single value, e.g. prosp)")
    reprocess_p.add_argument("--dry-run", action="store_true",
                             help="Print plan and affected row count, don't execute")
    reprocess_p.add_argument("--verbose", action="store_true",
                             help="Print per-row diff of changed fields")
    reprocess_p.add_argument("--force", action="store_true",
                             help="Skip confirmation prompt")
    reprocess_p.add_argument("--reingest", action="store_true",
                             help="Delete and re-ingest events from relay replay (refreshes all data in ID range)")

    pset = sub.add_parser("personalize-set", help="Write lead personalization (first_name, etc.)")
    pset.add_argument("--lead-id", type=int, help="Lead ID (single mode)")
    pset.add_argument("--field", help="Field name (single mode)")
    pset.add_argument("--value", help="Field value (single mode)")
    pset.add_argument("--date", help="Optional ISO date for date-aware fields")
    pset.add_argument("--batch", action="store_true", help="Read JSON array from --json")
    pset.add_argument("--json", dest="json_input", help="JSON array: [{lead_id, field, value, date?}, ...]")

    pget = sub.add_parser("personalize-get", help="Read merged personalization for a lead")
    pget.add_argument("--lead-id", type=int, required=True)
    pget.add_argument("--layer", choices=("merged", "lead", "company"), default="merged")
    pget.add_argument("--json", action="store_true")

    ppend = sub.add_parser("personalize-pending", help="List leads missing lead-scoped fields")
    ppend.add_argument("--fields", default="first_name", help="Comma-separated field names")
    ppend.add_argument("--limit", type=int, default=50)
    ppend.add_argument("--json", action="store_true")

    pstat = sub.add_parser("personalize-status", help="Lead personalization summary")
    pstat.add_argument("--json", action="store_true")

    cpset = sub.add_parser("company-personalize-set", help="Write company personalization (company_name, company_*)")
    cpset.add_argument("--company-id", type=int)
    cpset.add_argument("--domain")
    cpset.add_argument("--name", help="Company name lookup")
    cpset.add_argument("--field")
    cpset.add_argument("--value")
    cpset.add_argument("--date", help="Optional ISO date")
    cpset.add_argument("--batch", action="store_true")
    cpset.add_argument("--json", dest="json_input", help="JSON: [{company_id|domain|name, field, value, date?}]")

    cpget = sub.add_parser("company-personalize-get", help="Read company personalization")
    cpget.add_argument("--company-id", type=int)
    cpget.add_argument("--domain")
    cpget.add_argument("--name")
    cpget.add_argument("--json", action="store_true")

    cppend = sub.add_parser("company-personalize-pending", help="List companies missing company fields")
    cppend.add_argument("--fields", default="company_name", help="Comma-separated field names")
    cppend.add_argument("--limit", type=int, default=50)
    cppend.add_argument("--json", action="store_true")

    cpstat = sub.add_parser("company-personalize-status", help="Company personalization summary")
    cpstat.add_argument("--json", action="store_true")

    pclear = sub.add_parser("personalize-clear", help="Clear personalization data")
    pclear.add_argument("--lead-id", type=int, help="Clear one lead")
    pclear.add_argument("--field", help="Clear specific field across all leads")
    pclear.add_argument("--all", dest="clear_all", action="store_true", help="Clear everything")

    cleanup_rules_p = sub.add_parser("cleanup-rules", help="Remove invalid campaign mapping rules")
    cleanup_rules_p.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    cleanup_rules_p.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "export" and getattr(args, "format", None) == "sheets":
        _remap_to_lead_review_export(args)
    elif args.command == "sheets" and getattr(args, "sheets_command", None) == "export":
        _remap_to_lead_review_export(args)

    # Load synced API keys (primary + backup __N slots) before any command that may call vendors.
    if args.command not in (None, "update", "version"):
        try:
            agent_secrets_cloud.load_local_agent_secrets_to_environ()
        except OSError:
            pass

    # Check-only update notice (never downloads). At most once per hour.
    if args.command not in (None, "update", "version"):
        notify_update_available(quiet=getattr(args, "cron", False))

    if args.command == "version":
        print(f"outreachmagic {__version__}")
        _warn_duplicate_installs()
        return

    if args.command == "paths":
        payload: dict = {
            "install_dir": str(get_install_dir()),
            "data_root": str(get_data_root()),
            "skill_home": str(get_skill_home()),
            "database": str(get_db_path()),
            "config": str(get_config_path()),
            "agent_secrets": str(get_agent_secrets_path()),
            "cwd": str(Path.cwd()),
            **working_paths_payload(),
        }
        warn = hermes_profile_copy_warning()
        if warn:
            payload["warning"] = warn
        print(json.dumps(payload, indent=2))
        if warn:
            print(f"\n⚠ {warn}", file=sys.stderr)
        _warn_duplicate_installs()
        return

    if args.command == "update":
        _warn_duplicate_installs()
        if args.check:
            if not check_skill_update(quiet=False):
                sys.exit(1)
            print(f"Up to date ({__version__})")
            return
        try:
            result = update_skill(
                explicit_tag=args.tag,
                channel=getattr(args, "channel", "release"),
            )
            print(f"Updated to v{result['version']} from {result['source']} in {result['path']}")
            print("Files:", ", ".join(result["files"]))
        except urllib.error.HTTPError as e:
            msg = str(e)
            if e.code == 404:
                tag_hint = ""
                if not args.tag:
                    tag_hint = (
                        "\n\nTry a specific tag: pipeline.py update --tag v<VERSION>\n"
                        "  e.g. pipeline.py update --tag latest-tag"
                    )
                print(
                    f"Update failed: {msg}\n\n"
                    f"The update URL returned 404. This may mean:\n"
                    f"  1. The release tag does not exist or was removed\n"
                    f"  2. GitHub raw content URLs are temporarily unavailable\n"
                    f"  3. Your install config points to a repo that has no matching release{tag_hint}\n"
                    f"  Or try: pipeline.py update --channel main (install from the main branch)",
                    flush=True,
                )
            else:
                print(f"Update failed: {msg}", flush=True)
            sys.exit(1)
        except Exception as e:
            msg = str(e)
            print(f"Update failed: {msg}", flush=True)
            sys.exit(1)
        return

    if args.command == "rollback":
        result = rollback_skill()
        if result.get("status") != "rolled_back":
            print(json.dumps(result, indent=2))
            sys.exit(1)
        print(json.dumps(result, indent=2))
        return

    if args.command == "init":
        cfg = load_config()
        agent_choice = getattr(args, "agent", None)

        # If data_root is not yet configured and duplicates exist, ask interactively.
        if not agent_choice and not cfg.get("data_root"):
            duplicates = check_duplicate_installs()
            if duplicates:
                print(
                    "Outreach Magic is installed in multiple agent directories.",
                    file=sys.stderr,
                )
                print(
                    "Which agent are you using? Pick the one you run with this skill.",
                    file=sys.stderr,
                )
                print(file=sys.stderr)
                for i, name in enumerate(AGENT_DIR_NAMES, 1):
                    path = Path(AGENT_DIR_MAP[name]).expanduser()
                    installed = " ✓" if (path / "skills" / SKILL_NAME).exists() else ""
                    print(f"  {i}) {name}{installed}", file=sys.stderr)
                print(file=sys.stderr)
                prompt = f"Enter a number or name (1-{len(AGENT_DIR_NAMES)}): "
                while True:
                    try:
                        raw = input(prompt).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print(file=sys.stderr)
                        sys.exit(1)
                    if raw in AGENT_DIR_MAP:
                        agent_choice = raw
                        break
                    if raw.isdigit() and 1 <= int(raw) <= len(AGENT_DIR_NAMES):
                        agent_choice = AGENT_DIR_NAMES[int(raw) - 1]
                        break
                    print(
                        f"Please enter 1-{len(AGENT_DIR_NAMES)} or a name "
                        f"({', '.join(AGENT_DIR_NAMES)})",
                    )

        if agent_choice:
            data_root = Path(AGENT_DIR_MAP[agent_choice]).expanduser()
            cfg["data_root"] = str(data_root)
            save_config(cfg)
            # Redirect all path resolution in this process so init_db() and
            # get_db_path() use the newly-configured canonical root — not the
            # script-location-inferred root that get_data_root() still caches.
            set_data_root_override(data_root)
            print(f"✓ data_root set to {data_root}")
            print(f"  All skill copies will now use the same database and config.")
        else:
            # data_root already configured or no duplicates — use whatever is resolved
            print(f"Using existing data_root: {get_data_root()}")

        # Detect duplicates that have their own DB and suggest symlinking.
        duplicates = check_duplicate_installs()
        if duplicates:
            active = get_install_dir()
            for dup in duplicates:
                other_db = Path(dup["path"]) / "databases" / "outreachmagic.db"
                if other_db.exists() and not dup["is_symlink"]:
                    print(
                        f"Note: {other_db} exists in a separate copy at {dup['path']}.",
                        file=sys.stderr,
                    )
                    print(
                        f"  To share one database, symlink instead:\n"
                        f"  rm -rf '{dup['path']}' && ln -s '{active}' '{dup['path']}'",
                        file=sys.stderr,
                    )
        init_db()
        set_last_sync(datetime.now(timezone.utc).isoformat())
        from_tag = getattr(args, "from_tag", None)
        if from_tag:
            record_install_source(from_tag)
        print(f"Outreach Magic v{__version__} installed.")
        print(f"Database initialized: {get_db_path()}")
        paths = working_paths_payload()
        print(f"Working files: {paths['working_root']}")
        for key in ("imports", "exports", "batches", "sheets", "archive", "logs"):
            print(f"  {key:16} → {paths[key]}")
        print()
        print("Next: ask Outreach Magic to connect (login step).")
        return

    # Commands that only talk to the app API (no local DB required)
    if args.command == "status":
        cmd_status(json_output=getattr(args, "json", False))
        return

    if args.command == "whoami":
        cmd_whoami(json_output=getattr(args, "json", False))
        return

    if args.command == "refresh":
        cmd_refresh(args)
        return

    if args.command == "restore":
        cmd_restore(args)
        return

    if args.command == "login":
        _warn_duplicate_installs()
        login(
            platform=getattr(args, "platform", None),
            generate_url=getattr(args, "generate_url", False),
            claim_token=getattr(args, "claim_token", False),
            device_code=getattr(args, "device_code", None),
            wait_seconds=getattr(args, "wait", 30),
            force=getattr(args, "force", False),
        )
        return
    if args.command == "logout":
        logout()
        return

    if args.command == "sync-secrets":
        result = sync_agent_secrets_cli(
            check_only=getattr(args, "check", False),
            as_json=getattr(args, "json", False),
            quiet=getattr(args, "cron", False),
        )
        if not result.get("ok", True) and getattr(args, "check", False) is False:
            sys.exit(1)
        return

    if args.command == "api-keys":
        api_keys_cli(as_json=getattr(args, "json", False), push=getattr(args, "push", False))
        return

    if args.command not in _DB_OPTIONAL_COMMANDS and not database_has_schema():
        print(format_database_recovery_message(), file=sys.stderr)
        sys.exit(1)

    if args.command == "sync":
        if getattr(args, "inspect", None):
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace is required with sync --inspect"}))
                sys.exit(1)
            email = args.inspect.strip().lower()
            lead = find_lead(email=email)
            if not lead:
                print(json.dumps({"error": f"lead not found: {email}"}))
                sys.exit(1)
            conn = get_conn()
            try:
                result = inspect_sync_lead(
                    conn, DEFAULT_ORG_ID, lead["id"], workspace_slug=args.workspace,
                )
            finally:
                conn.close()
            print(json.dumps(result, indent=2))
            return
        if getattr(args, "status", False):
            status = get_sync_status()
            if getattr(args, "json", False):
                print(json.dumps(status, indent=2))
            else:
                mode = status.get("recommended_mode", "push")
                pending = status.get("leads_pending", 0) + status.get("workspace_leads_pending", 0)
                if pending:
                    print(
                        f"Sync status: {mode} mode recommended — "
                        f"{pending} snapshot(s) pending cloud push",
                        flush=True,
                    )
                print(json.dumps(status, indent=2))
        else:
            sync_ws = getattr(args, "workspace", None)
            if getattr(args, "full_snapshot_v2", False):
                mark_all_lead_snapshots_pending()
                if sync_ws:
                    print(f"Marked all leads pending (--workspace {sync_ws} scopes the push).", flush=True)
                else:
                    print("Marked all leads and workspace memberships for snapshot v2 push.", flush=True)
            force_bulk = None
            if getattr(args, "bulk", False) and getattr(args, "no_bulk", False):
                print(json.dumps({"error": "Use --bulk or --no-bulk, not both"}))
                sys.exit(1)
            if getattr(args, "bulk", False):
                force_bulk = True
            elif getattr(args, "no_bulk", False):
                force_bulk = False
            result = sync_all(
                no_health_report=getattr(args, "no_health_report", False),
                force_bulk=force_bulk,
                workspace=sync_ws,
            )
            print(json.dumps(result, indent=2))
        return

    if args.command == "activity":
        if args.activity_command == "show":
            lead = None
            if getattr(args, "lead_id", None):
                conn = get_conn()
                row = conn.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
                conn.close()
                lead = dict(row) if row else None
            elif getattr(args, "email", None):
                lead = find_lead(email=args.email.strip().lower())
            if not lead:
                print(json.dumps({"error": "lead not found (--lead-id or --email required)"}))
                sys.exit(1)
            conn = get_conn()
            try:
                result = inspect_sync_lead(
                    conn,
                    DEFAULT_ORG_ID,
                    lead["id"],
                    workspace_slug=getattr(args, "workspace", None),
                )
            finally:
                conn.close()
            print(json.dumps(result, indent=2))
            return
        if args.activity_command == "recompute":
            conn = get_conn()
            try:
                ws_slug = getattr(args, "workspace", None)
                ws_id = None
                if ws_slug:
                    ws_row = resolve_workspace_identity(conn, ws_slug)
                    if not ws_row:
                        print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                        sys.exit(1)
                    ws_id = ws_row["id"]
                    merged = refresh_lead_activity_from_events(conn, args.lead_id, ws_id)
                    conn.commit()
                    results = {ws_slug: merged}
                else:
                    rows = conn.execute(
                        "SELECT workspace_id FROM workspace_leads WHERE lead_id = ?",
                        (args.lead_id,),
                    ).fetchall()
                    results = {}
                    for row in rows:
                        merged = refresh_lead_activity_from_events(
                            conn, args.lead_id, row["workspace_id"],
                        )
                        results[row["workspace_id"]] = merged
                    conn.commit()
            finally:
                conn.close()
            print(json.dumps({"status": "ok", "lead_id": args.lead_id, "activity": results}, indent=2))
            return

    if args.command == "db-health":
        conn = get_conn()
        try:
            health = db_health.collect_db_health(
                conn,
                org_id=DEFAULT_ORG_ID,
                fast=not getattr(args, "full", False),
                verbose=getattr(args, "verbose", False),
                pipeline_version=__version__,
            )
        finally:
            conn.close()
        if getattr(args, "push", False):
            conn_push = get_conn()
            try:
                health["cloud"] = db_health.maybe_report_db_health_to_cloud(
                    conn_push,
                    org_id=DEFAULT_ORG_ID,
                    pipeline_version=__version__,
                    get_agent_key_fn=get_agent_key,
                    load_config_fn=load_config,
                    save_config_fn=save_config,
                    get_client_id_fn=get_or_create_client_id,
                    cloud_routing_enabled_fn=routing_cloud.cloud_routing_enabled,
                    get_api_base_fn=routing_cloud.get_api_base,
                    push_db_health_fn=routing_cloud.push_db_health,
                    fast=not getattr(args, "full", False),
                    force=True,
                    skip=False,
                )
            finally:
                conn_push.close()
        out = json.dumps(health, indent=2) if getattr(args, "json", False) or getattr(args, "push", False) else json.dumps(health)
        print(out)
        return

    if args.command == "archive":
        ws = args.workspace
        if args.dry_run:
            conn = get_conn()
            try:
                _ids, meta = workspace_archive.resolve_archive_lead_ids(
                    conn,
                    DEFAULT_ORG_ID,
                    ws,
                    resolve_workspace_identity_fn=resolve_workspace_identity,
                )
                ev_count = 0
                if _ids:
                    ph = ",".join("?" for _ in _ids)
                    ev_count = conn.execute(
                        f"SELECT COUNT(*) FROM events WHERE lead_id IN ({ph})",
                        tuple(_ids),
                    ).fetchone()[0]
                print(
                    json.dumps(
                        {
                            "workspace": ws,
                            "dry_run": True,
                            "lead_count": len(_ids),
                            "event_count": ev_count,
                            **meta,
                        },
                        indent=2,
                    )
                )
            finally:
                conn.close()
            return
        if not args.output:
            print(json.dumps({"error": "--output required (or use --dry-run)"}))
            sys.exit(1)
        out_path = Path(args.output).expanduser()

        def _init_archive_schema(c):
            c.executescript(SCHEMA_SQL)
            migrate_db(c)

        conn = get_conn()
        try:
            manifest = workspace_archive.export_workspace_archive(
                conn,
                DEFAULT_ORG_ID,
                ws,
                out_path,
                resolve_workspace_identity_fn=resolve_workspace_identity,
                init_schema_fn=_init_archive_schema,
            )
            if args.purge:
                purge_result = workspace_archive.purge_workspace_archive(
                    conn,
                    DEFAULT_ORG_ID,
                    ws,
                    resolve_workspace_identity_fn=resolve_workspace_identity,
                    vacuum=getattr(args, "vacuum", False),
                )
                manifest["purge"] = purge_result
        finally:
            conn.close()
        print(json.dumps(manifest, indent=2))
        return

    if args.command == "connections":
        cmd_connections(json_output=getattr(args, "json", False))
        return

    if args.command == "connect-platform":
        cmd_connect_platform(args.platform)
        return

    if args.command == "disconnect-platform":
        cmd_disconnect_platform(args.platform, skip_confirm=getattr(args, "yes", False))
        return

    if not db_exists():
        print("Database not initialized. Ask Outreach Magic to initialize the database.")
        sys.exit(1)

    migrate_db()
    sync_workspace_routing_mode_from_config()

    if args.command == "crm-sync":
        _cmd_crm_sync(args)
        return

    if args.command == "pull":
        _warn_duplicate_installs()
        agent_key = _require_agent_key()
        pull_stats = {}

        if (
            args.full
            and not args.cron
            and not getattr(args, "yes", False)
        ):
            hint = "all webhook events"
            try:
                probe = probe_relay_backlog(agent_key)
                pending = (probe.get("events") or {}).get("pending")
                if pending is not None:
                    hint = f"~{int(pending):,} webhook events"
            except (RuntimeError, ValueError, TypeError):
                pass
            print(
                f"This will replay {hint} from the relay (may take several minutes). "
                "Continue? [Y/n] ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
            if answer not in ("", "y", "yes"):
                print("Aborted.")
                sys.exit(0)

        if_stale = getattr(args, "if_stale", None)
        if getattr(args, "force", False):
            if_stale = None
        if if_stale:
            try:
                skip_payload = pull_if_stale_skip_result(if_stale, force=False)
            except ValueError as e:
                print(f"Pull failed: {e}")
                sys.exit(1)
            if skip_payload:
                if not args.cron:
                    print(json.dumps(skip_payload, indent=2))
                sys.exit(0)

        if getattr(args, "probe", False):
            try:
                print_relay_probe(probe_relay_backlog(agent_key))
            except (RuntimeError, ValueError) as e:
                print(f"Probe failed: {e}")
            return

        try:
            pull_kinds = parse_pull_kinds(getattr(args, "kind", None))
        except ValueError as e:
            print(f"Pull failed: {e}")
            sys.exit(0)

        if getattr(args, "reset_snapshot_cursors", False):
            clear_snapshot_cursors()
            if not args.cron:
                print(
                    "Reset snapshot cursors to 0 (core, workspace). "
                    "Use after a hung pull left config ahead of the local DB.",
                    flush=True,
                )

        try:
            imported, skipped = sync_from_relay_org(
                agent_key,
                after_id=None if args.full else get_last_max_id(),
                full=args.full,
                debug_sentiment=args.debug_sentiment,
                quiet=args.cron,
                stats=pull_stats,
                skip_routing_sync=getattr(args, "skip_routing_sync", False),
                pull_kinds=pull_kinds,
                skip_snapshots=getattr(args, "skip_snapshots", False),
            )
        except RuntimeError as e:
            if not args.cron:
                print(f"Pull failed: {_pull_failure_message(e)}")
            sys.exit(0)

        if args.diagnose and not args.cron:
            print_pull_diagnostics(pull_stats)
            print()

        if imported == 0 and skipped == 0:
            if not args.cron:
                print("No events on relay.")
            sys.exit(0)

        if not args.cron:
            print(format_pull_summary(imported, skipped, pull_stats))
            mode = pull_stats.get("mode", "incremental")
            newest = pull_stats.get("newest_relay_id_seen")
            print(f"[mode={mode}, newest_relay_id={newest or '-'}]")
            if args.full:
                print("Full replay complete.")
            if pull_stats.get("cursor_stalled"):
                print(
                    "Warning: pull cursor stalled on a full relay page; "
                    "investigate relay max_id pagination."
                )
            print("Ask Outreach Magic to show the pipeline.")
        return

    if args.command == "reprocess":
        _warn_duplicate_installs()
        agent_key = _require_agent_key()
        kinds = args.kind if args.kind != ["all"] else ["events", "core", "workspace", "company"]

        if not args.dry_run and not args.force:
            print(
                f"This will reprocess {len(kinds)} kind(s): {', '.join(kinds)}. "
                "Existing metadata will be re-extracted and updated in place. "
                "Continue? [Y/n] ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
            if answer not in ("", "y", "yes"):
                print("Aborted.")
                sys.exit(0)

        import reprocess

        for kind in kinds:
            if kind == "events":
                result = reprocess.reprocess_events(
                    agent_key,
                    from_id=args.from_id or 0,
                    to_id=args.to_id,
                    platform=args.platform,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    reingest=args.reingest,
                )
            else:
                print(
                    f"[reprocess] {kind} snapshots not yet implemented",
                    file=sys.stderr,
                )
                continue

            # ── Summary card ────────────────────────────────────────────────
            fetched = result.get("fetched", 0)
            updated = result.get("updated", 0)
            pages = result.get("pages", 0)
            elapsed = result.get("elapsed_s", 0)
            rate = result.get("rate", 0)
            errors = result.get("errors", 0)
            total_count = result.get("total_count")
            kind_label = kind.upper()

            elapsed_fmt = f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s" if elapsed >= 60 else f"{elapsed:.0f}s"
            dry_tag = " [DRY RUN]" if args.dry_run else ""
            check = "✅" if errors == 0 else "⚠️"
            count_line = f"   Events:     {fetched} fetched"
            if total_count:
                count_line += f" / {total_count} total"
            count_line += f" · {updated} updated"

            print(
                file=sys.stderr,
            )
            print(
                f"  ────────────────────────────────────────{dry_tag}",
                file=sys.stderr,
            )
            print(
                f"  {check} {kind_label} reprocess complete",
                file=sys.stderr,
            )
            print(
                f"   Platform:   {args.platform or 'all'}",
                file=sys.stderr,
            )
            print(
                count_line,
                file=sys.stderr,
            )
            print(
                f"   Pages:      {pages}",
                file=sys.stderr,
            )
            print(
                f"   Duration:   {elapsed_fmt}",
                file=sys.stderr,
            )
            print(
                f"   Rate:       {int(rate)} ev/s",
                file=sys.stderr,
            )
            if errors:
                print(
                    f"   Errors:     {errors} ⚠️",
                    file=sys.stderr,
                )
            else:
                print(
                    f"   Errors:     0",
                    file=sys.stderr,
                )
            print(
                f"  ────────────────────────────────────────",
                file=sys.stderr,
            )

        # Clean up quarantine rows for reprocessed events (only if not dry-run).
        if not args.dry_run:
            cleanup_stale_quarantine_for_reprocessed()
            print("[reprocess] quarantine cleanup complete.", file=sys.stderr)

        return

    pull_before_commands = ("show", "lead-table", "stats", "campaigns", "summary")
    if args.command in pull_before_commands and getattr(args, "pull", False):
        agent_key = get_agent_key()
        if agent_key:
            try:
                skip_payload = pull_if_stale_skip_result(
                    "5m",
                    force=getattr(args, "force_pull", False),
                )
                if skip_payload:
                    print(skip_payload.get("freshness_message", "Pull skipped (fresh)."))
                else:
                    imported, _ = sync_from_relay_org(
                        agent_key,
                        after_id=get_last_max_id(),
                        quiet=True,
                        skip_snapshots=True,
                    )
                    if imported:
                        print(f"Pulled from relay: {imported} new events imported.")
                    else:
                        print("Pulled from relay: 0 new events imported.")
            except (RuntimeError, ValueError):
                pass
        print()

    if args.command == "show":
        query_cli.cmd_pipeline_view(args, table_formatter=format_pipeline_table)
    elif args.command == "lead-table":
        query_cli.cmd_pipeline_view(
            args,
            table_formatter=lambda leads: format_lead_table(
                leads, markdown=getattr(args, "markdown", False)
            ),
        )
    elif args.command == "query":
        query_cli.cmd_query(args)
    elif args.command == "sheets" and getattr(args, "sheets_command", None) == "campaign-stats":
        _cmd_sheets_campaign_stats(args)
    elif args.command == "email-finding-candidates":
        conn = get_conn()
        try:
            lead_ids = None
            if getattr(args, "lead_ids", None):
                lead_ids = [
                    int(x.strip()) for x in str(args.lead_ids).split(",") if x.strip()
                ]
            elif getattr(args, "file", None):
                in_path = resolve_project_path(args.file, kind="input")
                batch_rows = json.loads(in_path.read_text(encoding="utf-8"))
                if not isinstance(batch_rows, list):
                    raise ValueError("--file must be a JSON array of lead rows")
                lead_ids = []
                for row in batch_rows:
                    if not isinstance(row, dict):
                        continue
                    lid = row.get("lead_id") or row.get("id")
                    if lid is not None:
                        lead_ids.append(int(lid))
            scope_leads = pipeline_lead_review.load_workspace_leads_for_review(
                conn,
                args.workspace,
                tag=getattr(args, "tag", None),
                stage=getattr(args, "stage", None),
                since=getattr(args, "since", None),
                limit=args.limit,
                never_contacted=getattr(args, "never_contacted", False),
                no_email=False,
                require_domain=False,
                lead_ids=lead_ids,
                enrich_fn=enrich_lead_rows,
            )
            skipped_has_email = sum(
                1 for lead in scope_leads if (lead.get("email") or "").strip()
            )
            pool = scope_leads
            if getattr(args, "no_email", True):
                pool = [
                    lead for lead in scope_leads if not (lead.get("email") or "").strip()
                ]
            candidates = pipeline_lead_review.email_finder_candidates_from_leads(pool)
            skipped_no_domain = len(pool) - len(candidates)
            print(json.dumps({
                "status": "ok",
                "workspace": args.workspace,
                "scanned": len(scope_leads),
                "skipped_has_email": skipped_has_email,
                "skipped_no_domain": skipped_no_domain,
                "count": len(candidates),
                "candidates": candidates,
            }, indent=2))
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        finally:
            conn.close()
    elif args.command == "export":
        try:
            result = export_leads(
                workspace=args.workspace,
                tag=getattr(args, "tag", None),
                stage=getattr(args, "stage", None),
                since=getattr(args, "since", None),
                limit=args.limit,
                fmt=args.format,
                file_path=getattr(args, "file", None),
                never_contacted=getattr(args, "never_contacted", False),
                no_email=getattr(args, "no_email", False),
                require_domain=getattr(args, "require_domain", False),
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if args.format == "json" and not getattr(args, "file", None):
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
    elif args.command == "summary":
        import read_queries as rq

        digest = rq.daily_digest(
            since=getattr(args, "since", None) or "today",
            workspace=getattr(args, "workspace", None),
            campaign_prefix=getattr(args, "campaign_prefix", None),
        )
        digest = attach_freshness(digest, last_pull=get_last_pull())
        if getattr(args, "json", False):
            print(json.dumps(digest, indent=2))
        else:
            print_freshness_stderr(get_last_pull())
            print(rq.format_daily_digest(digest))
    elif args.command == "stats":
        total_events = get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print_freshness_stderr(get_last_pull(), total_events=total_events)
        stats = attach_freshness(get_stats(), last_pull=get_last_pull())
        print(json.dumps(stats, indent=0) if getattr(args, "json", False) else format_stats(stats))
    elif args.command == "campaigns":
        total_events = get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print_freshness_stderr(get_last_pull(), total_events=total_events)
        stats = attach_freshness(get_campaign_stats(), last_pull=get_last_pull())
        if getattr(args, "json", False):
            print(json.dumps(stats, indent=2))
        else:
            lines = format_campaign_stats(stats, include_header=False)
            print("\n".join(lines) if lines else "No campaign data yet.")
    elif args.command == "platform-map":
        cmd_platform_map(getattr(args, "platform", None))
    elif args.command == "add-lead":
        result = add_lead(name=args.name, company=args.company, title=args.title,
                          industry=args.industry, headcount=args.headcount,
                          email=args.email, linkedin_url=args.linkedin,
                          channel=args.channel, stage=args.stage, notes=args.notes)
        ws_slug = getattr(args, "workspace", None)
        if ws_slug and result.get("id"):
            conn = get_conn()
            ws_row = resolve_workspace_identity(conn, ws_slug)
            if ws_row:
                upsert_workspace_lead(conn, DEFAULT_ORG_ID, ws_row["id"], result["id"],
                                      status=args.stage or "prospecting")
                conn.commit()
                result["workspace"] = ws_row["slug"]
            else:
                result["workspace_error"] = f"workspace not found: {ws_slug}"
            conn.close()
        print(json.dumps(result))
    elif args.command == "import-profiles":
        rows: list[dict] = []
        if args.file and args.json_data:
            print(json.dumps({"error": "Use --file or --json, not both"}))
            sys.exit(1)
        if args.file:
            try:
                path = resolve_project_path(args.file, kind="input")
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if not path.is_file():
                print(json.dumps({"error": f"File not found: {path}"}))
                sys.exit(1)
            try:
                rows = load_profile_rows_from_file(path)
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
        elif args.json_data:
            raw = sys.stdin.read() if args.json_data.strip() == "-" else args.json_data
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON: {e}"}))
                sys.exit(1)
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = [data]
            else:
                print(json.dumps({"error": "JSON must be an array of objects or a single object"}))
                sys.exit(1)
        else:
            print(json.dumps({"error": "Provide --file or --json"}))
            sys.exit(1)
        summary = import_profiles(
            rows,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            channel=args.channel,
            stage=args.stage,
            notes=args.notes,
            workspace=getattr(args, "workspace", None),
            sender_profile=getattr(args, "sender_profile", None),
            source=getattr(args, "source", None),
            source_detail=getattr(args, "source_detail", None),
            import_batch_id=getattr(args, "import_batch_id", None),
            import_format=getattr(args, "import_format", None),
        )
        if (
            not args.dry_run
            and not getattr(args, "no_sync", False)
            and (summary.get("created") or summary.get("matched"))
        ):
            counts = get_local_pending_counts()
            if counts.get("leads_pending") or counts.get("workspace_leads_pending"):
                sync_result = sync_all(no_health_report=True)
                summary["sync"] = sync_result
                if sync_result.get("status") == "ok":
                    summary["sync_hint"] = "Imported leads pushed to relay via pipeline.py sync."
                else:
                    summary["sync_hint"] = (
                        f"Auto-sync failed: {sync_result.get('error', 'unknown')}. "
                        "Run: pipeline.py sync"
                    )
        print(json.dumps(summary, indent=2))
    elif args.command == "apply-email-find-results":
        rows: list[dict] = []
        if args.file and args.json_data:
            print(json.dumps({"error": "Use --file or --json, not both"}))
            sys.exit(1)
        if args.file:
            try:
                path = resolve_project_path(args.file, kind="input")
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if not path.is_file():
                print(json.dumps({"error": f"File not found: {path}"}))
                sys.exit(1)
            try:
                rows = load_profile_rows_from_file(path)
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
        elif args.json_data:
            raw = sys.stdin.read() if args.json_data.strip() == "-" else args.json_data
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON: {e}"}))
                sys.exit(1)
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = [data]
            else:
                print(json.dumps({"error": "JSON must be an array of objects or a single object"}))
                sys.exit(1)
        else:
            print(json.dumps({"error": "Provide --file or --json"}))
            sys.exit(1)
        summary = apply_email_find_results(
            rows,
            workspace=args.workspace,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            source=getattr(args, "source", None),
            source_detail=getattr(args, "source_detail", None),
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "tag":
        action = getattr(args, "tag_action", None)
        if action == "repair":
            if not db_exists():
                print(json.dumps({"error": "Database not initialized. Ask Outreach Magic to initialize."}))
                sys.exit(1)
            migrate_db()
            conn = get_conn()
            try:
                print(json.dumps(repair_malformed_tags(conn, dry_run=getattr(args, "dry_run", False)), indent=2))
            finally:
                conn.close()
            return
        tag_ws = getattr(args, "workspace", None)
        if not tag_ws:
            print(json.dumps({"error": "--workspace required"}))
            sys.exit(1)
        conn = get_conn()
        ws_row = resolve_workspace_identity(conn, tag_ws)
        conn.close()
        if not ws_row:
            print(json.dumps({"error": f"workspace not found: {tag_ws}"}))
            sys.exit(1)
        ws_id = ws_row["id"]
        if action == "add":
            print(json.dumps(tag_add(ws_id, args.lead_id, args.tag)))
        elif action == "remove":
            print(json.dumps(tag_remove(ws_id, args.lead_id, args.tag)))
        elif action == "set":
            tags_list = _parse_cli_tags(args.tags)
            print(json.dumps(tag_set(ws_id, args.lead_id, tags_list)))
        elif action == "list":
            print(json.dumps(tag_list(ws_id, lead_id=getattr(args, "lead_id", None))))
        elif action == "bulk":
            lead_ids = [int(x.strip()) for x in args.lead_ids.split(",") if x.strip()]
            tags_list = _parse_cli_tags(args.tags)
            print(json.dumps(tag_bulk(ws_id, lead_ids, tags_list, remove=getattr(args, "remove", False))))
        else:
            print(json.dumps({"error": "tag subcommand required: add, remove, set, list, bulk, repair"}))
    elif args.command == "verify-email":
        if getattr(args, "batch", False):
            try:
                items = load_json_array_from_cli(
                    json_input=getattr(args, "json_input", None),
                    file_path=getattr(args, "file", None),
                )
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            print(json.dumps(verify_email_batch(items), indent=2))
        else:
            lid = getattr(args, "lead_id", None)
            st = getattr(args, "status", None)
            src = getattr(args, "source", None)
            if not lid or not st or not src:
                print(json.dumps({"error": "--lead-id, --status, and --source required (or use --batch --json)"}))
                sys.exit(1)
            print(json.dumps(verify_email(
                lid, st, src,
                sub_status=getattr(args, "sub_status", None),
                source_detail=getattr(args, "source_detail", None),
                smtp_provider=getattr(args, "smtp_provider", None),
            ), indent=2))
    elif args.command == "verify-status":
        print(json.dumps(verify_status(
            lead_id=getattr(args, "lead_id", None),
            email=getattr(args, "email", None),
        ), indent=2))
    elif args.command == "verify-pending":
        result = verify_pending(limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} leads pending verification:")
            for r in result:
                print(f"  [{r['id']}] {r.get('name') or '?'} — {r.get('email') or ''}")
    elif args.command == "verification-candidates":
        print(json.dumps(
            leads_needing_verification(
                args.workspace,
                max_age_days=getattr(args, "max_age", 30),
                skip_mv_days=getattr(args, "skip_mv_days", 7),
                limit=getattr(args, "limit", 5000),
                never_contacted_only=getattr(args, "never_contacted", False),
                skip_mv_attempted_tag=not getattr(args, "include_mv_attempted", False),
            ),
            indent=2,
        ))
    elif args.command == "bounce-list":
        rows = list_bounce_events(
            platform=getattr(args, "platform", None),
            bounce_type=getattr(args, "bounce_type", None),
            sender=getattr(args, "sender", None),
            since=getattr(args, "since", None),
            limit=args.limit,
        )
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("No bounce records found.")
            else:
                print(f"{'Lead':<28} {'Sender':<28} {'Type':<8} {'MX':<18} {'Seen':<12} {'Msg'}")
                print("-" * 120)
                for row in rows:
                    msg = (row.get("bounce_message") or "")[:60]
                    print(
                        f"{(row.get('lead_email') or '—'):<28} "
                        f"{(row.get('sender_email') or '—'):<28} "
                        f"{(row.get('bounce_type') or '—'):<8} "
                        f"{(row.get('recipient_mx') or '—'):<18} "
                        f"{(row.get('last_seen_at') or '')[:10]:<12} "
                        f"{msg}"
                    )
    elif args.command == "bounce-stats":
        stats = bounce_stats(since=getattr(args, "since", None))
        if getattr(args, "json", False):
            print(json.dumps(stats, indent=2))
        else:
            print(
                f"Unique bounces: {stats['total_unique_bounces']} | "
                f"Suppressed duplicate webhooks: {stats['suppressed_duplicate_webhooks']}"
            )
            if stats["by_platform"]:
                print("By platform: " + ", ".join(
                    f"{r['platform']}={r['c']}" for r in stats["by_platform"]
                ))
            if stats["by_bounce_type"]:
                print("By type: " + ", ".join(
                    f"{r['bounce_type']}={r['c']}" for r in stats["by_bounce_type"]
                ))
    elif args.command == "agent-changes":
        result = export_local_changes(
            all_leads=getattr(args, "all", False),
            workspace=getattr(args, "workspace", None),
        )
        if getattr(args, "file", None):
            write_export_csv(result, args.file)
        else:
            print(json.dumps(result, indent=2))
    elif args.command == "update-stage":
        ws_slug = getattr(args, "workspace", None)
        conn = get_conn()
        routing_config = get_org_routing_config(conn, DEFAULT_ORG_ID)
        ws_row = None
        if routing_config.mode == WORKSPACE_ROUTING_MULTI:
            if not ws_slug:
                conn.close()
                print(json.dumps({"error": "Multi-workspace mode: --workspace is required for update-stage"}))
                sys.exit(1)
            ws_row = resolve_workspace_identity(conn, ws_slug)
            if not ws_row:
                conn.close()
                print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                sys.exit(1)
        elif ws_slug:
            ws_row = resolve_workspace_identity(conn, ws_slug)
        conn.close()

        update_lead_stage(args.id, args.stage, args.next_action)

        result = {"status": "updated", "id": args.id, "stage": args.stage}
        if ws_row:
            conn = get_conn()
            ws_lead_id = upsert_workspace_lead(
                conn, DEFAULT_ORG_ID, ws_row["id"], args.id, status=args.stage,
                current_status_label=getattr(args, "label", None),
                current_status_sentiment=getattr(args, "sentiment", None))
            stage_ts = datetime.now(timezone.utc).isoformat()
            update_sets = ["status = ?", "stage_entered_at = ?"]
            update_params = [args.stage, stage_ts]
            sentiment_val = getattr(args, "sentiment", None)
            label_val = getattr(args, "label", None)
            if sentiment_val:
                update_sets.append("current_status_sentiment = ?")
                update_params.append(sentiment_val)
            if label_val:
                update_sets.append("current_status_label = ?")
                update_params.append(label_val)
            update_params.append(ws_lead_id)
            conn.execute(
                f"UPDATE workspace_leads SET {', '.join(update_sets)} WHERE id = ?",
                update_params)
            conn.commit()
            conn.close()
            result["workspace"] = ws_row["slug"]

        # Log an event for the stage change
        event_metadata = {
            "lead_status_raw": args.stage,
            "lead_status_display": args.stage.replace("_", " "),
        }
        if getattr(args, "sentiment", None):
            event_metadata["lead_status_sentiment"] = args.sentiment
        if getattr(args, "label", None):
            event_metadata["lead_status_display"] = args.label
        log_event(
            lead_id=args.id,
            event_type="lead_status_updated",
            direction="inbound",
            metadata=event_metadata,
        )

        print(json.dumps(result))
        if getattr(args, "crm_sync", False) and ws_slug:
            _maybe_trigger_crm_sync(lead_id=args.id, stage=args.stage, workspace_slug=ws_slug)
    elif args.command == "log-event":
        ws_slug = getattr(args, "workspace", None)
        conn = get_conn()
        routing_config = get_org_routing_config(conn, DEFAULT_ORG_ID)
        ws_row = None
        if routing_config.mode == WORKSPACE_ROUTING_MULTI:
            if not ws_slug:
                conn.close()
                print(json.dumps({"error": "Multi-workspace mode: --workspace is required for log-event"}))
                sys.exit(1)
            ws_row = resolve_workspace_identity(conn, ws_slug)
            if not ws_row:
                conn.close()
                print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                sys.exit(1)
        elif ws_slug:
            ws_row = resolve_workspace_identity(conn, ws_slug)
        conn.close()

        metadata = json.loads(args.metadata) if getattr(args, "metadata", None) else None
        log_event(lead_id=args.lead_id, event_type=args.event_type, direction=args.direction,
                  channel=args.channel, subject=args.subject, body_preview=args.body,
                  metadata=metadata)

        result = {"status": "logged", "lead_id": args.lead_id}
        if ws_row:
            conn = get_conn()
            status_defaults = {
                "email_sent": "contacted",
                "linkedin_connect": "contacted",
                "linkedin_message": "contacted",
                "email_reply": "replied",
                "linkedin_reply": "replied",
                "linkedin_connection_accepted": "replied",
                "meeting_booked": "scheduled",
            }
            initial_status = status_defaults.get(args.event_type, "prospecting")
            ws_lead_id = upsert_workspace_lead(
                conn, DEFAULT_ORG_ID, ws_row["id"], args.lead_id,
                status=initial_status)
            idem_key = f"agent_cli_{args.lead_id}_{args.event_type}_{datetime.now(timezone.utc).isoformat()}"
            append_workspace_event(
                conn, DEFAULT_ORG_ID, ws_row["id"], args.lead_id, ws_lead_id,
                event_type=args.event_type,
                event_at=datetime.now(timezone.utc).isoformat(),
                source_platform="agent",
                idempotency_key=idem_key,
                payload={"subject": args.subject, "direction": args.direction,
                         "channel": args.channel, "body_preview": args.body})
            conn.commit()
            conn.close()
            result["workspace"] = ws_row["slug"]
        print(json.dumps(result))
        if getattr(args, "crm_sync", False) and ws_slug:
            _maybe_trigger_crm_sync(lead_id=args.lead_id, workspace_slug=ws_slug)
    elif args.command == "review":
        if args.review_command == "templates" and args.templates_command == "list":
            print(json.dumps({"templates": ["dedup-review", "lead-review"]}, indent=2))
            sys.exit(0)
        elif args.review_command == "presets":
            template = pipeline_lead_review.normalize_review_template(args.template)
            tok = get_agent_key()
            api_base = review_cloud.get_api_base(load_config)
            if tok and review_cloud.review_enabled(load_config, get_agent_key):
                try:
                    print(json.dumps(review_cloud.list_presets(api_base, tok, template=template), indent=2))
                except RuntimeError:
                    print(json.dumps(pipeline_lead_review.list_presets(template), indent=2))
            else:
                print(json.dumps(pipeline_lead_review.list_presets(template), indent=2))
            sys.exit(0)
        elif args.review_command == "export-payload":
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace required"}))
                sys.exit(1)
            custom_fields = None
            if getattr(args, "fields", None):
                custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
            conn = get_conn()
            try:
                payload = pipeline_lead_review.build_export_payload(
                    conn,
                    workspace=args.workspace,
                    detail=getattr(args, "detail", "standard"),
                    title=args.title,
                    custom_fields=custom_fields,
                    tag=getattr(args, "tag", None),
                    stage=getattr(args, "stage", None),
                    since=getattr(args, "since", None),
                    limit=getattr(args, "limit", 5000),
                    never_contacted=getattr(args, "never_contacted", False),
                    no_email=getattr(args, "no_email", False),
                    require_domain=getattr(args, "require_domain", False),
                    enrich_fn=enrich_lead_rows,
                    **pipeline_lead_review.review_export_filter_kwargs(args),
                )
            except (ValueError, OSError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            finally:
                conn.close()
            print(json.dumps(payload, indent=2))
            sys.exit(0)
        elif args.review_command == "apply-sync":
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace required"}))
                sys.exit(1)
            in_path = resolve_project_path(args.input, kind="input")
            try:
                sheet_rows = json.loads(in_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(json.dumps({"error": f"invalid --input JSON: {e}"}))
                sys.exit(1)
            if not isinstance(sheet_rows, list):
                print(json.dumps({"error": "--input must be a JSON array of row objects"}))
                sys.exit(1)
            conn = get_conn()
            try:
                ws_row = resolve_workspace_identity(conn, args.workspace)
                if not ws_row:
                    print(json.dumps({"error": f"workspace not found: {args.workspace}"}))
                    sys.exit(1)
                summary = pipeline_lead_review.apply_lead_review_sync(
                    conn,
                    ws_row["id"],
                    sheet_rows,
                    upsert_workspace_lead_fn=upsert_workspace_lead,
                    org_id=DEFAULT_ORG_ID,
                    dry_run=args.dry_run or not args.commit,
                )
            finally:
                conn.close()
            print(json.dumps(summary, indent=2))
            sys.exit(0)
        elif args.review_command in ("export", "sync"):
            tok = get_agent_key()
            if not tok:
                print(json.dumps({"error": "login required — ask Outreach Magic to log in"}))
                sys.exit(1)
            api_base = review_cloud.get_api_base(load_config)
            template = pipeline_lead_review.normalize_review_template(args.template)
        if args.review_command == "export":
            try:
                if template == "lead-review":
                    if not getattr(args, "workspace", None):
                        print(json.dumps({"error": "--workspace required for lead-review export"}))
                        sys.exit(1)
                    custom_fields = None
                    if getattr(args, "fields", None):
                        custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
                    conn = get_conn()
                    try:
                        payload = pipeline_lead_review.build_export_payload(
                            conn,
                            workspace=args.workspace,
                            detail=getattr(args, "detail", "standard"),
                            title=args.title,
                            custom_fields=custom_fields,
                            tag=getattr(args, "tag", None),
                            stage=getattr(args, "stage", None),
                            since=getattr(args, "since", None),
                            limit=getattr(args, "limit", 5000),
                            never_contacted=getattr(args, "never_contacted", False),
                            no_email=getattr(args, "no_email", False),
                            require_domain=getattr(args, "require_domain", False),
                            enrich_fn=enrich_lead_rows,
                            **pipeline_lead_review.review_export_filter_kwargs(args),
                        )
                    finally:
                        conn.close()
                    if not payload.get("rows"):
                        print(json.dumps({"error": "no leads matched export filters"}))
                        sys.exit(1)
                    share_email, public_link = resolve_sheets_export_access(args)
                    sheet_id = getattr(args, "sheet_id", None)
                    parent_sheet_id = getattr(args, "parent_sheet_id", None)
                    tab_name = getattr(args, "tab_name", None)
                    result = review_cloud.export_review(
                        api_base,
                        tok,
                        template=template,
                        title=args.title,
                        share_email=share_email,
                        public_link=public_link,
                        sheet_id=str(sheet_id).strip() if sheet_id else None,
                        parent_sheet_id=str(parent_sheet_id).strip() if parent_sheet_id else None,
                        tab_title=tab_name or getattr(args, "stage", None),
                        detail=payload.get("detail"),
                        headers=payload.get("headers"),
                        rows=payload.get("rows"),
                        workspace=args.workspace,
                        columns=payload.get("columns"),
                        freeze_header=payload.get("freeze_header"),
                    )
                else:
                    if not getattr(args, "input", None):
                        print(json.dumps({"error": "--input required for dedup-review export"}))
                        sys.exit(1)
                    in_path = resolve_project_path(args.input, kind="input")
                    payload = pipeline_dedup.load_candidates_file(str(in_path))
                    candidates = payload.get("candidates") or []
                    if not candidates:
                        print(json.dumps({"error": "no candidates in input file"}))
                        sys.exit(1)
                    result = review_cloud.export_review(
                        api_base,
                        tok,
                        template=template,
                        candidates=candidates,
                        title=args.title,
                        share_email=require_share_email_for_export(getattr(args, "share_email", None)),
                    )
            except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if template == "lead-review" and isinstance(result, dict) and result.get("sheet_id"):
                try:
                    meta_path = save_sheets_export_record(
                        workspace=args.workspace,
                        title=args.title,
                        sheet_id=str(result["sheet_id"]),
                        url=str(result.get("url") or result.get("spreadsheet_url") or ""),
                        detail=str(payload.get("detail") or getattr(args, "detail", "") or ""),
                        tag=getattr(args, "tag", None),
                        stage=getattr(args, "stage", None),
                        since=getattr(args, "since", None),
                        limit=getattr(args, "limit", 5000),
                        never_contacted=getattr(args, "never_contacted", False),
                        no_email=getattr(args, "no_email", False),
                        require_domain=getattr(args, "require_domain", False),
                        **pipeline_lead_review.review_export_filter_kwargs(args),
                    )
                    result = dict(result)
                    result["metadata_path"] = str(meta_path)
                except OSError:
                    pass
            print(json.dumps(result, indent=2))
        elif args.review_command == "sync":
            field_keys = None
            baseline_rows = None
            if template == "lead-review":
                ws_slug = getattr(args, "workspace", None)
                if not ws_slug:
                    print(json.dumps({"error": "--workspace required for lead-review sync"}))
                    sys.exit(1)
                detail = getattr(args, "detail", None) or "standard"
                custom_fields = None
                if getattr(args, "fields", None):
                    custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
                conn = get_conn()
                try:
                    ws_row = resolve_workspace_identity(conn, ws_slug)
                    if not ws_row:
                        print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                        sys.exit(1)
                    senders = (
                        pipeline_lead_review.list_workspace_senders(conn, ws_row["id"])
                        if detail == "full"
                        else []
                    )
                    columns = pipeline_lead_review.resolve_columns(
                        detail, custom_fields=custom_fields, sender_profiles=senders,
                    )
                    field_keys = {label: key for label, key in columns}

                    # Resolve export filters from stored metadata + CLI override
                    stored = find_sheets_export_record(args.sheet_id)
                    stored_filters = (stored or {}).get("filters") or {}
                    effective_tag = getattr(args, "tag", None) or stored_filters.get("tag")
                    effective_stage = getattr(args, "stage", None) or stored_filters.get("stage")
                    effective_since = getattr(args, "since", None) or stored_filters.get("since")
                    effective_never = (
                        getattr(args, "sync_never_contacted", False)
                        or stored_filters.get("never_contacted", False)
                    )
                    effective_no_email = (
                        getattr(args, "sync_no_email", False)
                        or stored_filters.get("no_email", False)
                    )
                    effective_domain = (
                        getattr(args, "sync_require_domain", False)
                        or stored_filters.get("require_domain", False)
                    )
                    effective_limit = (
                        getattr(args, "limit", None)
                        or stored_filters.get("limit")
                        or 5000
                    )

                    if stored:
                        # Primary path: build a targeted baseline using stored filters
                        # plus any CLI overrides the user supplied.
                        export_payload = pipeline_lead_review.build_export_payload(
                            conn,
                            workspace=ws_slug,
                            detail=detail,
                            title="baseline",
                            custom_fields=custom_fields,
                            tag=effective_tag,
                            stage=effective_stage,
                            since=effective_since,
                            limit=effective_limit,
                            never_contacted=effective_never,
                            no_email=effective_no_email,
                            require_domain=effective_domain,
                            enrich_fn=enrich_lead_rows,
                        )
                        baseline_rows = []
                        for row in export_payload.get("rows") or []:
                            obj: dict[str, Any] = {"lead_id": row[0] if row else ""}
                            for i, (_label, key) in enumerate(columns):
                                if i < len(row):
                                    obj[key] = row[i]
                            baseline_rows.append(obj)
                    else:
                        # Fallback: no stored metadata — skip the baseline.
                        # The cloud API returns all sheet rows, and
                        # apply_lead_review_sync() diffs each one against
                        # current DB state via _current_row_state(), which is
                        # the authoritative comparison. Slightly more data
                        # over the wire, but always correct.
                        baseline_rows = None
                finally:
                    conn.close()
            try:
                read_result = review_cloud.sync_read(
                    api_base,
                    tok,
                    sheet_id=args.sheet_id,
                    template=template,
                    field_keys=field_keys,
                    baseline_rows=baseline_rows,
                )
            except RuntimeError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if template == "lead-review":
                sheet_rows = read_result.get("rows") or []
                if not sheet_rows:
                    print(json.dumps({
                        "status": "noop",
                        "rows": 0,
                        "changed_count": read_result.get("changed_count", 0),
                        "message": "no rows to sync",
                    }))
                    sys.exit(0)
                conn = get_conn()
                try:
                    ws_row = resolve_workspace_identity(conn, ws_slug)

                    summary = pipeline_lead_review.apply_lead_review_sync(
                        conn,
                        ws_row["id"],
                        sheet_rows,
                        upsert_workspace_lead_fn=upsert_workspace_lead,
                        org_id=DEFAULT_ORG_ID,
                        dry_run=args.dry_run or not args.commit,
                    )
                finally:
                    conn.close()
                if args.commit and summary.get("updated"):
                    write_results = [
                        {"lead_id": int(chg["lead_id"]), "result": "✓ Synced"}
                        for chg in summary.get("changes") or []
                        if chg.get("lead_id") is not None
                    ]
                    if write_results:
                        try:
                            review_cloud.sync_write_results(
                                api_base,
                                tok,
                                sheet_id=args.sheet_id,
                                template=template,
                                results=write_results,
                            )
                        except RuntimeError:
                            pass
                print(json.dumps(summary, indent=2))
                sys.exit(0)

            approved = read_result.get("approved") or []
            if not approved:
                print(json.dumps({"status": "noop", "approved": 0, "message": "no rows to sync"}))
                sys.exit(0)
            merge_candidates = [
                {"keep_id": int(row["keep_id"]), "merge_id": int(row["merge_id"])}
                for row in approved
            ]
            if args.dry_run or not args.commit:
                print(json.dumps({
                    "status": "dry_run",
                    "approved": len(merge_candidates),
                    "approved_pairs": merge_candidates,
                }, indent=2))
                sys.exit(0)
            conn = get_conn()
            try:
                merge_result = pipeline_dedup.batch_merge_candidates(
                    conn,
                    merge_candidates,
                    commit=True,
                    reason="dedup_review",
                    merge_leads_fn=merge_leads,
                )
            finally:
                conn.close()
            failures = {
                (int(f.get("keep_id")), int(f.get("merge_id"))): f.get("error", "failed")
                for f in (merge_result.get("failures") or [])
            }
            sheet_results = []
            for pair in merge_candidates:
                key = (pair["keep_id"], pair["merge_id"])
                if key in failures:
                    text = f"✗ {failures[key]}"
                else:
                    text = "✓ Merged"
                sheet_results.append({
                    "keep_id": pair["keep_id"],
                    "merge_id": pair["merge_id"],
                    "result": text,
                })
            try:
                write_result = review_cloud.sync_write_results(
                    api_base,
                    tok,
                    sheet_id=args.sheet_id,
                    results=sheet_results,
                    template=template,
                )
            except RuntimeError as e:
                merge_result["sheet_write_error"] = str(e)
                print(json.dumps({
                    "status": "completed_with_sheet_error",
                    "merge": merge_result,
                }, indent=2))
                sys.exit(1)
            print(json.dumps({
                "status": "completed",
                "merge": merge_result,
                "sheet": write_result,
            }, indent=2))
        else:
            print(json.dumps({"error": f"unknown review subcommand: {args.review_command}"}))
            sys.exit(1)
    elif args.command == "dedup":
        if args.dedup_command == "find":
            conn = get_conn()
            try:
                payload = pipeline_dedup.find_duplicates(
                    conn,
                    workspace_slug=args.workspace,
                    tag_filter=args.tag,
                    min_confidence=args.min_confidence,
                    resolve_workspace_fn=resolve_workspace_identity,
                    normalize_tag_fn=normalize_tag,
                )
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            finally:
                conn.close()
            text = json.dumps(payload, indent=2)
            if args.output:
                out = resolve_project_path(args.output, kind="export", for_write=True)
                out.write_text(text + "\n", encoding="utf-8")
                print(json.dumps({"status": "written", "file": str(out), "stats": payload["stats"]}))
            else:
                print(text)
        elif args.dedup_command == "merge":
            try:
                cand_path = resolve_project_path(args.candidates, kind="input")
                payload = pipeline_dedup.load_candidates_file(str(cand_path))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            filtered = pipeline_dedup.filter_candidates(
                payload, min_confidence=args.min_confidence,
            )
            conn = get_conn()
            try:
                result = pipeline_dedup.batch_merge_candidates(
                    conn,
                    filtered,
                    commit=args.commit,
                    reason=args.reason,
                    merge_leads_fn=merge_leads,
                )
            finally:
                conn.close()
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"error": f"unknown dedup subcommand: {args.dedup_command}"}))
            sys.exit(1)
    elif args.command == "merge-leads":
        if args.keep and args.merge:
            result = merge_leads(args.keep, args.merge, reason="manual_cli")
        elif args.email and args.linkedin:
            keep_lead = find_lead(email=args.email)
            merge_lead = find_lead(linkedin=args.linkedin)
            if not keep_lead or not merge_lead:
                print(json.dumps({"error": "Could not resolve both leads by email and linkedin"}))
                sys.exit(1)
            if keep_lead["id"] == merge_lead["id"]:
                result = {"status": "noop", "keep_id": keep_lead["id"]}
            else:
                conn = get_conn()
                keep_id, merge_id = _pick_merge_keep_id(
                    conn, keep_lead["id"], merge_lead["id"]
                )
                conn.close()
                result = merge_leads(keep_id, merge_id, reason="manual_email_linkedin")
        else:
            print(json.dumps({"error": "Provide --keep and --merge, or --email and --linkedin"}))
            sys.exit(1)
        print(json.dumps(result, indent=2))
    elif args.command == "batch-lead-lookup":
        try:
            items = load_json_array_from_cli(
                json_input=getattr(args, "json_input", None),
                file_path=getattr(args, "file", None),
            )
            print(json.dumps(
                batch_lead_lookup(items, workspace=getattr(args, "workspace", None)),
                indent=2,
            ))
        except (json.JSONDecodeError, ValueError) as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
    elif args.command == "history":
        try:
            lead = find_lead(
                lead_id=args.id, email=args.email,
                linkedin=getattr(args, "linkedin", None), name=args.name,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if not lead:
            print(json.dumps({"error": "Lead not found"}))
            sys.exit(1)

        enriched = enrich_lead_rows([lead], workspace=getattr(args, "workspace", None))
        lead = enriched[0] if enriched else lead

        events = get_lead_events(lead["id"], args.limit)
        if args.json:
            print(json.dumps({"lead": lead, "events": events}, indent=2))
        else:
            print(format_event_timeline(lead, events))
    elif args.command == "copy-insights":
        try:
            insights = get_copy_insights(
                lead_status=args.lead_status,
                limit=args.limit,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(insights, indent=2) if args.json else format_copy_insights(insights))
    elif args.command == "segment-insights":
        try:
            insights = get_segment_insights(
                positive_lead_status=args.positive_lead_status,
                positive_sentiment=args.positive_sentiment,
                fields=args.fields,
                min_sent=args.min_sent,
                top=args.top,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(insights, indent=2) if args.json else format_segment_insights(insights))
    elif args.command == "workspace":
        if args.workspace_cmd == "summary":
            summary = get_workspace_summary(
                args.workspace,
                tags_only=getattr(args, "tags_only", False),
            )
            if summary.get("error"):
                print(json.dumps(summary, indent=2) if getattr(args, "json", False) else summary["error"])
                sys.exit(1)
            if getattr(args, "json", False):
                summary["local_pending"] = get_local_pending_counts()
                print(json.dumps(summary, indent=2))
            else:
                print(format_workspace_summary(summary))
        elif args.workspace_cmd == "create":
            print(json.dumps(create_workspace(args.name, args.slug, sync=getattr(args, "sync", False)), indent=2))
        elif args.workspace_cmd == "sync":
            print(json.dumps(sync_workspaces_to_cloud(), indent=2))
        elif args.workspace_cmd == "routing":
            if args.workspace_routing_cmd == "set":
                print(json.dumps(
                    set_workspace_routing(args.mode, workspace_slug=args.workspace),
                    indent=2,
                ))
            else:
                print(json.dumps(get_workspace_routing(), indent=2))
        elif args.workspace_cmd == "list":
            workspaces = list_workspaces()
            if getattr(args, "json", False):
                print(json.dumps(
                    attach_freshness({"workspaces": workspaces}, last_pull=get_last_pull()),
                    indent=2,
                ))
            else:
                print_freshness_stderr(get_last_pull())
                for ws in workspaces:
                    print(f"  {ws.get('slug') or ws.get('name')}: {ws.get('name')}")
        else:
            print(json.dumps(list_workspaces(), indent=2))
    elif args.command == "campaign-map":
        if args.campaign_map_cmd == "add":
            print(json.dumps(
                add_campaign_map_cli(
                    args.platform,
                    args.workspace,
                    campaign_platform_id=args.campaign_platform_id,
                    campaign_name=args.campaign_name,
                    match_strategy=args.match_strategy,
                    priority=args.priority,
                ),
                indent=2,
            ))
        else:
            print(json.dumps(list_campaign_maps(), indent=2))
    elif args.command == "quarantine":
        if args.quarantine_cmd == "skip":
            if getattr(args, "all", False):
                print(json.dumps(skip_quarantine_bulk(all_pending=True), indent=2))
            elif getattr(args, "reason", None):
                print(json.dumps(
                    skip_quarantine_bulk(
                        reason=args.reason,
                        platform=getattr(args, "platform", None),
                    ),
                    indent=2,
                ))
            elif getattr(args, "campaign_platform_id", None):
                print(json.dumps(
                    skip_quarantine_bulk(
                        campaign_platform_id=args.campaign_platform_id,
                        platform=getattr(args, "platform", None),
                    ),
                    indent=2,
                ))
            elif getattr(args, "id", None):
                print(json.dumps(skip_quarantine(args.id), indent=2))
            else:
                print("Error: quarantine skip requires --id, --campaign-platform-id, --reason, or --all")
                sys.exit(1)
        elif args.quarantine_cmd == "backfill-no-campaign":
            print(json.dumps(
                backfill_null_campaign_quarantine(
                    auto_skip=not getattr(args, "keep_pending", False),
                    quiet=False,
                ),
                indent=2,
            ))
        elif args.quarantine_cmd == "assign":
            print(json.dumps(assign_quarantine(args.id, args.workspace), indent=2))
        elif args.quarantine_cmd == "replay":
            print(json.dumps(replay_pending_quarantine(args.workspace, args.limit), indent=2))
        else:
            status = getattr(args, "status", "pending") or "pending"
            print_freshness_stderr(get_last_pull())
            if getattr(args, "json", False):
                raw_limit = getattr(args, "limit", 0) or 0
                limit = raw_limit if raw_limit > 0 else 1000000
                rows = list_quarantine(status=status, limit=limit)
                print(json.dumps(
                    attach_freshness({"items": rows}, last_pull=get_last_pull()),
                    indent=2,
                ))
            elif status == "pending":
                rows = list_quarantine(status=status, limit=50)
                if not rows:
                    print("No pending quarantined events.")
                else:
                    print(f"Pending quarantine ({len(rows)} row(s), showing up to 50):")
                    for row in rows:
                        campaign = row.get("campaign_name_raw") or row.get("campaign_platform_id") or "unknown"
                        print(
                            f"  {row.get('id')}  {row.get('source_platform') or '-'}  {campaign}"
                        )
                    print()
                    print(format_quarantine_campaign_summary(get_quarantine_campaign_summary()))
            else:
                print(json.dumps(list_quarantine(status=status, limit=50), indent=2))
    elif args.command == "personalize-set":
        if args.batch:
            items = json.loads(args.json_input or "[]")
            print(json.dumps(personalize_set_batch(items), indent=2))
        else:
            if not args.lead_id or not args.field or args.value is None:
                print("Error: --lead-id, --field, and --value are required (or use --batch --json)")
                sys.exit(1)
            print(json.dumps(personalize_set(
                args.lead_id, args.field, args.value, field_date=getattr(args, "date", None),
            ), indent=2))
    elif args.command == "personalize-get":
        result = personalize_get(args.lead_id, layer=getattr(args, "layer", "merged"))
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            if not result:
                print(f"No personalization for lead {args.lead_id}")
            else:
                for k, v in sorted(result.items()):
                    print(f"  {k}: {v}")
    elif args.command == "personalize-pending":
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        result = personalize_pending(fields, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} leads pending (fields: {', '.join(fields)})")
            for r in result:
                print(f"  [{r['id']}] {r['name'] or '?'} — {r['email'] or ''}")
    elif args.command == "personalize-status":
        result = personalize_status()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"Total leads: {result['total_leads']}")
            print(f"Personalized: {result['personalized']}")
            print(f"Pending: {result['pending']}")
            print(f"Stale: {result['stale']}")
    elif args.command == "company-personalize-set":
        if args.batch:
            items = json.loads(args.json_input or "[]")
            print(json.dumps(company_personalize_set_batch(items), indent=2))
        else:
            if not args.field or args.value is None:
                print("Error: --field and --value required (plus --company-id, --domain, or --name)")
                sys.exit(1)
            if not any([args.company_id, args.domain, args.name]):
                print("Error: --company-id, --domain, or --name required")
                sys.exit(1)
            print(json.dumps(company_personalize_set(
                args.field, args.value,
                company_id=args.company_id, domain=args.domain, name=args.name,
                field_date=getattr(args, "date", None),
            ), indent=2))
    elif args.command == "company-personalize-get":
        result = company_personalize_get(
            company_id=args.company_id, domain=args.domain, name=args.name,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            for k, v in sorted(result.items()):
                print(f"  {k}: {v}")
    elif args.command == "company-personalize-pending":
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        result = company_personalize_pending(fields, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} companies pending (fields: {', '.join(fields)})")
            for r in result:
                print(f"  [{r['company_id']}] {r['name']} — {r['domain'] or ''} ({r['lead_count']} leads)")
    elif args.command == "company-personalize-status":
        result = company_personalize_status()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"Total companies: {result['total_companies']}")
            print(f"Personalized: {result['personalized']}")
            print(f"Pending: {result['pending']}")
            print(f"Stale: {result['stale']}")
    elif args.command == "personalize-clear":
        result = personalize_clear(
            lead_id=args.lead_id,
            field=args.field,
            clear_all=getattr(args, "clear_all", False),
        )
        print(json.dumps(result, indent=2))
    elif args.command == "cleanup-rules":
        result = cleanup_campaign_rules(dry_run=getattr(args, "dry_run", False))
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            if result["dry_run"]:
                print(f"Would remove {result['found']} invalid rules")
            else:
                print(f"Removed {result['removed']} invalid mapping rules")
    else:
        if not db_exists():
            init_db()
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))

    if (
        args.command in ("workspace", "campaign-map", "quarantine", "pull", "enrich", "stage", "import-profiles", None)
        and not getattr(args, "json", False)
    ):
        try:
            hint = format_local_sync_hint(get_local_pending_counts())
            if hint:
                print(hint, file=sys.stderr)
        except Exception:
            pass



if __name__ == "__main__":
    main()
