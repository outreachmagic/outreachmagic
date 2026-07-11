#!/usr/bin/env python3
"""
Outreach Magic — Agent-First Lead Database for Hermes

One SQLite file. No MongoDB. No BigQuery. Just your leads, visible.

Architecture:
  ~/.hermes/skills/outreachmagic/databases/outreachmagic.db  — Local SQLite database
  api.outreachmagic.io           — cloud relay server (optional)
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
  pipeline.py query engagement --workspace acme --since 48h --json
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
    AUTO_MERGE_SAFE_IDENTITY_TYPES,
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
    normalize_relay_timestamp_for_storage,
    notify_update_available,
    pull_if_stale_skip_result,
    record_install_source,
    resolve_update_source,
    rollback_skill,
    save_config,
    utc_now_for_storage,
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
# Every scripts/**/*.py — auto-discovered so new modules (incl. subpackages
# like crm_drivers/) are not skipped by update.
_SCRIPTS_ROOT = Path(__file__).resolve().parent
UPDATE_SCRIPT_FILES = tuple(
    sorted(p.relative_to(_SCRIPTS_ROOT).as_posix() for p in _SCRIPTS_ROOT.glob("**/*.py"))
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
        # Add the merged lead's email as an additional email if it differs from primary
        # (case/whitespace-normalized so "Alex@x.com" vs "alex@x.com" doesn't duplicate)
        merged_email_norm = normalize_email(other["email"])
        kept_email_norm = normalize_email(keep["email"])
        if merged_email_norm and merged_email_norm != kept_email_norm:
            conn.execute(
                "INSERT OR IGNORE INTO lead_emails (lead_id, email, is_primary) VALUES (?, ?, 0)",
                (keep_id, merged_email_norm),
            )
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
        # Merge just modified the survivor (secondary email, moved children, etc.),
        # but relay_ingested may already carry an entry for keep_id at least as
        # recent (from the pull that brought both leads in) -- if the pull and
        # this merge land in the same wall-clock second, relay_bump_explained_clause
        # would wrongly treat the merge's updated_at bump as relay data being
        # echoed back rather than a genuine local change. Downdate so the survivor
        # is unambiguously pending re-sync regardless of timestamp resolution.
        conn.execute(
            "UPDATE relay_ingested SET ingested_at = datetime('now', '-1 second') WHERE lead_id = ?",
            (keep_id,),
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

    raw_name = profile.get("name")
    name = raw_name
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
        # If the input didn't include a name, don't let the synthesized
        # create-time fallback (name_from_email / "Unknown") also overwrite an
        # existing matched lead's real name when overwrite=True. resolve_lead
        # treats enrich_name=None as "not specified, inherit `name`" — so we
        # must pass "" (not None) to actually force "nothing to enrich with".
        enrich_name=enrich_name if enrich_name is not None else (raw_name or ""),
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
    "linkedin_headline", "linkedin_bio",
)

# Canonical → alias mapping applied before _extract_extra_import_fields.
# Keys in this dict are checked first; if the canonical key is absent from
# the raw row and an alias exists, the value is copied to the canonical key.
_EXTRA_FIELD_ALIASES: dict[str, str] = {
    "domain": "company_domain",
}

# Fields pulled into `extra` that map to real `leads`/`workspace_leads` columns
# (handled by dedicated write blocks below) rather than the generic
# personalization loop -- anything NOT in this set gets written to
# lead_personalization/company_personalization instead. linkedin_headline and
# linkedin_bio must stay listed here; otherwise they'd double-write into
# personalization as bogus custom merge-fields in addition to the leads columns.
RESERVED_IMPORT_FIELDS = frozenset([
    "company_domain", "is_connected_linkedin", "is_linkedin_request_pending",
    "lead_status", "lead_sentiment", "import_name", "list_source",
    "tags", "contact_order", "hq_city", "hq_state", "hq_country",
    "external_id", "notes", "last_message_sent", "last_message_received",
    "member linkedin sales nav id", "linkedin_sales_nav_id", "sales_nav_id",
    "linkedin_headline", "linkedin_bio",
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

        headline = extra.get("linkedin_headline")
        bio = extra.get("linkedin_bio")
        if headline or bio:
            li_conn = shared_conn or get_conn()
            li_row = li_conn.execute(
                "SELECT linkedin_headline, linkedin_bio FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            li_sets, li_params = [], []
            if headline and (overwrite or not (li_row["linkedin_headline"] or "").strip()):
                li_sets.append("linkedin_headline = ?")
                li_params.append(headline)
            if bio and (overwrite or not (li_row["linkedin_bio"] or "").strip()):
                li_sets.append("linkedin_bio = ?")
                li_params.append(bio)
            if li_sets:
                li_sets.append("updated_at = datetime('now')")
                li_params.append(lead_id)
                li_conn.execute(f"UPDATE leads SET {', '.join(li_sets)} WHERE id = ?", li_params)
                if li_conn is not shared_conn:
                    li_conn.commit()
            if li_conn is not shared_conn:
                li_conn.close()

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


from pipeline_batch_jobs import (
    record_batch_job, find_pending_batch_job, mark_batch_job_status, list_batch_jobs,
)

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

from pipeline_workspace import (
    _push_agent_events_to_relay,
    _push_pending_company_updates,
    _push_pending_lead_snapshots,
    _push_pending_merge_deletes,
    _push_pending_quarantine_resolutions,
    _relay_log,
    _relay_push_batches,
    add_campaign_map_cli,
    api_keys_cli,
    assign_quarantine,
    backfill_null_campaign_quarantine,
    cleanup_stale_quarantine_for_reprocessed,
    create_workspace,
    format_local_sync_hint,
    format_quarantine_campaign_summary,
    format_routing_refresh_summary,
    format_sync_status,
    get_local_pending_counts,
    get_quarantine_campaign_summary,
    get_routing_config_summary,
    get_sync_status,
    get_workspace_routing,
    list_campaign_maps,
    list_quarantine,
    list_workspaces,
    maybe_backfill_null_campaign_quarantine,
    maybe_sync_agent_secrets_from_cloud,
    maybe_sync_routing_from_cloud,
    preview_sync,
    print_quarantine_guidance,
    set_workspace_routing,
    skip_quarantine,
    skip_quarantine_bulk,
    sync_agent_secrets_cli,
    sync_all,
    sync_workspaces_to_cloud,
)


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
    print_pull_diagnostics,
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
    write_export_csv,
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

from pipeline_cli import _cmd_crm_sync, _cmd_sheets_campaign_stats, main


if __name__ == "__main__":
    main()
