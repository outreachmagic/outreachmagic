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
  pipeline.py log-event --lead-id 1 ...     # Log outreach event
  pipeline.py history --id 1                # Show lead's event timeline
  pipeline.py history --email j@acme.com    # Look up by email
  pipeline.py history --name "Jane"         # Look up by name (partial)
  pipeline.py stats                         # Quick stats
  pipeline.py campaigns                   # Counts by campaign name
  pipeline.py update                        # Install latest release (user-triggered)
  pipeline.py update --check                # Check for newer release without installing
"""

import ast
import sqlite3
import json
import os
import sys
import csv
import argparse
import hashlib
import re
import shutil
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relay_extractors import (
    build_display_name,
    extract_bounce_fields,
    extract_relay_fields,
    extract_relay_identity,
    name_from_email,
)
from workspace_routing import (
    CampaignContext,
    DEFAULT_ORG_ID,
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
    upsert_all_identities,
    upsert_identity_alias,
    enqueue_identity_conflict_merge,
    normalize_linkedin,
    parse_linkedin_value,
    upsert_linkedin_status,
    upsert_workspace_lead,
)

import routing_cloud
import connections_cloud
import db_health
import quarantine_resolutions as qres
import workspace_archive

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
)
from constants import (
    ATTRIBUTE_INSIGHT_FIELDS,
    BILLING_UPGRADE_URL,
    MAX_EVENT_BODY_STORAGE_CHARS,
    PIPELINE_STAGES,
    RELAY_PUSH_BATCH_SIZE,
    RELAY_PUSH_MAX_ATTEMPTS,
    RELAY_PUSH_RETRY_BASE_SECONDS,
    RELAY_PUSH_TIMEOUT_SECONDS,
    SHARED_EMAIL_DOMAINS,
    STAGE_EMOJI,
    USAGE_WARNING_PERCENT,
)
from db_conn import get_conn
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
    normalize_lead_status_display,
    relay_already_ingested,
    relay_dedupe_key,
)


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

from om_paths import (
    ensure_project_layout,
    get_agent_resources_dir,
    get_config_path,
    get_data_root,
    get_db_path,
    get_export_dir,
    get_input_dir,
    get_install_dir,
    get_project_root,
    get_skill_home,
    hermes_profile_copy_warning,
    resolve_project_path,
)

SKILL_NAME = "outreachmagic"
RELAY_URL = "https://api.outreachmagic.io"

SKILL_SCRIPTS_DIR = f"skills/{SKILL_NAME}/scripts"
UPDATE_SCRIPT_FILES = (
    "pipeline.py",
    "constants.py",
    "db_conn.py",
    "formatters.py",
    "bounces.py",
    "activity_sync.py",
    "event_classification.py",
    "lead_sync.py",
    "platform_registry.py",
    "relay_ingest.py",
    "relay_extractors.py",
    "workspace_routing.py",
    "workspace_archive.py",
    "routing_cloud.py",
    "connections_cloud.py",
    "db_health.py",
    "om_paths.py",
    "device_login.py",
)
UPDATE_MANIFEST_FILES = (*UPDATE_SCRIPT_FILES, "VERSION")
# Unified public release repo (skills/outreachmagic layout).
SKILL_REPO_PATH = "skills/outreachmagic"
GITHUB_REPO = "outreachmagic/outreachmagic"


def _read_version_file(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    return "0.0.0"


__version__ = _read_version_file(Path(__file__).resolve().parent / "VERSION")


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.strip().split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts) or (0,)


def skill_scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def normalize_release_tag(tag: str) -> str:
    tag = tag.strip()
    if tag and not tag.startswith(("v", "V")):
        return f"v{tag}"
    return tag


def release_tag_version(tag: str) -> str:
    return normalize_release_tag(tag).lstrip("vV")


def effective_update_target() -> tuple[str, str]:
    """Repo + path prefix used for update downloads on this machine."""
    return GITHUB_REPO, SKILL_REPO_PATH


def update_release_candidates() -> list[tuple[str, str]]:
    """Ordered repos to try when resolving latest release / downloads."""
    return [effective_update_target()]


def raw_repo_base_for_tag(
    tag: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    repo, _path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    return f"https://raw.githubusercontent.com/{repo}/{normalize_release_tag(tag)}"


def scripts_base_for_tag(
    tag: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    repo, path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    base = f"https://raw.githubusercontent.com/{repo}/{normalize_release_tag(tag)}"
    if path == ".":
        return f"{base}/scripts"
    return f"{base}/{path}/scripts"


def update_manifest_url(repo_base: str, skill_repo_path: str) -> str:
    if skill_repo_path == ".":
        return f"{repo_base.rstrip('/')}/update-manifest.json"
    return f"{repo_base.rstrip('/')}/{skill_repo_path}/update-manifest.json"


def skill_md_url_for_repo(repo_base: str, skill_repo_path: str) -> str:
    if skill_repo_path == ".":
        return f"{repo_base.rstrip('/')}/SKILL.md"
    return f"{repo_base.rstrip('/')}/{skill_repo_path}/SKILL.md"


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"Outreach Magic/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_latest_release() -> Optional[dict]:
    """Return latest GitHub release metadata or None if unavailable."""
    for github_repo, skill_path in update_release_candidates():
        releases_url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        try:
            req = urllib.request.Request(
                releases_url,
                headers={
                    "User-Agent": f"Outreach Magic/{__version__}",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
            continue

        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            continue
        return {
            "tag": normalize_release_tag(tag),
            "version": release_tag_version(tag),
            "base": scripts_base_for_tag(tag, github_repo=github_repo, skill_repo_path=skill_path),
            "github_repo": github_repo,
            "skill_repo_path": skill_path,
        }
    return None


def dev_update_base_url() -> Optional[str]:
    """Dev-only override via config key dev_update_url (not env)."""
    cfg = load_config() if get_config_path().exists() else {}
    url = (cfg.get("dev_update_url") or "").strip()
    return url.rstrip("/") if url else None


def fetch_remote_version() -> Optional[str]:
    """Latest published release version, or None if no release is available."""
    release = fetch_latest_release()
    if release:
        return release["version"]
    if dev_update_base_url():
        try:
            return _fetch_url(f"{dev_update_base_url()}/VERSION").decode().strip()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return None
    return None


UPDATE_CHECK_INTERVAL_DEFAULT = 3600


def get_update_check_interval() -> int:
    cfg = load_config() if get_config_path().exists() else {}
    raw = cfg.get("update_check_interval_seconds", UPDATE_CHECK_INTERVAL_DEFAULT)
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return UPDATE_CHECK_INTERVAL_DEFAULT


def update_check_due() -> bool:
    cfg = load_config() if get_config_path().exists() else {}
    last = cfg.get("update_checked_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age >= get_update_check_interval()
    except (ValueError, TypeError):
        return True


def record_update_check():
    cfg = load_config()
    cfg["update_checked_at"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)


def notify_update_available(quiet: bool = False) -> None:
    """Check-only: inform the user when a newer release exists (never downloads)."""
    if not update_check_due():
        return
    record_update_check()
    remote = fetch_remote_version()
    if not remote or parse_version(remote) <= parse_version(__version__):
        return
    if quiet:
        return
    print(
        f"outreachmagic: update available {__version__} → {remote} "
        "(run: pipeline.py update  or  hermes skills update)",
        file=sys.stderr,
    )


def check_skill_update(quiet: bool = False) -> bool:
    """Return True if installed scripts match or exceed the latest release."""
    remote = fetch_remote_version()
    if not remote or parse_version(remote) <= parse_version(__version__):
        return True
    if not quiet:
        print(
            f"Update available: {__version__} → {remote} "
            "(run: pipeline.py update  or  hermes skills update)"
        )
    return False


def sync_skill_md_version():
    """Align SKILL.md frontmatter version with scripts/VERSION."""
    import re
    dest = skill_scripts_dir()
    ver = _read_version_file(dest / "VERSION")
    skill_md = dest.parent / "SKILL.md"
    if skill_md.exists():
        text = skill_md.read_text()
        skill_md.write_text(re.sub(r"^version: .*", f"version: {ver}", text, count=1, flags=re.M))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_update_manifest(repo_base: str, skill_repo_path: Optional[str] = None) -> Optional[dict]:
    _, default_path = effective_update_target()
    path = skill_repo_path if skill_repo_path is not None else default_path
    url = update_manifest_url(repo_base, path)
    try:
        payload = json.loads(_fetch_url(url).decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_update_source(explicit_tag: Optional[str] = None) -> tuple[Optional[Path], str, str, str]:
    """
    Resolve where to download skill files from.
    Returns (local_scripts_dir or None, scripts_base_url, repo_base_url, label).
    """
    dev_repo = (load_config().get("dev_repo") or "").strip() if get_config_path().exists() else ""
    if dev_repo:
        src = Path(dev_repo) / SKILL_REPO_PATH / "scripts"
        if not src.is_dir():
            raise FileNotFoundError(
                f"dev_repo in config has no {SKILL_REPO_PATH}/scripts/: {src}"
            )
        return src, "", str(Path(dev_repo)), "local clone"

    dev_base = dev_update_base_url()
    if dev_base:
        repo_base = dev_base.rsplit(f"/{SKILL_REPO_PATH}/scripts", 1)[0]
        return None, dev_base, repo_base, "dev_update_url"

    github_repo, skill_path = effective_update_target()

    if explicit_tag:
        norm = normalize_release_tag(explicit_tag)
        return (
            None,
            scripts_base_for_tag(norm, github_repo=github_repo, skill_repo_path=skill_path),
            raw_repo_base_for_tag(norm, github_repo=github_repo, skill_repo_path=skill_path),
            norm,
        )

    release = fetch_latest_release()
    if not release:
        raise RuntimeError(
            "No GitHub release found on the platform update repo for this install. "
            "Publish a release (see docs/RELEASING.md), run "
            "pipeline.py update --tag vX.Y.Z, or set dev_repo in config."
        )
    rel_repo = release.get("github_repo") or github_repo
    rel_path = release.get("skill_repo_path") or skill_path
    repo_base = raw_repo_base_for_tag(release["tag"], github_repo=rel_repo, skill_repo_path=rel_path)
    return None, release["base"], repo_base, release["tag"]


def update_skill(explicit_tag: Optional[str] = None) -> dict:
    """Download or copy a tagged release into this skill install, then migrate DB."""
    dest = skill_scripts_dir()
    local_src, scripts_base, repo_base, source_label = resolve_update_source(explicit_tag)
    updated: list[str] = []
    _, skill_path = effective_update_target()
    manifest = None if local_src else fetch_update_manifest(repo_base, skill_path)

    if local_src:
        for name in UPDATE_MANIFEST_FILES:
            shutil.copy2(local_src / name, dest / name)
            updated.append(name)
    else:
        for name in UPDATE_MANIFEST_FILES:
            content = _fetch_url(f"{scripts_base}/{name}")
            expected = (manifest or {}).get("files", {}).get(name)
            if expected and _sha256_hex(content) != expected:
                raise RuntimeError(
                    f"Checksum mismatch for {name} from {source_label}. "
                    "Refusing to install. Try again or report at security@outreachmagic.io."
                )
            (dest / name).write_bytes(content)
            updated.append(name)
        try:
            skill_md_url = skill_md_url_for_repo(repo_base, skill_path)
            skill_content = _fetch_url(skill_md_url)
            expected_md = (manifest or {}).get("files", {}).get("SKILL.md")
            if expected_md and _sha256_hex(skill_content) != expected_md:
                raise RuntimeError("Checksum mismatch for SKILL.md. Refusing to install.")
            (dest.parent / "SKILL.md").write_bytes(skill_content)
            updated.append("SKILL.md")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass

    init_db()
    sync_skill_md_version()
    cfg = load_config()
    cfg["auto_update"] = False
    cfg["installed_from_tag"] = source_label
    cfg.pop("update_url", None)
    save_config(cfg)
    new_version = _read_version_file(dest / "VERSION")
    return {
        "status": "updated",
        "version": new_version,
        "files": updated,
        "path": str(dest),
        "source": source_label,
    }



# ──────────────────────────────────────────────────────────────────────
# Config (api token, last pull timestamp)
# ──────────────────────────────────────────────────────────────────────

def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    if get_config_path().exists():
        return _load_json_dict(get_config_path())
    return {}


def _read_positive_int(raw: object, fallback: int) -> int:
    try:
        val = int(str(raw).strip())
        return val if val > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def get_relay_push_settings() -> dict:
    """Runtime-tunable relay push settings (env overrides config)."""
    cfg = load_config()
    batch_size = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_BATCH_SIZE", cfg.get("sync_batch_size", RELAY_PUSH_BATCH_SIZE)),
        RELAY_PUSH_BATCH_SIZE,
    )
    timeout_seconds = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS", cfg.get("sync_timeout_seconds", RELAY_PUSH_TIMEOUT_SECONDS)),
        RELAY_PUSH_TIMEOUT_SECONDS,
    )
    max_attempts = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_MAX_ATTEMPTS", cfg.get("sync_max_attempts", RELAY_PUSH_MAX_ATTEMPTS)),
        RELAY_PUSH_MAX_ATTEMPTS,
    )
    retry_base_seconds = _read_positive_int(
        os.environ.get(
            "OUTREACHMAGIC_SYNC_RETRY_BASE_SECONDS",
            cfg.get("sync_retry_base_seconds", RELAY_PUSH_RETRY_BASE_SECONDS),
        ),
        RELAY_PUSH_RETRY_BASE_SECONDS,
    )
    # Keep runtime bounds sane on small VPS boxes.
    batch_size = max(10, min(batch_size, 500))
    timeout_seconds = max(10, min(timeout_seconds, 300))
    max_attempts = max(1, min(max_attempts, 10))
    retry_base_seconds = max(1, min(retry_base_seconds, 60))
    return {
        "batch_size": batch_size,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
    }

def _chmod_best_effort(path: Path, mode: int):
    try:
        os.chmod(path, mode)
    except OSError:
        pass

def save_config(cfg: dict):
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)
    path.write_text(json.dumps(cfg, indent=2))
    _chmod_best_effort(path, 0o600)

def get_agent_key() -> Optional[str]:
    return os.environ.get("OUTREACHMAGIC_AGENT_KEY") or load_config().get("agent_key")

def get_last_pull() -> Optional[str]:
    return load_config().get("last_pull")

def set_last_pull(ts: str):
    cfg = load_config()
    cfg["last_pull"] = ts
    save_config(cfg)

def get_last_max_id() -> Optional[int]:
    return load_config().get("last_max_id")


_SNAPSHOT_CURSOR_KEYS = {
    "core": "last_snapshot_core_after_id",
    "workspace": "last_snapshot_workspace_after_id",
    "company": "last_snapshot_company_after_id",
}


def migrate_legacy_snapshot_cursor(cfg: Optional[dict] = None) -> dict:
    """Move pre-v2 last_snapshot_after_id into last_snapshot_workspace_after_id."""
    cfg = dict(cfg if cfg is not None else load_config())
    legacy = int(cfg.get("last_snapshot_after_id") or 0)
    ws_key = _SNAPSHOT_CURSOR_KEYS["workspace"]
    if legacy and not int(cfg.get(ws_key) or 0):
        cfg[ws_key] = legacy
    if "last_snapshot_after_id" in cfg:
        del cfg["last_snapshot_after_id"]
        save_config(cfg)
    return cfg


def get_snapshot_cursor(kind: str = "workspace") -> int:
    key = _SNAPSHOT_CURSOR_KEYS.get(kind, _SNAPSHOT_CURSOR_KEYS["workspace"])
    cfg = migrate_legacy_snapshot_cursor()
    return int(cfg.get(key) or 0)


def set_snapshot_cursor(snapshot_id: int, kind: str = "workspace") -> None:
    key = _SNAPSHOT_CURSOR_KEYS.get(kind, _SNAPSHOT_CURSOR_KEYS["workspace"])
    cfg = load_config()
    cfg[key] = int(snapshot_id)
    save_config(cfg)


def clear_snapshot_cursors() -> None:
    cfg = load_config()
    for key in _SNAPSHOT_CURSOR_KEYS.values():
        cfg.pop(key, None)
    cfg.pop("last_snapshot_after_id", None)
    save_config(cfg)


def normalize_relay_timestamp(ts: Optional[str]) -> str:
    """UTC ISO timestamp for relay push/pull (sortable, comparable in D1)."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    s = str(ts).strip()
    if "T" in s:
        if s.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", s):
            return s
        return s + "+00:00"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(\.\d+)?$", s)
    if m:
        frac = m.group(3) or ".000000"
        return f"{m.group(1)}T{m.group(2)}{frac}+00:00"
    return s


def set_last_max_id(max_id: int):
    cfg = load_config()
    cfg["last_max_id"] = max_id
    save_config(cfg)


def get_or_create_client_id() -> str:
    cfg = load_config()
    cid = cfg.get("client_id")
    if cid:
        return cid
    cid = str(uuid.uuid4())
    cfg["client_id"] = cid
    save_config(cfg)
    return cid


def get_workspace_routing_mode_from_config() -> Optional[str]:
    raw = str(load_config().get("workspace_routing_mode") or "").strip().lower()
    if raw in VALID_WORKSPACE_ROUTING_MODES:
        return raw
    return None


def sync_workspace_routing_mode_from_config(org_id: str = DEFAULT_ORG_ID):
    """If workspace_routing_mode is set in config, sync it into DB routing state."""
    mode = get_workspace_routing_mode_from_config()
    if not mode:
        return
    conn = get_conn()
    ensure_organization(conn, org_id)
    row = conn.execute(
        "SELECT workspace_routing_mode, default_workspace_id FROM organizations WHERE id = ?",
        (org_id,),
    ).fetchone()
    current_mode = (row["workspace_routing_mode"] or "").strip().lower() if row else ""
    current_ws_id = (row["default_workspace_id"] or "").strip() if row else ""
    if mode == WORKSPACE_ROUTING_SINGLE:
        ws_id = current_ws_id or ensure_default_org_workspace(conn)
        if current_mode != WORKSPACE_ROUTING_SINGLE or current_ws_id != ws_id:
            conn.execute(
                """UPDATE organizations
                   SET workspace_routing_mode = ?, default_workspace_id = ? WHERE id = ?""",
                (WORKSPACE_ROUTING_SINGLE, ws_id, org_id),
            )
            conn.commit()
    else:
        if current_mode != WORKSPACE_ROUTING_MULTI or current_ws_id:
            conn.execute(
                """UPDATE organizations
                   SET workspace_routing_mode = ?, default_workspace_id = NULL WHERE id = ?""",
                (WORKSPACE_ROUTING_MULTI, org_id),
            )
            conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    domain          TEXT,
    industry        TEXT,
    headcount       TEXT,
    headcount_numeric   INTEGER,
    hq_city             TEXT,
    hq_state            TEXT,
    hq_country          TEXT,
    cloud_pending       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    name                     TEXT NOT NULL,
    company_id               INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    company                  TEXT,
    title                    TEXT,
    industry                 TEXT,
    headcount                TEXT,
    headcount_numeric        INTEGER,
    email                    TEXT,
    email_domain             TEXT,
    linkedin_url             TEXT,
    location_city            TEXT,
    location_state           TEXT,
    location_country         TEXT,
    channel                  TEXT NOT NULL DEFAULT 'email',
    stage                    TEXT NOT NULL DEFAULT 'prospecting',
    notes                    TEXT,
    original_source          TEXT,
    original_source_detail   TEXT,
    original_source_platform TEXT,
    original_source_at       TEXT,
    latest_source            TEXT,
    latest_source_detail     TEXT,
    latest_source_platform   TEXT,
    latest_source_at         TEXT,
    email_verification_status TEXT,
    email_verified_at         TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    last_contact_at          TEXT,
    next_action              TEXT,
    next_action_at           TEXT,
    cloud_pending            INTEGER NOT NULL DEFAULT 0,
    latest_sender            TEXT,
    latest_sender_platform   TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    direction       TEXT NOT NULL DEFAULT 'outbound',
    channel         TEXT NOT NULL DEFAULT 'email',
    subject         TEXT,
    body_preview    TEXT,
    metadata_json   TEXT DEFAULT '{}',
    campaign_id     INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    sender          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaign_leads (
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_updated ON leads(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_lead_created ON events(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique ON leads(linkedin_url) WHERE linkedin_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL;

CREATE TABLE IF NOT EXISTS lead_merges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keep_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    merge_id        INTEGER NOT NULL,
    reason          TEXT,
    merged_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS relay_ingested (
    dedupe_key      TEXT PRIMARY KEY,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Org + workspace routing (org-wide lead, workspace-scoped status/events)
CREATE TABLE IF NOT EXISTS organizations (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    workspace_routing_mode  TEXT NOT NULL DEFAULT 'single',
    default_workspace_id    TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    cloud_synced    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, slug)
);

CREATE TABLE IF NOT EXISTS lead_identities (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    lead_id                 INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    identity_type           TEXT NOT NULL,
    identity_value_normalized TEXT NOT NULL,
    source                  TEXT,
    is_verified             INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, identity_type, identity_value_normalized)
);

CREATE INDEX IF NOT EXISTS idx_lead_identities_lead ON lead_identities(org_id, lead_id);

CREATE TABLE IF NOT EXISTS workspace_leads (
    id                       TEXT PRIMARY KEY,
    org_id                   TEXT NOT NULL,
    workspace_id             TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id                  INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status                   TEXT NOT NULL DEFAULT 'prospecting',
    owner_user_id            TEXT,
    stage_entered_at         TEXT,
    last_activity_at         TEXT,
    current_status_label     TEXT,
    current_status_sentiment TEXT,
    contact_priority         INTEGER,
    latest_sender            TEXT,
    cloud_pending            INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_leads_status ON workspace_leads(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_workspace_leads_owner ON workspace_leads(workspace_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_workspace_leads_activity ON workspace_leads(workspace_id, last_activity_at);

CREATE TABLE IF NOT EXISTS workspace_lead_events (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    workspace_lead_id   TEXT REFERENCES workspace_leads(id) ON DELETE SET NULL,
    event_type          TEXT NOT NULL,
    event_at            TEXT NOT NULL,
    source_platform     TEXT NOT NULL,
    external_event_id   TEXT,
    idempotency_key     TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ws_events_lead ON workspace_lead_events(workspace_id, lead_id, event_at);
CREATE INDEX IF NOT EXISTS idx_ws_events_type ON workspace_lead_events(workspace_id, event_type, event_at);

CREATE TABLE IF NOT EXISTS campaign_workspace_map (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_id             TEXT,
    campaign_name_normalized  TEXT,
    workspace_id            TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    match_strategy          TEXT NOT NULL DEFAULT 'id_exact',
    priority                INTEGER NOT NULL DEFAULT 100,
    is_active               INTEGER NOT NULL DEFAULT 1,
    cloud_synced            INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_campaign_map_lookup ON campaign_workspace_map(
    org_id, source_platform, is_active, priority
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_id ON campaign_workspace_map(
    org_id, source_platform, campaign_id
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_name ON campaign_workspace_map(
    org_id, source_platform, campaign_name_normalized
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_map_id_active ON campaign_workspace_map(
    org_id, source_platform, campaign_id
) WHERE campaign_id IS NOT NULL AND is_active = 1;

CREATE TABLE IF NOT EXISTS unmapped_campaign_queue (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_id             TEXT,
    campaign_name_raw       TEXT,
    campaign_name_normalized TEXT,
    external_event_id       TEXT,
    reason                  TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending',
    payload_json            TEXT NOT NULL,
    received_at             TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at             TEXT,
    cloud_pending           INTEGER NOT NULL DEFAULT 0,
    assigned_workspace      TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_status ON unmapped_campaign_queue(org_id, status, received_at);
CREATE INDEX IF NOT EXISTS idx_quarantine_campaign ON unmapped_campaign_queue(
    org_id, source_platform, campaign_id, status
);
CREATE INDEX IF NOT EXISTS idx_quarantine_cloud_pending ON unmapped_campaign_queue(cloud_pending)
    WHERE cloud_pending = 1;

CREATE TABLE IF NOT EXISTS lead_merge_jobs (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    keep_lead_id    INTEGER NOT NULL,
    merge_lead_id   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed',
    reason          TEXT,
    audit_json      TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lead_personalization (
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    field_value     TEXT NOT NULL,
    field_date      TEXT,
    source_hash     TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    cloud_pending   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (lead_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_personalization_pending ON lead_personalization(cloud_pending) WHERE cloud_pending = 1;

CREATE TABLE IF NOT EXISTS company_personalization (
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    field_value     TEXT NOT NULL,
    field_date      TEXT,
    source_hash     TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    cloud_pending   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (company_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_company_pers_pending ON company_personalization(cloud_pending) WHERE cloud_pending = 1;

CREATE TABLE IF NOT EXISTS workspace_lead_tags (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    tag             TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_wlt_workspace_tag ON workspace_lead_tags(workspace_id, tag);
CREATE INDEX IF NOT EXISTS idx_wlt_lead ON workspace_lead_tags(lead_id);

CREATE TABLE IF NOT EXISTS workspace_lead_linkedin_status (
    id                 TEXT PRIMARY KEY,
    workspace_id       TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id            INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    sender_profile     TEXT NOT NULL,
    is_connected       INTEGER NOT NULL DEFAULT 0,
    is_request_pending INTEGER NOT NULL DEFAULT 0,
    connected_at       TEXT,
    request_sent_at    TEXT,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id, sender_profile)
);

CREATE INDEX IF NOT EXISTS idx_li_status_workspace ON workspace_lead_linkedin_status(workspace_id, sender_profile);
CREATE INDEX IF NOT EXISTS idx_li_status_lead ON workspace_lead_linkedin_status(lead_id);

CREATE TABLE IF NOT EXISTS lead_email_verification (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    status          TEXT NOT NULL,
    sub_status      TEXT,
    source          TEXT NOT NULL,
    source_detail   TEXT,
    bounce_message  TEXT,
    free_email      INTEGER,
    mx_found        INTEGER,
    smtp_provider   TEXT,
    verified_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, lead_id, source)
);

CREATE INDEX IF NOT EXISTS idx_verification_email ON lead_email_verification(email);
CREATE INDEX IF NOT EXISTS idx_verification_status ON lead_email_verification(org_id, status);
CREATE INDEX IF NOT EXISTS idx_verification_lead ON lead_email_verification(lead_id);

CREATE TABLE IF NOT EXISTS bounce_events (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    first_event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
    latest_event_id     INTEGER REFERENCES events(id) ON DELETE SET NULL,
    platform            TEXT NOT NULL,
    sender_email        TEXT NOT NULL,
    lead_email          TEXT NOT NULL,
    bounce_type         TEXT NOT NULL DEFAULT 'unknown',
    bounce_message      TEXT,
    smtp_code           TEXT,
    recipient_mx        TEXT,
    sender_mx           TEXT,
    campaign_id         INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    campaign_name       TEXT,
    workspace_id        TEXT,
    relay_id            TEXT,
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lead_id, sender_email)
);

CREATE INDEX IF NOT EXISTS idx_bounce_events_lead ON bounce_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_bounce_events_platform ON bounce_events(platform, bounce_type);
CREATE INDEX IF NOT EXISTS idx_bounce_events_sender ON bounce_events(sender_email);
CREATE INDEX IF NOT EXISTS idx_bounce_events_seen ON bounce_events(last_seen_at DESC);
"""


# ──────────────────────────────────────────────────────────────────────
# Database Operations
# ──────────────────────────────────────────────────────────────────────
def init_db():
    db = get_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(db.parent, 0o700)
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    migrate_db(conn)
    conn.close()
    if db.exists():
        _chmod_best_effort(db, 0o600)
    return True


def migrate_db(conn=None):
    """Apply incremental schema changes and backfill derived data."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            domain TEXT,
            industry TEXT,
            headcount TEXT,
            headcount_numeric INTEGER,
            hq_city TEXT,
            hq_state TEXT,
            hq_country TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS lead_merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keep_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            merge_id INTEGER NOT NULL,
            reason TEXT,
            merged_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            cloud_synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, slug)
        );
        CREATE TABLE IF NOT EXISTS lead_identities (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            identity_type TEXT NOT NULL,
            identity_value_normalized TEXT NOT NULL,
            source TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, identity_type, identity_value_normalized)
        );
        CREATE TABLE IF NOT EXISTS workspace_leads (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'prospecting',
            owner_user_id TEXT,
            stage_entered_at TEXT,
            last_activity_at TEXT,
            current_status_label TEXT,
            current_status_sentiment TEXT,
            contact_priority INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id)
        );
        CREATE TABLE IF NOT EXISTS workspace_lead_events (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL,
            workspace_lead_id TEXT,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            external_event_id TEXT,
            idempotency_key TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS campaign_workspace_map (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            campaign_id TEXT,
            campaign_name_normalized TEXT,
            workspace_id TEXT NOT NULL,
            match_strategy TEXT NOT NULL DEFAULT 'id_exact',
            priority INTEGER NOT NULL DEFAULT 100,
            is_active INTEGER NOT NULL DEFAULT 1,
            cloud_synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS unmapped_campaign_queue (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            campaign_id TEXT,
            campaign_name_raw TEXT,
            campaign_name_normalized TEXT,
            external_event_id TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payload_json TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            cloud_pending INTEGER NOT NULL DEFAULT 0,
            assigned_workspace TEXT
        );
        CREATE TABLE IF NOT EXISTS lead_merge_jobs (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            keep_lead_id INTEGER NOT NULL,
            merge_lead_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            reason TEXT,
            audit_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS lead_personalization (
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            field_value TEXT NOT NULL,
            source_hash TEXT,
            processed_at TEXT NOT NULL DEFAULT (datetime('now')),
            cloud_pending INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (lead_id, field_name)
        );
        CREATE INDEX IF NOT EXISTS idx_personalization_pending ON lead_personalization(cloud_pending) WHERE cloud_pending = 1;
        CREATE TABLE IF NOT EXISTS workspace_lead_tags (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_wlt_workspace_tag ON workspace_lead_tags(workspace_id, tag);
        CREATE INDEX IF NOT EXISTS idx_wlt_lead ON workspace_lead_tags(lead_id);
        CREATE TABLE IF NOT EXISTS workspace_lead_linkedin_status (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            sender_profile TEXT NOT NULL,
            is_connected INTEGER NOT NULL DEFAULT 0,
            is_request_pending INTEGER NOT NULL DEFAULT 0,
            connected_at TEXT,
            request_sent_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id, sender_profile)
        );
        CREATE INDEX IF NOT EXISTS idx_li_status_workspace ON workspace_lead_linkedin_status(workspace_id, sender_profile);
        CREATE INDEX IF NOT EXISTS idx_li_status_lead ON workspace_lead_linkedin_status(lead_id);
        CREATE TABLE IF NOT EXISTS lead_email_verification (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            status TEXT NOT NULL,
            sub_status TEXT,
            source TEXT NOT NULL,
            source_detail TEXT,
            bounce_message TEXT,
            free_email INTEGER,
            mx_found INTEGER,
            smtp_provider TEXT,
            verified_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, lead_id, source)
        );
        CREATE INDEX IF NOT EXISTS idx_verification_email ON lead_email_verification(email);
        CREATE INDEX IF NOT EXISTS idx_verification_status ON lead_email_verification(org_id, status);
        CREATE INDEX IF NOT EXISTS idx_verification_lead ON lead_email_verification(lead_id);
        CREATE TABLE IF NOT EXISTS bounce_events (
            id                  TEXT PRIMARY KEY,
            org_id              TEXT NOT NULL,
            lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            first_event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
            latest_event_id     INTEGER REFERENCES events(id) ON DELETE SET NULL,
            platform            TEXT NOT NULL,
            sender_email        TEXT NOT NULL,
            lead_email          TEXT NOT NULL,
            bounce_type         TEXT NOT NULL DEFAULT 'unknown',
            bounce_message      TEXT,
            smtp_code           TEXT,
            recipient_mx        TEXT,
            sender_mx           TEXT,
            campaign_id         INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
            campaign_name       TEXT,
            workspace_id        TEXT,
            relay_id            TEXT,
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (lead_id, sender_email)
        );
        CREATE INDEX IF NOT EXISTS idx_bounce_events_lead ON bounce_events(lead_id);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_platform ON bounce_events(platform, bounce_type);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_sender ON bounce_events(sender_email);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_seen ON bounce_events(last_seen_at DESC);
    """)
    for col, col_type in [
        ("industry", "TEXT"), ("headcount", "TEXT"), ("email_domain", "TEXT"),
        ("company_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """UPDATE leads SET email_domain = lower(substr(email, instr(email, '@') + 1))
           WHERE email LIKE '%@%' AND (email_domain IS NULL OR email_domain = '')"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_email_domain ON leads(email_domain)")
    try:
        conn.execute(
            "ALTER TABLE events ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_lead_created ON events(lead_id, created_at DESC)"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_name ON campaigns(name)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(email) WHERE email IS NOT NULL"
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique
           ON leads(linkedin_url) WHERE linkedin_url IS NOT NULL"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL"
    )
    backfill_campaigns_from_events(conn)
    backfill_plusvibe_status_metadata(conn)
    for col, col_type in [
        ("workspace_routing_mode", "TEXT NOT NULL DEFAULT 'single'"),
        ("default_workspace_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE organizations ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    backfill_workspace_routing(conn)
    for tbl in ("workspaces", "campaign_workspace_map"):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN cloud_synced INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN cloud_pending INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN cloud_pending INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for col, col_type in [
        ("cloud_pending", "INTEGER NOT NULL DEFAULT 0"),
        ("assigned_workspace", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE unmapped_campaign_queue ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE lead_personalization ADD COLUMN field_date TEXT")
    except sqlite3.OperationalError:
        pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company_personalization (
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            field_value TEXT NOT NULL,
            field_date TEXT,
            source_hash TEXT,
            processed_at TEXT NOT NULL DEFAULT (datetime('now')),
            cloud_pending INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (company_id, field_name)
        );
        CREATE INDEX IF NOT EXISTS idx_company_pers_pending ON company_personalization(cloud_pending) WHERE cloud_pending = 1;
    """)
    for col, col_type in [
        ("original_source", "TEXT"),
        ("original_source_detail", "TEXT"),
        ("original_source_platform", "TEXT"),
        ("original_source_at", "TEXT"),
        ("latest_source", "TEXT"),
        ("latest_source_detail", "TEXT"),
        ("latest_source_platform", "TEXT"),
        ("latest_source_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("current_status_label", "TEXT"),
        ("current_status_sentiment", "TEXT"),
        ("contact_priority", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE workspace_leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("headcount_numeric", "INTEGER"),
        ("location_city", "TEXT"),
        ("location_state", "TEXT"),
        ("location_country", "TEXT"),
        ("email_verification_status", "TEXT"),
        ("email_verified_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("headcount_numeric", "INTEGER"),
        ("hq_city", "TEXT"),
        ("hq_state", "TEXT"),
        ("hq_country", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    # Backfill companies derived from existing leads only after ensuring
    # all referenced company columns exist (e.g. `headcount_numeric`).
    backfill_companies_from_leads(conn)
    for col, col_type in [
        ("latest_sender", "TEXT"),
        ("latest_sender_platform", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE events ADD COLUMN sender TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE workspace_leads ADD COLUMN latest_sender TEXT")
    except sqlite3.OperationalError:
        pass
    for col, col_type in [
        ("email_sent_count", "INTEGER NOT NULL DEFAULT 0"),
        ("linkedin_sent_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_replies_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_contacted_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE workspace_leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """UPDATE workspace_leads
           SET last_contacted_at = last_activity_at
           WHERE last_contacted_at IS NULL AND last_activity_at IS NOT NULL"""
    )
    try:
        conn.execute("ALTER TABLE workspace_leads ADD COLUMN cloud_pending INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_leads_cloud_pending "
        "ON workspace_leads(cloud_pending) WHERE cloud_pending = 1"
    )
    repair_malformed_tags(conn)
    backfill_bounce_events_from_events(conn)
    conn.execute(
        """UPDATE leads SET cloud_pending = 0
           WHERE cloud_pending = 1
             AND (
               original_source_platform = 'relay'
               OR original_source IN ('agent_sync', 'relay_sync')
             )"""
    )
    conn.commit()
    if own_conn:
        conn.close()


def _lead_should_cloud_pending(
    source: Optional[str],
    source_platform: Optional[str],
) -> bool:
    """True when a local change should be pushed to relay on next sync."""
    if source_platform == "relay":
        return False
    if source in ("agent_sync", "relay_sync"):
        return False
    return True


def _mark_lead_cloud_pending(lead_id: int, conn: Optional[sqlite3.Connection] = None) -> None:
    """Flag org-wide lead profile for relay lead_core_update."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.execute(
        "UPDATE leads SET cloud_pending = 1, updated_at = datetime('now') WHERE id = ?",
        (lead_id,),
    )
    if own_conn:
        conn.commit()
        conn.close()


def _mark_workspace_lead_cloud_pending(
    lead_id: int,
    workspace_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Flag workspace overlay for relay lead_workspace_update."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.execute(
        """UPDATE workspace_leads SET cloud_pending = 1, updated_at = datetime('now')
           WHERE lead_id = ? AND workspace_id = ?""",
        (lead_id, workspace_id),
    )
    if own_conn:
        conn.commit()
        conn.close()


def mark_all_lead_snapshots_pending(conn: Optional[sqlite3.Connection] = None) -> None:
    """Queue full snapshot v2 backfill (core + every workspace membership)."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.execute("UPDATE leads SET cloud_pending = 1")
    conn.execute("UPDATE workspace_leads SET cloud_pending = 1")
    if own_conn:
        conn.commit()
        conn.close()


def repair_malformed_tags(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Fix workspace tags stored as list literals (e.g. \"['nace']\" -> \"nace\")."""
    rows = conn.execute(
        "SELECT id, workspace_id, lead_id, tag FROM workspace_lead_tags ORDER BY id"
    ).fetchall()
    fixed_rows = 0
    removed_rows = 0
    inserted_tags = 0
    examples: list[dict] = []

    for row in rows:
        raw_tag = row["tag"] or ""
        parsed = parse_tags_value(raw_tag)
        if len(parsed) == 1 and parsed[0] == normalize_tag(raw_tag):
            continue
        if not parsed:
            removed_rows += 1
            if len(examples) < 5:
                examples.append({"from": raw_tag, "to": []})
            if not dry_run:
                conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["id"],))
            continue

        fixed_rows += 1
        if len(examples) < 5:
            examples.append({"from": raw_tag, "to": parsed})
        if dry_run:
            inserted_tags += len(parsed)
            continue

        conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["id"],))
        for tag in parsed:
            tag_id = (
                f"wlt_{row['workspace_id']}_{row['lead_id']}_"
                f"{hashlib.md5(tag.encode()).hexdigest()[:8]}"
            )
            cur = conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (tag_id, row["workspace_id"], row["lead_id"], tag),
            )
            inserted_tags += cur.rowcount

    return {
        "status": "ok",
        "dry_run": dry_run,
        "rows_fixed": fixed_rows,
        "rows_removed": removed_rows,
        "tags_inserted": inserted_tags,
        "examples": examples,
    }


def backfill_workspace_routing(conn: sqlite3.Connection):
    """Identity aliases for all leads; workspace_leads/maps only in single-workspace mode."""
    ensure_organization(conn)
    config = get_org_routing_config(conn, DEFAULT_ORG_ID)

    leads = conn.execute(
        "SELECT id, email, linkedin_url FROM leads"
    ).fetchall()
    for lead in leads:
        lid = lead["id"]
        if lead["email"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       id, org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, ?, 'email', ?, 'backfill', 1, datetime('now')
                   )""",
                (f"id_email_{lid}", DEFAULT_ORG_ID, lid, lead["email"]),
            )
        if lead["linkedin_url"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       id, org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, ?, 'linkedin_url', ?, 'backfill', 1, datetime('now')
                   )""",
                (f"id_li_{lid}", DEFAULT_ORG_ID, lid, lead["linkedin_url"]),
            )

    if config.mode == WORKSPACE_ROUTING_MULTI:
        return

    workspace_id = config.default_workspace_id or ensure_default_org_workspace(conn)
    for lead in leads:
        lid = lead["id"]
        stage_row = conn.execute("SELECT stage FROM leads WHERE id = ?", (lid,)).fetchone()
        status = stage_row["stage"] if stage_row else "prospecting"
        upsert_workspace_lead(conn, DEFAULT_ORG_ID, workspace_id, lid, status=status)

    campaigns = conn.execute("SELECT name FROM campaigns").fetchall()
    for row in campaigns:
        name = (row["name"] or "").strip()
        if not name:
            continue
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id=workspace_id,
            campaign_name=name,
            match_strategy="name_exact",
        )


def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


def normalize_company_domain(raw: Optional[str]) -> Optional[str]:
    """Normalize a company domain to canonical form: 'acme.com'."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if not text or "." not in text or " " in text or len(text) > 253:
        return None
    return text


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def normalize_event_sender(platform: str, sender: str) -> Optional[str]:
    """Normalize relay sender for storage; None if missing or unknown."""
    raw = (sender or "").strip()
    if not raw or raw.lower() == "unknown":
        return None
    plat = (platform or "").lower()
    if plat in LINKEDIN_PLATFORMS:
        return normalize_linkedin(raw)
    return raw.lower()


def normalize_tag(tag: str) -> str:
    """Lowercase, strip whitespace, collapse internal whitespace."""
    return " ".join(tag.strip().lower().split())


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        norm = normalize_tag(tag)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def parse_tags_value(val) -> list[str]:
    """Parse tags from CSV/JSON/CLI/sync payloads into normalized tag strings."""
    if val is None:
        return []
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            out.extend(parse_tags_value(item))
        return _dedupe_tags(out)
    if isinstance(val, (int, float)):
        val = str(val)
    if not isinstance(val, str):
        val = str(val)
    raw = val.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except (ValueError, SyntaxError):
            pass
        inner = raw[1:-1].strip().strip("'\"")
        if inner and ";" not in inner and "," not in inner:
            norm = normalize_tag(inner)
            return [norm] if norm else []
    return _parse_tags(raw)


def parse_headcount_numeric(raw: Optional[str]) -> Optional[int]:
    """Extract a numeric midpoint from headcount strings like '11-50' or '500+'."""
    if not raw:
        return None
    text = re.sub(r'[^\d\-+]', '', str(raw).strip())
    if not text:
        return None
    range_match = re.match(r'(\d+)-(\d+)', text)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        return (lo + hi) // 2
    plus_match = re.match(r'(\d+)\+?$', text)
    if plus_match:
        return int(plus_match.group(1))
    return None


def furthest_stage(stage_a: str, stage_b: str) -> str:
    def rank(s: str) -> int:
        try:
            return PIPELINE_STAGES.index(s)
        except ValueError:
            return 0
    return stage_a if rank(stage_a) >= rank(stage_b) else stage_b


def ensure_company(
    conn: sqlite3.Connection,
    name: Optional[str] = None,
    domain: Optional[str] = None,
    industry: Optional[str] = None,
    headcount: Optional[str] = None,
    hq_city: Optional[str] = None,
    hq_state: Optional[str] = None,
    hq_country: Optional[str] = None,
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
                                   hq_city=hq_city, hq_state=hq_state, hq_country=hq_country)
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
                                   hq_city=hq_city, hq_state=hq_state, hq_country=hq_country)
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
):
    sets, params = [], []
    if name:
        sets.append("name = CASE WHEN trim(name) = '' THEN ? ELSE name END")
        params.append(name)
    if industry:
        sets.append("industry = COALESCE(industry, ?)")
        params.append(industry)
    if headcount:
        sets.append("headcount = COALESCE(headcount, ?)")
        params.append(headcount)
        hc_num = parse_headcount_numeric(headcount)
        if hc_num is not None:
            sets.append("headcount_numeric = COALESCE(headcount_numeric, ?)")
            params.append(hc_num)
    if hq_city:
        sets.append("hq_city = COALESCE(hq_city, ?)")
        params.append(hq_city)
    if hq_state:
        sets.append("hq_state = COALESCE(hq_state, ?)")
        params.append(hq_state)
    if hq_country:
        sets.append("hq_country = COALESCE(hq_country, ?)")
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
    domain = email_domain(email) if email else None
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


def ensure_lead_domain(lead_id: int, email: Optional[str]):
    domain = email_domain(email)
    if not domain:
        return
    conn = get_conn()
    conn.execute(
        "UPDATE leads SET email_domain = ? WHERE id = ? AND (email_domain IS NULL OR email_domain = '')",
        (domain, lead_id),
    )
    conn.commit()
    conn.close()


def find_lead_by_email(conn: sqlite3.Connection, email: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM leads WHERE email = ?", (email,)).fetchone()
    return row["id"] if row else None


def find_lead_by_linkedin(conn: sqlite3.Connection, linkedin_norm: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM leads WHERE linkedin_url = ?", (linkedin_norm,)
    ).fetchone()
    return row["id"] if row else None


def resolve_workspace_identity(
    conn: sqlite3.Connection,
    workspace: Optional[str],
    *,
    org_id: str = DEFAULT_ORG_ID,
) -> Optional[dict]:
    token = (workspace or "").strip()
    if not token:
        return None
    row = conn.execute(
        """SELECT id, name, slug
           FROM workspaces
           WHERE org_id = ?
             AND (lower(slug) = lower(?) OR lower(name) = lower(?))
           ORDER BY CASE WHEN lower(slug) = lower(?) THEN 0 ELSE 1 END
           LIMIT 1""",
        (org_id, token, token, token),
    ).fetchone()
    return dict(row) if row else None


def find_lead(
    *,
    lead_id: Optional[int] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    name: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[dict]:
    conn = get_conn()
    row = None
    workspace_row = resolve_workspace_identity(conn, workspace)
    if workspace and not workspace_row:
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
    elif name:
        params = [*workspace_params, f"%{name}%"]
        row = conn.execute(
            f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
               FROM leads l LEFT JOIN companies c ON l.company_id = c.id
               {workspace_join}
               WHERE l.name LIKE ? LIMIT 1""",
            tuple(params),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


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


def merge_leads(keep_id: int, merge_id: int, reason: str = "manual") -> dict:
    """Combine two lead rows; merge_id is deleted after moving children."""
    if keep_id == merge_id:
        return {"status": "noop", "keep_id": keep_id}
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        keep = conn.execute("SELECT * FROM leads WHERE id = ?", (keep_id,)).fetchone()
        other = conn.execute("SELECT * FROM leads WHERE id = ?", (merge_id,)).fetchone()
        if not keep or not other:
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
                            "UPDATE workspace_leads SET lead_id = ? WHERE id = ?",
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
        conn.execute(
            "INSERT INTO lead_merges (keep_id, merge_id, reason) VALUES (?, ?, ?)",
            (keep_id, merge_id, reason),
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
               cloud_pending = 1,
               updated_at = datetime('now')
               WHERE id = ?""",
            (
                email, domain, li_merged, company_id,
                company, title, industry, headcount, new_stage, keep_id,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
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

    if not identities:
        return {"status": "error", "error": "no identity: need email, linkedin, external_id, or name+company"}

    own_conn = conn is None
    if own_conn:
        conn = get_conn()

    by_email = find_lead_by_email(conn, email_norm) if email_norm else None
    by_li = find_lead_by_linkedin(conn, li_public) if li_public else None

    lead_id: Optional[int] = None
    created = True
    match_method: Optional[str] = None

    if by_email and by_li and by_email != by_li and auto_merge and not dry_run:
        keep_id, merge_id = _pick_merge_keep_id(conn, by_email, by_li)
        if own_conn:
            conn.close()
        merge_leads(keep_id, merge_id, reason="auto_dual_identifier")
        if own_conn:
            conn = get_conn()
        lead_id = keep_id
        created = False

    for itype, val in identities:
        found = find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val)
        if found:
            if lead_id is None:
                lead_id = found
                match_method = itype
                created = False
            elif lead_id != found and itype in (
                "email", "linkedin_url", "linkedin_sales_nav_id",
                "linkedin_member_id", "external_id",
            ):
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
    mark_pending = _lead_should_cloud_pending(source, source_platform)
    pending_val = 1 if mark_pending else 0

    if created:
        company_id = ensure_company(
            conn, name=company, domain=effective_domain, industry=industry, headcount=headcount,
            hq_city=hq_city, hq_state=hq_state, hq_country=hq_country,
        )
        cur = conn.execute(
            """INSERT INTO leads (name, company_id, company, title, industry, headcount, headcount_numeric,
               email, email_domain, linkedin_url,
               location_city, location_state, location_country,
               channel, stage, notes, cloud_pending,
               original_source, original_source_detail, original_source_platform, original_source_at,
               latest_source, latest_source_detail, latest_source_platform, latest_source_at)
               VALUES (?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                name, company_id, company, title, industry, headcount, parse_headcount_numeric(headcount),
                email_norm, domain_from_email, li_public,
                location_city, location_state, location_country,
                channel, stage, notes, pending_val,
                source, source_detail, source_platform, now_ts,
                source, source_detail, source_platform, now_ts,
            ),
        )
        lead_id = int(cur.lastrowid)
        match_method = match_method or identities[0][0]
        conn.commit()
    else:
        sets, params = [], []
        if email_norm:
            sets.extend(["email = COALESCE(email, ?)", "email_domain = COALESCE(email_domain, ?)"])
            params.extend([email_norm, domain_from_email])
        if li_public:
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
        if mark_pending:
            sets.append("cloud_pending = 1")
        sets.append("updated_at = datetime('now')")
        params.append(lead_id)
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()

    id_conflicts = upsert_all_identities(
        conn, DEFAULT_ORG_ID, int(lead_id), identities, source=source_platform,
    )
    if own_conn:
        conn.commit()
        conn.close()

    name_for_enrich = enrich_name if enrich_name is not None else name
    filled = enrich_lead(
        lead_id, name=name_for_enrich, title=title, industry=industry,
        company=company, headcount=headcount, overwrite=overwrite,
        mark_cloud_pending=mark_pending,
    )
    if email_norm:
        ensure_lead_domain(lead_id, email_norm)
    link_conn = get_conn()
    link_lead_company(link_conn, lead_id, company=company, email=email_norm,
                      industry=industry, headcount=headcount)
    if domain_explicit:
        ensure_company(link_conn, name=company, domain=domain_explicit,
                       industry=industry, headcount=headcount,
                       hq_city=hq_city, hq_state=hq_state, hq_country=hq_country)
    link_conn.commit()
    link_conn.close()

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
                    cloud_pending = 1,
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
    "linkedin": ("linkedin", "linkedin_url", "lead_linkedin_url", "profile_url"),
    "name": ("name", "full_name", "display_name"),
    "title": ("title", "job_title", "role"),
    "company": ("company", "company_name", "organization", "org"),
    "industry": ("industry",),
    "headcount": ("headcount", "company_size", "employees", "employee_count", "company_headcount"),
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


def normalize_profile_row(row: dict) -> dict[str, str]:
    """Map CSV/JSON/webhook-shaped dicts to canonical profile fields."""
    out: dict[str, str] = {}
    for canonical, aliases in PROFILE_ALIASES.items():
        val = _pick_profile_field(row, aliases)
        if val:
            out[canonical] = val
    first = _pick_profile_field(row, ("first_name",))
    last = _pick_profile_field(row, ("last_name",))
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
    mark_cloud_pending: bool = True,
) -> list[str]:
    """Fill empty lead profile fields (won't overwrite non-empty unless overwrite=True)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT name, email, title, industry, company, headcount FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if not row:
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
        if mark_cloud_pending:
            updates.append("cloud_pending = 1")
        updates.append("updated_at = datetime('now')")
        conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", (*params, lead_id))
        conn.commit()
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
    if not idents:
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
    )


IMPORT_EXTRA_FIELDS = (
    "company_domain", "mailmerge_first_name", "mailmerge_company_name",
    "is_connected_linkedin", "is_linkedin_request_pending",
    "lead_status", "lead_sentiment", "import_name", "list_source",
    "tags", "contact_order",
    "hq_city", "hq_state", "hq_country",
    "external_id", "notes",
    "last_message_sent", "last_message_received",
)

RESERVED_IMPORT_FIELDS = frozenset([
    "company_domain", "is_connected_linkedin", "is_linkedin_request_pending",
    "lead_status", "lead_sentiment", "import_name", "list_source",
    "tags", "contact_order", "hq_city", "hq_state", "hq_country",
    "external_id", "notes", "last_message_sent", "last_message_received",
])

def _extract_extra_import_fields(raw: dict) -> dict[str, str]:
    """Extract non-PROFILE_ALIASES fields from the raw CSV/JSON row."""
    out: dict[str, str] = {}
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
        if not key.startswith("mailmerge_"):
            continue
        text = str(val).strip() if val is not None else ""
        if text:
            out[key] = text
    if not out.get("external_id"):
        ext = pick_external_id_from_raw(raw)
        if ext:
            out["external_id"] = ext
    return out


def _parse_tags(raw_tags: str) -> list[str]:
    """Parse semicolon or comma-separated tags into a deduplicated list."""
    tags: list[str] = []
    seen: set[str] = set()
    for sep in (";", ","):
        if sep in raw_tags:
            for t in raw_tags.split(sep):
                norm = normalize_tag(t)
                if norm and norm not in seen:
                    tags.append(norm)
                    seen.add(norm)
            return tags
    norm = normalize_tag(raw_tags)
    if norm:
        return [norm]
    return []


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
    source_detail: Optional[str] = None,
    import_batch_id: Optional[str] = None,
) -> dict:
    """Import many profile rows (CSV dicts or JSON objects). Tiered identity match keys."""
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
        "errors": [],
        "results": [],
        "skipped_features": [],
    }

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
            if key.startswith("mailmerge_") and str(rows[0].get(key) or "").strip():
                field = key[len("mailmerge_"):]
                personalize_columns_detected.append(f"{key} -> {field}")
    if personalize_columns_detected and dry_run:
        summary["personalization_detected"] = personalize_columns_detected

    for i, raw in enumerate(rows):
        profile = normalize_profile_row(raw)
        extra = _extract_extra_import_fields(raw)
        row_company_domain = normalize_company_domain(extra.get("company_domain"))
        row_notes = extra.get("notes") or notes
        idents = build_import_identities(
            profile, extra, import_batch=import_batch_id, company_domain=row_company_domain,
        )
        if not idents:
            summary["skipped_no_identity"] += 1
            summary["errors"].append({"row": i + 1, "error": "no identity"})
            continue
        summary["processed"] += 1

        row_source_detail = extra.get("import_name") or extra.get("list_source") or source_detail
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
                source="csv_import",
                source_detail=row_source_detail,
                source_platform="csv",
                hq_city=row_hq_city,
                hq_state=row_hq_state,
                hq_country=row_hq_country,
                import_batch=import_batch_id,
                import_extra=extra,
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

        if dry_run:
            continue

        lead_id = result["id"]

        lead_items = []
        co_items = []
        for key, val in extra.items():
            if not val:
                continue
            
            field = None
            if key.startswith("mailmerge_"):
                field = key[len("mailmerge_"):]
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

    return summary


# ──────────────────────────────────────────────────────────────────────
# Tag CRUD (workspace-scoped)
# ──────────────────────────────────────────────────────────────────────

def tag_add(workspace_id: str, lead_id: int, tag: str) -> dict:
    """Add a tag to a lead in a workspace."""
    parsed = parse_tags_value(tag)
    if len(parsed) > 1:
        results = [tag_add(workspace_id, lead_id, t) for t in parsed]
        return {"status": "added", "tags": [r.get("tag") for r in results], "lead_id": lead_id}
    if not parsed:
        return {"status": "error", "error": "empty tag"}
    tag = parsed[0]
    tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, ?)""",
            (tag_id, workspace_id, lead_id, tag),
        )
        conn.commit()
        return {"status": "added", "tag": tag, "lead_id": lead_id}
    except sqlite3.IntegrityError:
        return {"status": "exists", "tag": tag, "lead_id": lead_id}
    finally:
        conn.close()


def tag_remove(workspace_id: str, lead_id: int, tag: str) -> dict:
    """Remove a tag from a lead in a workspace."""
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? AND tag = ?",
        (workspace_id, lead_id, normalize_tag(tag)),
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        return {"status": "removed", "tag": tag, "lead_id": lead_id}
    return {"status": "not_found", "tag": tag, "lead_id": lead_id}


def tag_set(workspace_id: str, lead_id: int, tags: list[str]) -> dict:
    """Replace all tags for a lead in a workspace."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
        (workspace_id, lead_id),
    )
    added = []
    for tag in tags:
        tag = normalize_tag(tag)
        if not tag:
            continue
        tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, ?)""",
            (tag_id, workspace_id, lead_id, tag),
        )
        added.append(tag)
    conn.commit()
    conn.close()
    return {"status": "set", "tags": added, "lead_id": lead_id}


def tag_list(workspace_id: str, lead_id: Optional[int] = None) -> list[dict]:
    """List tags for a workspace, optionally filtered by lead_id."""
    conn = get_conn()
    if lead_id:
        rows = conn.execute(
            "SELECT tag, lead_id, created_at FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at",
            (workspace_id, lead_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT tag, COUNT(*) as lead_count
               FROM workspace_lead_tags WHERE workspace_id = ?
               GROUP BY tag ORDER BY lead_count DESC""",
            (workspace_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _sender_slug_from_profile(sender_profile: str) -> str:
    """Short handle from a stored LinkedIn sender profile URL."""
    sp = (sender_profile or "").strip().rstrip("/")
    if not sp:
        return "(unknown)"
    norm = normalize_linkedin(sp) or sp
    if "/in/" in norm:
        return norm.split("/in/")[-1].split("?")[0]
    parts = [p for p in norm.split("/") if p]
    return parts[-1] if parts else norm


def linkedin_status_summary(workspace_id: str) -> dict:
    """Aggregate LinkedIn connection state by sender for a workspace."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT sender_profile,
                  SUM(CASE WHEN is_connected = 1 THEN 1 ELSE 0 END) AS connected,
                  SUM(CASE WHEN is_request_pending = 1 THEN 1 ELSE 0 END) AS pending
           FROM workspace_lead_linkedin_status
           WHERE workspace_id = ?
           GROUP BY sender_profile
           ORDER BY connected DESC, pending DESC, sender_profile""",
        (workspace_id,),
    ).fetchall()
    connected_leads = conn.execute(
        """SELECT COUNT(DISTINCT lead_id) FROM workspace_lead_linkedin_status
           WHERE workspace_id = ? AND is_connected = 1""",
        (workspace_id,),
    ).fetchone()[0]
    conn.close()
    senders = []
    for row in rows:
        profile = row["sender_profile"] or ""
        senders.append({
            "sender_profile": profile,
            "sender_slug": _sender_slug_from_profile(profile),
            "connected": int(row["connected"] or 0),
            "pending": int(row["pending"] or 0),
        })
    return {
        "linkedin_senders": senders,
        "linkedin_connected_leads": int(connected_leads or 0),
    }


def get_workspace_summary(workspace: str) -> dict:
    """Workspace inventory: lead count, tags, LinkedIn sender connection aggregates."""
    conn = get_conn()
    try:
        ws_row = resolve_workspace_identity(conn, workspace)
        if not ws_row:
            return {"error": f"workspace not found: {workspace}"}
        ws_id = ws_row["id"]
        lead_count = conn.execute(
            "SELECT COUNT(*) FROM workspace_leads WHERE workspace_id = ?",
            (ws_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    li_summary = linkedin_status_summary(ws_id)
    cfg = load_config()
    return {
        "workspace": ws_row["slug"],
        "workspace_name": ws_row["name"],
        "lead_count": int(lead_count or 0),
        "last_pull": cfg.get("last_pull"),
        "tags": tag_list(ws_id),
        **li_summary,
    }


def format_workspace_summary(summary: dict) -> str:
    if summary.get("error"):
        return str(summary["error"])
    lines = [
        f"Workspace: {summary.get('workspace_name')} ({summary.get('workspace')})",
        f"Leads: {summary.get('lead_count', 0)}",
        f"Data as of last_pull: {summary.get('last_pull') or '(never)'}",
        f"LinkedIn connected leads (any sender): {summary.get('linkedin_connected_leads', 0)}",
        "",
        "Tags:",
    ]
    tags = summary.get("tags") or []
    if not tags:
        lines.append("  (none)")
    else:
        tag_w = max(len("Tag"), max((len(t.get("tag") or "") for t in tags), default=3))
        lines.append(f"  {'Tag':<{tag_w}}  {'Leads':>7}")
        lines.append(f"  {'-' * tag_w}  {'-' * 7}")
        for row in tags:
            lines.append(f"  {row.get('tag', ''):<{tag_w}}  {int(row.get('lead_count') or 0):>7}")
    lines.extend(["", "LinkedIn senders:"])
    senders = summary.get("linkedin_senders") or []
    if not senders:
        lines.append("  (none)")
    else:
        slug_w = max(len("Sender"), max((len(s.get("sender_slug") or "") for s in senders), default=6))
        lines.append(f"  {'Sender':<{slug_w}}  {'Connected':>10}  {'Pending':>8}")
        lines.append(f"  {'-' * slug_w}  {'-' * 10}  {'-' * 8}")
        for row in senders:
            lines.append(
                f"  {row.get('sender_slug', ''):<{slug_w}}  "
                f"{int(row.get('connected') or 0):>10}  {int(row.get('pending') or 0):>8}"
            )
    return "\n".join(lines)


def tag_bulk(workspace_id: str, lead_ids: list[int], tags: list[str], *, remove: bool = False) -> dict:
    """Add or remove tags in bulk across multiple leads."""
    conn = get_conn()
    changed = 0
    for lead_id in lead_ids:
        for tag in tags:
            tag = normalize_tag(tag)
            if not tag:
                continue
            if remove:
                cur = conn.execute(
                    "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? AND tag = ?",
                    (workspace_id, lead_id, tag),
                )
                changed += cur.rowcount
            else:
                tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
                try:
                    conn.execute(
                        """INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                           VALUES (?, ?, ?, ?)""",
                        (tag_id, workspace_id, lead_id, tag),
                    )
                    changed += 1
                except sqlite3.IntegrityError:
                    pass
    conn.commit()
    conn.close()
    action = "removed" if remove else "added"
    return {"status": action, "changed": changed, "leads": len(lead_ids), "tags": tags}



def load_profile_rows_from_file(path: Path) -> list[dict]:
    """Load rows from a .csv file or a .json / .jsonl file."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError("JSON file must be an array of objects or a single object")


def update_lead_stage(lead_id, stage, next_action=None, event_at=None):
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Valid: {PIPELINE_STAGES}")
    ts_expr = "?" if event_at else "datetime('now')"
    conn = get_conn()
    conn.execute(
        f"""UPDATE leads SET stage = ?, cloud_pending = 1, updated_at = {ts_expr},
           next_action = CASE WHEN ? IS NOT NULL THEN ? ELSE next_action END WHERE id = ?""",
        (stage, event_at, next_action, next_action, lead_id) if event_at
        else (stage, next_action, next_action, lead_id),
    )
    conn.commit()
    conn.close()

def ensure_campaign(conn, name: str, lead_id: int) -> int:
    """Return campaign id, creating the row and campaign_leads link if needed."""
    row = conn.execute("SELECT id FROM campaigns WHERE name = ?", (name,)).fetchone()
    if row:
        campaign_id = row["id"]
    else:
        campaign_id = conn.execute("INSERT INTO campaigns (name) VALUES (?)", (name,)).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
        (campaign_id, lead_id),
    )
    return campaign_id


def backfill_campaigns_from_events(conn=None):
    """Populate campaigns from event metadata_json for rows missing campaign_id."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    rows = conn.execute(
        """SELECT id, lead_id, metadata_json FROM events
           WHERE campaign_id IS NULL AND metadata_json IS NOT NULL AND metadata_json != '{}'"""
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        campaign = meta.get("campaign")
        if not campaign or not str(campaign).strip():
            continue
        campaign_id = ensure_campaign(conn, str(campaign).strip(), row["lead_id"])
        conn.execute("UPDATE events SET campaign_id = ? WHERE id = ?", (campaign_id, row["id"]))
    if own_conn:
        conn.commit()
        conn.close()


def backfill_plusvibe_status_metadata(conn=None):
    """Repair mismatched PlusVibe status label/sentiment from explicit webhook event type."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    rows = conn.execute(
        """SELECT id, metadata_json
           FROM events
           WHERE metadata_json IS NOT NULL
             AND metadata_json != '{}'
             AND lower(json_extract(metadata_json, '$.platform')) = 'plusvibe'
             AND lower(json_extract(metadata_json, '$.plusvibe_webhook_event')) IN (
                'lead_marked_as_interested',
                'lead_marked_as_not_interested'
             )"""
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        et = str(meta.get("plusvibe_webhook_event") or "").strip().lower()
        if et == "lead_marked_as_interested":
            wanted_label, wanted_sentiment = "interested", "positive"
        elif et == "lead_marked_as_not_interested":
            wanted_label, wanted_sentiment = "not_interested", "negative"
        else:
            continue
        changed = False
        if meta.get("lead_status_raw") != wanted_label:
            meta["lead_status_raw"] = wanted_label
            meta["lead_status_display"] = normalize_lead_status_display(wanted_label)
            changed = True
        if str(meta.get("lead_status_sentiment") or "").strip().lower() != wanted_sentiment:
            meta["lead_status_sentiment"] = wanted_sentiment
            changed = True
        if changed:
            conn.execute(
                "UPDATE events SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta), row["id"]),
            )
    if own_conn:
        conn.commit()
        conn.close()


def cap_event_body(body: str) -> tuple[str, bool]:
    """Truncate stored event body to MAX_EVENT_BODY_STORAGE_CHARS. Returns (text, was_truncated)."""
    if not body:
        return "", False
    limit = MAX_EVENT_BODY_STORAGE_CHARS
    if len(body) <= limit:
        return body, False
    return body[:limit], True


def _prepare_stored_event_body(meta: dict, body_preview: Optional[str]) -> str:
    """Normalize HTML bodies, cap length, and derive body_preview for events row."""
    preview = (body_preview or "")[:200]
    if meta.get("body"):
        raw_body = str(meta["body"])
        plain, was_html = normalize_event_body_for_storage(raw_body)
        if was_html:
            meta["body_was_html"] = True
            meta["body_original_length"] = len(raw_body)
        pre_cap_len = len(plain)
        capped, truncated = cap_event_body(plain)
        meta["body"] = capped
        if truncated:
            meta["body_truncated"] = True
            if not was_html:
                meta["body_original_length"] = pre_cap_len
        preview = capped[:200]
    elif looks_like_html(preview):
        preview = strip_html_reply(preview, max_len=200)
    return preview


def log_event(lead_id, event_type, direction="outbound", channel="email",
              subject=None, body_preview=None, metadata=None, campaign=None,
              event_at=None, sender=None):
    meta = dict(metadata or {})
    preview = _prepare_stored_event_body(meta, body_preview)
    campaign_name = campaign or meta.get("campaign")
    conn = get_conn()
    campaign_id = None
    if campaign_name and str(campaign_name).strip():
        campaign_id = ensure_campaign(conn, str(campaign_name).strip(), lead_id)
    created = event_at or None
    if created:
        conn.execute(
            """INSERT INTO events (
                   lead_id, event_type, direction, channel, subject, body_preview,
                   metadata_json, campaign_id, sender, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, event_type, direction, channel, subject, preview,
             json.dumps(meta), campaign_id, sender, created),
        )
        conn.execute(
            """UPDATE leads SET updated_at = ?, last_contact_at = ?
               WHERE id = ? AND (last_contact_at IS NULL OR last_contact_at < ?)""",
            (created, created, lead_id, created),
        )
    else:
        conn.execute(
            """INSERT INTO events (
                   lead_id, event_type, direction, channel, subject, body_preview,
                   metadata_json, campaign_id, sender
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, event_type, direction, channel, subject, preview,
             json.dumps(meta), campaign_id, sender),
        )
        conn.execute(
            "UPDATE leads SET updated_at = datetime('now'), last_contact_at = datetime('now') WHERE id = ?",
            (lead_id,),
        )
    event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    refresh_lead_activity_for_lead(lead_id)
    return event_id


def _update_lead_sender(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_id: Optional[str],
    sender: str,
    platform: str,
    event_at: str,
) -> None:
    conn.execute(
        """UPDATE leads SET latest_sender = ?, latest_sender_platform = ?, updated_at = ?
           WHERE id = ?""",
        (sender, platform, event_at, lead_id),
    )
    if workspace_id:
        conn.execute(
            """UPDATE workspace_leads SET latest_sender = ?, updated_at = ?
               WHERE workspace_id = ? AND lead_id = ?""",
            (sender, event_at, workspace_id, lead_id),
        )

def get_lead_events(lead_id, limit=50):
    """Get all events for a lead, newest first."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, event_type, direction, channel, subject, body_preview,
                  metadata_json, sender, created_at
           FROM events WHERE lead_id = ? ORDER BY created_at DESC LIMIT ?""",
        (lead_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _decode_event_metadata(raw_meta) -> dict:
    if not raw_meta:
        return {}
    try:
        return json.loads(raw_meta)
    except (json.JSONDecodeError, TypeError):
        return {}


def _event_subject_and_body(event: dict) -> tuple[str, str]:
    meta = _decode_event_metadata(event.get("metadata_json"))
    subject = (event.get("subject") or "").strip()
    body = (meta.get("body") or event.get("body_preview") or "").strip()
    return subject, body


def _anonymize_template_text(text: str, lead: dict) -> str:
    out = text or ""

    # Normalize common greeting/sign-off personalization so template grouping
    # is not split by sender names.
    out = re.sub(r"(?im)^hi\s+[^,\n]{1,60},", "Hi [first_name],", out)
    out = re.sub(r"(?im)^best,\s*$", "Best,", out)
    out = re.sub(r"(?im)^(best,\s*\n)[^\n]+", r"\1[sender]", out)

    replacements = [
        (lead.get("name") or "", "[name]"),
        ((lead.get("name") or "").split(" ")[0] if lead.get("name") else "", "[first_name]"),
        (lead.get("email") or "", "[email]"),
        (lead.get("company_display") or lead.get("company") or "", "[company]"),
    ]
    for original, token in replacements:
        original = (original or "").strip()
        if not original:
            continue
        escaped = re.escape(original)
        if len(original) <= 3 and re.fullmatch(r"[A-Za-z0-9 _.-]+", original):
            pattern = rf"\b{escaped}\b"
        else:
            pattern = escaped
        out = re.sub(pattern, token, out, flags=re.IGNORECASE)
    return out


def _normalize_for_signature(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _template_signature(subject: str, body: str) -> str:
    data = f"{_normalize_for_signature(subject)}\n{_normalize_for_signature(body)}"
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:12]


def _first_touch_email_events_for_leads(conn, lead_ids: list[int]) -> dict[int, dict]:
    if not lead_ids:
        return {}
    placeholders = ",".join("?" for _ in lead_ids)
    rows = conn.execute(
        f"""SELECT e.id, e.lead_id, e.subject, e.body_preview, e.metadata_json, e.created_at,
                   l.name, l.email, l.company,
                   COALESCE(co.name, l.company) AS company_display
            FROM events e
            JOIN leads l ON l.id = e.lead_id
            LEFT JOIN companies co ON co.id = l.company_id
            WHERE e.lead_id IN ({placeholders})
              AND lower(e.channel) = 'email'
              AND lower(e.direction) = 'outbound'
              AND lower(e.event_type) = 'email_sent'
            ORDER BY e.lead_id ASC, e.created_at ASC, e.id ASC""",
        lead_ids,
    ).fetchall()
    first_events: dict[int, dict] = {}
    for row in rows:
        event = dict(row)
        lead_id = event["lead_id"]
        if lead_id in first_events:
            continue
        subject, body = _event_subject_and_body(event)
        if not subject and not body:
            continue
        first_events[lead_id] = event
    return first_events


def get_copy_insights(
    lead_status: str = "interested",
    limit: int = 200,
    workspace: Optional[str] = None,
) -> dict:
    """Analyze winning copy from current positive leads.

    Uses current lead status filter for "positives" and scores templates using the
    first outbound email sent to each lead (positive-hit count and hit rate).
    """
    positive_leads = get_pipeline(
        limit=limit,
        lead_status=lead_status,
        sort="updated_at",
        order="desc",
        workspace=workspace,
    )
    positive_by_id = {int(lead["id"]): lead for lead in positive_leads}
    positive_ids = sorted(positive_by_id.keys())

    conn = get_conn()
    workspace_row = resolve_workspace_identity(conn, workspace)
    if workspace and not workspace_row:
        conn.close()
        raise ValueError(f"workspace not found: {workspace}")
    if workspace_row:
        all_lead_rows = conn.execute(
            """SELECT l.id
               FROM leads l
               INNER JOIN workspace_leads wl ON wl.lead_id = l.id
               WHERE wl.workspace_id = ?""",
            (workspace_row["id"],),
        ).fetchall()
    else:
        all_lead_rows = conn.execute("SELECT id FROM leads").fetchall()
    all_lead_ids = [int(r["id"]) for r in all_lead_rows]

    first_touch_all = _first_touch_email_events_for_leads(conn, all_lead_ids)
    first_touch_positive = _first_touch_email_events_for_leads(conn, positive_ids)
    conn.close()

    template_stats: dict[str, dict] = {}
    lead_template_by_id: dict[int, str] = {}

    for lead_id, event in first_touch_all.items():
        lead_row = {
            "name": event.get("name"),
            "email": event.get("email"),
            "company": event.get("company"),
            "company_display": event.get("company_display"),
        }
        subject, body = _event_subject_and_body(event)
        anon_subject = _anonymize_template_text(subject, lead_row)
        anon_body = _anonymize_template_text(body, lead_row)
        sig = _template_signature(anon_subject, anon_body)
        bucket = template_stats.setdefault(
            sig,
            {
                "template_id": sig,
                "subject_template": anon_subject,
                "body_template": anon_body,
                "total_leads": 0,
                "positive_leads": 0,
                "positive_rate": 0.0,
            },
        )
        bucket["total_leads"] += 1
        lead_template_by_id[lead_id] = sig
        if lead_id in positive_by_id:
            bucket["positive_leads"] += 1

    for row in template_stats.values():
        total = row["total_leads"] or 1
        row["positive_rate"] = round(row["positive_leads"] / total, 4)

    ranked_templates = sorted(
        template_stats.values(),
        key=lambda r: (r["positive_leads"], r["positive_rate"], r["total_leads"]),
        reverse=True,
    )

    positive_copy = []
    for lead in positive_leads:
        lead_id = int(lead["id"])
        event = first_touch_positive.get(lead_id)
        if not event:
            continue
        subject, body = _event_subject_and_body(event)
        template_id = lead_template_by_id.get(lead_id)
        positive_copy.append(
            {
                "lead_id": lead_id,
                "lead_name": lead.get("name"),
                "lead_status": lead_status,
                "stage": lead.get("stage"),
                "event_id": event.get("id"),
                "sent_at": event.get("created_at"),
                "subject": subject,
                "body": body,
                "template_id": template_id,
            }
        )

    return {
        "filter": {"lead_status": lead_status, "limit": limit, "workspace": workspace},
        "counts": {
            "positive_leads": len(positive_leads),
            "positive_with_copy": len(positive_copy),
            "templates_seen": len(ranked_templates),
        },
        "positive_leads_copy": positive_copy,
        "templates_ranked": ranked_templates,
        "best_template": ranked_templates[0] if ranked_templates else None,
    }

def _normalize_segment_value(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _parse_segment_fields(raw_fields: Optional[str]) -> list[str]:
    if not raw_fields:
        return list(ATTRIBUTE_INSIGHT_FIELDS)
    out: list[str] = []
    for chunk in raw_fields.split(","):
        key = (chunk or "").strip().lower()
        if not key:
            continue
        if key not in ATTRIBUTE_INSIGHT_FIELDS:
            raise ValueError(
                f"invalid field '{key}'. Allowed: {', '.join(ATTRIBUTE_INSIGHT_FIELDS)}"
            )
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("no valid fields provided")
    return out


def get_segment_insights(
    *,
    positive_lead_status: Optional[str] = "interested",
    positive_sentiment: Optional[str] = None,
    fields: Optional[str] = None,
    min_sent: int = 2,
    top: int = 12,
    workspace: Optional[str] = None,
) -> dict:
    """Find best converting lead segments (title/industry/headcount)."""
    if min_sent < 1:
        raise ValueError("min_sent must be >= 1")
    if top < 1:
        raise ValueError("top must be >= 1")
    selected_fields = _parse_segment_fields(fields)

    conn = get_conn()
    workspace_row = resolve_workspace_identity(conn, workspace)
    if workspace and not workspace_row:
        conn.close()
        raise ValueError(f"workspace not found: {workspace}")

    workspace_join = ""
    workspace_filter_sql = ""
    workspace_params: list = []
    if workspace_row:
        workspace_join = "INNER JOIN workspace_leads wl ON wl.lead_id = l.id"
        workspace_filter_sql = " AND wl.workspace_id = ?"
        workspace_params.append(workspace_row["id"])

    field_select = ", ".join(f"l.{field} AS {field}" for field in selected_fields)
    sent_rows = conn.execute(
        f"""SELECT DISTINCT l.id, {field_select}
            FROM leads l
            {workspace_join}
            WHERE EXISTS (
                SELECT 1
                FROM events e
                WHERE e.lead_id = l.id
                  AND lower(e.channel) = 'email'
                  AND lower(e.direction) = 'outbound'
                  AND lower(e.event_type) = 'email_sent'
            ){workspace_filter_sql}""",
        workspace_params,
    ).fetchall()

    positive_clauses: list[str] = []
    positive_params: list = []
    if positive_lead_status:
        positive_clauses.append("lower(COALESCE(rs.current_lead_status_raw, '')) = lower(?)")
        positive_params.append(positive_lead_status)
    if positive_sentiment:
        positive_clauses.append("lower(COALESCE(rs.current_sentiment, '')) = lower(?)")
        positive_params.append(positive_sentiment)
    if not positive_clauses:
        positive_clauses.append("1 = 1")
    positive_where_sql = " AND ".join(positive_clauses)

    positive_id_rows = conn.execute(
        _LATEST_STATUS_CTE
        + f"""
        SELECT DISTINCT rs.lead_id
        FROM ranked_status rs
        JOIN leads l ON l.id = rs.lead_id
        {workspace_join}
        WHERE rs.rn = 1
          AND {positive_where_sql}
          {workspace_filter_sql}
        """,
        positive_params + workspace_params,
    ).fetchall()
    conn.close()

    positive_ids = {int(row["lead_id"]) for row in positive_id_rows}
    sent_ids = {int(row["id"]) for row in sent_rows}
    positive_sent_ids = sent_ids.intersection(positive_ids)

    insights_by_field: dict[str, list[dict]] = {}
    for field in selected_fields:
        buckets: dict[str, dict] = {}
        for row in sent_rows:
            lead_id = int(row["id"])
            value = _normalize_segment_value(row[field])
            if not value:
                continue
            key = value.lower()
            bucket = buckets.setdefault(
                key,
                {
                    "value": value,
                    "sent_leads": 0,
                    "positive_leads": 0,
                    "conversion_rate": 0.0,
                },
            )
            bucket["sent_leads"] += 1
            if lead_id in positive_ids:
                bucket["positive_leads"] += 1

        ranked: list[dict] = []
        for item in buckets.values():
            sent_total = int(item["sent_leads"] or 0)
            if sent_total < min_sent:
                continue
            positive_total = int(item["positive_leads"] or 0)
            item["conversion_rate"] = round(positive_total / sent_total, 4)
            ranked.append(item)

        ranked.sort(
            key=lambda item: (
                float(item["conversion_rate"]),
                int(item["positive_leads"]),
                int(item["sent_leads"]),
                (item["value"] or "").lower(),
            ),
            reverse=True,
        )
        insights_by_field[field] = ranked[:top]

    recommended_titles = [
        row["value"] for row in insights_by_field.get("title", []) if row.get("value")
    ]

    return {
        "filter": {
            "positive_lead_status": positive_lead_status,
            "positive_sentiment": positive_sentiment,
            "fields": selected_fields,
            "min_sent": min_sent,
            "top": top,
            "workspace": workspace,
        },
        "counts": {
            "sent_leads": len(sent_ids),
            "positive_leads_matching_filter": len(positive_ids),
            "positive_leads_with_sent_email": len(positive_sent_ids),
        },
        "insights_by_field": insights_by_field,
        "recommended_job_titles": recommended_titles,
    }


# Events that carry lead status / sentiment / auto-reply for current-state filters.
_STATUS_METADATA_PREDICATE = """(
    json_extract(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
    OR json_extract(e.metadata_json, '$.lead_status_raw') IS NOT NULL
    OR CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
)"""

_LATEST_STATUS_CTE = f"""
WITH ranked_status AS (
  SELECT
    e.lead_id,
    lower(json_extract(e.metadata_json, '$.lead_status_sentiment')) AS current_sentiment,
    json_extract(e.metadata_json, '$.lead_status_raw') AS current_lead_status_raw,
    json_extract(e.metadata_json, '$.lead_status_display') AS current_lead_status_display,
    CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) AS current_is_auto_reply,
    e.created_at AS status_at,
    ROW_NUMBER() OVER (
      PARTITION BY e.lead_id
      ORDER BY e.created_at DESC, e.id DESC
    ) AS rn
  FROM events e
  WHERE {_STATUS_METADATA_PREDICATE}
)
"""


def get_pipeline(
    stage_filter=None,
    limit=50,
    sentiment=None,
    auto_reply=None,
    lead_status=None,
    sort="updated_at",
    order="desc",
    workspace: Optional[str] = None,
    since: Optional[str] = None,
    email: Optional[str] = None,
    name: Optional[str] = None,
):
    """List leads; optional filters use latest status-bearing event per lead (current-only)."""
    conn = get_conn()
    order = (order or "desc").lower()
    if order not in ("asc", "desc"):
        order = "desc"
    sort_key = (sort or "updated_at").lower()
    use_status_join = (
        sentiment is not None
        or auto_reply is not None
        or lead_status is not None
        or sort_key in ("sentiment", "auto_reply", "status_at")
    )
    workspace_row = resolve_workspace_identity(conn, workspace)
    if workspace and not workspace_row:
        conn.close()
        raise ValueError(f"workspace not found: {workspace}")
    workspace_join = ""
    workspace_filter_sql = ""
    workspace_params: list = []
    if workspace_row:
        workspace_join = "INNER JOIN workspace_leads wl ON wl.lead_id = l.id"
        workspace_filter_sql = " AND wl.workspace_id = ?"
        workspace_params.append(workspace_row["id"])

    company_join = "LEFT JOIN companies co ON l.company_id = co.id"
    company_col = "COALESCE(co.name, l.company) AS company_display"
    if use_status_join:
        query = _LATEST_STATUS_CTE + f"""
        SELECT l.*, {company_col},
               rs.current_sentiment,
               rs.current_lead_status_raw,
               rs.current_lead_status_display,
               rs.current_is_auto_reply,
               rs.status_at,
               (SELECT event_type FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) AS last_event,
               (SELECT created_at FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) AS last_event_at,
               (SELECT COUNT(*) FROM events WHERE lead_id = l.id) AS event_count
        FROM leads l
        {workspace_join}
        {company_join}
        INNER JOIN ranked_status rs ON rs.lead_id = l.id AND rs.rn = 1
        WHERE 1=1
        {workspace_filter_sql}
        """
    else:
        query = f"""
        SELECT l.*, {company_col},
               NULL AS current_sentiment,
               NULL AS current_lead_status_raw,
               NULL AS current_lead_status_display,
               NULL AS current_is_auto_reply,
               NULL AS status_at,
               (SELECT event_type FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) AS last_event,
               (SELECT created_at FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) AS last_event_at,
               (SELECT COUNT(*) FROM events WHERE lead_id = l.id) AS event_count
        FROM leads l
        {workspace_join}
        {company_join}
        WHERE 1=1
        {workspace_filter_sql}
        """
    params: list = [*workspace_params]
    if stage_filter:
        query += " AND l.stage = ?"
        params.append(stage_filter)
    if sentiment:
        query += " AND rs.current_sentiment = ?"
        params.append(sentiment.lower())
    if auto_reply is not None:
        want = 1 if auto_reply in (True, 1, "1", "true", "yes") else 0
        query += " AND rs.current_is_auto_reply = ?"
        params.append(want)
    if lead_status:
        query += (
            " AND (lower(rs.current_lead_status_raw) = lower(?) "
            "OR lower(rs.current_lead_status_display) = lower(?))"
        )
        params.extend([lead_status, lead_status.replace("_", " ")])

    if since:
        since_date = since.strip()
        if since_date.lower() == "today":
            since_date = datetime.now().strftime("%Y-%m-%d")
        query += " AND (l.created_at >= ? OR l.updated_at >= ?)"
        params.extend([since_date, since_date])

    if email:
        em = normalize_email(email)
        if em:
            query += " AND l.email = ?"
            params.append(em)

    if name:
        query += " AND l.name LIKE ?"
        params.append(f"%{name}%")

    order_sql = {
        "updated_at": f"l.updated_at {order.upper()}",
        "sentiment": f"rs.current_sentiment {order.upper()}, l.updated_at DESC",
        "auto_reply": f"rs.current_is_auto_reply {order.upper()}, l.updated_at DESC",
        "status_at": f"rs.status_at {order.upper()}",
    }.get(sort_key, f"l.updated_at {order.upper()}")
    query += f" ORDER BY {order_sql} LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def enrich_lead_rows(
    leads: list[dict],
    *,
    workspace: Optional[str] = None,
) -> list[dict]:
    """Attach personalization, tags, sender, and sync snapshot fields for JSON/export."""
    if not leads:
        return []
    conn = get_conn()
    ws_slug = workspace
    try:
        if workspace:
            ws_row = resolve_workspace_identity(conn, workspace)
            ws_slug = ws_row["slug"] if ws_row else workspace
        enriched: list[dict] = []
        for lead in leads:
            row = dict(lead)
            snap = build_lead_sync_payload(
                conn, DEFAULT_ORG_ID, int(lead["id"]), workspace_slug=ws_slug,
            )
            for key in (
                "tags", "linkedin_status",
                "latest_sender", "latest_sender_platform", "linkedin",
                "lead_status", "lead_sentiment", "contact_order", "workspace_stage",
                "external_id", "company_domain", "hq_city", "hq_state", "hq_country",
                "activity",
            ):
                if key in snap and snap[key] is not None:
                    row[key] = snap[key]
            activity = snap.get("activity") or {}
            if activity:
                row["last_contacted_at"] = activity.get("last_contacted_at") or row.get("last_contact_at")
                row["email_sent_count"] = activity.get("email_sent_count", 0)
                row["linkedin_sent_count"] = activity.get("linkedin_sent_count", 0)
                row["total_replies_count"] = activity.get("total_replies_count", 0)
                row["total_contacted_count"] = activity.get("total_contacted_count", 0)
            row["personalization"] = resolve_personalization(int(lead["id"]))
            if not row.get("latest_sender") and lead.get("latest_sender"):
                row["latest_sender"] = lead["latest_sender"]
            if not row.get("latest_sender_platform") and lead.get("latest_sender_platform"):
                row["latest_sender_platform"] = lead["latest_sender_platform"]
            enriched.append(row)
        return enriched
    finally:
        conn.close()


_EXPORT_CSV_BASE_COLUMNS = [
    "email", "linkedin", "name", "company", "title", "industry", "headcount",
    "stage", "notes", "location_city", "location_state", "location_country",
    "hq_city", "hq_state", "hq_country", "company_domain",
    "workspace_stage", "lead_status", "lead_sentiment", "contact_order",
    "latest_sender", "latest_sender_platform", "tags",
    "external_id", "event_count", "last_event", "last_event_at",
    "last_contacted_at", "email_sent_count", "linkedin_sent_count",
    "total_replies_count", "total_contacted_count",
]


def _flatten_lead_for_csv(lead: dict) -> dict:
    """Flatten enrich_lead_rows output for CSV export."""
    row: dict = {}
    company = lead.get("company_display") or lead.get("company")
    row["email"] = lead.get("email") or ""
    row["linkedin"] = lead.get("linkedin") or lead.get("linkedin_url") or ""
    row["name"] = lead.get("name") or ""
    row["company"] = company or ""
    row["title"] = lead.get("title") or ""
    row["industry"] = lead.get("industry") or ""
    row["headcount"] = lead.get("headcount") or ""
    row["stage"] = lead.get("stage") or ""
    row["notes"] = lead.get("notes") or ""
    row["location_city"] = lead.get("location_city") or ""
    row["location_state"] = lead.get("location_state") or ""
    row["location_country"] = lead.get("location_country") or ""
    row["hq_city"] = lead.get("hq_city") or ""
    row["hq_state"] = lead.get("hq_state") or ""
    row["hq_country"] = lead.get("hq_country") or ""
    row["company_domain"] = lead.get("company_domain") or ""
    row["workspace_stage"] = lead.get("workspace_stage") or ""
    row["lead_status"] = lead.get("lead_status") or ""
    row["lead_sentiment"] = lead.get("lead_sentiment") or ""
    row["contact_order"] = lead.get("contact_order") if lead.get("contact_order") is not None else ""
    row["latest_sender"] = lead.get("latest_sender") or ""
    row["latest_sender_platform"] = lead.get("latest_sender_platform") or ""
    tags = lead.get("tags")
    if isinstance(tags, list):
        row["tags"] = ";".join(tags)
    else:
        row["tags"] = tags or ""
    row["external_id"] = lead.get("external_id") or ""
    row["event_count"] = lead.get("event_count") or 0
    row["last_event"] = lead.get("last_event") or ""
    row["last_event_at"] = lead.get("last_event_at") or ""
    row["last_contacted_at"] = lead.get("last_contacted_at") or ""
    row["email_sent_count"] = lead.get("email_sent_count") or 0
    row["linkedin_sent_count"] = lead.get("linkedin_sent_count") or 0
    row["total_replies_count"] = lead.get("total_replies_count") or 0
    row["total_contacted_count"] = lead.get("total_contacted_count") or 0
    pers = lead.get("personalization") or {}
    if isinstance(pers, dict):
        for field, val in sorted(pers.items()):
            row[f"personalized_{field}"] = val
    return row


def query_leads_for_export(
    *,
    workspace: str,
    tag: Optional[str] = None,
    stage: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5000,
) -> tuple[list[dict], bool]:
    """Load leads for export; returns (rows, truncated)."""
    conn = get_conn()
    workspace_row = resolve_workspace_identity(conn, workspace)
    if not workspace_row:
        conn.close()
        raise ValueError(f"workspace not found: {workspace}")
    ws_id = workspace_row["id"]
    join_tags = ""
    if tag:
        join_tags = (
            " INNER JOIN workspace_lead_tags wlt "
            " ON wlt.workspace_id = wl.workspace_id AND wlt.lead_id = l.id "
            " AND wlt.tag = ? "
        )
    query = f"""
        SELECT l.*,
               COALESCE(co.name, l.company) AS company_display,
               co.domain AS company_domain,
               co.hq_city AS hq_city,
               co.hq_state AS hq_state,
               co.hq_country AS hq_country,
               wl.status AS workspace_stage,
               wl.latest_sender AS workspace_latest_sender,
               (SELECT event_type FROM events WHERE lead_id = l.id
                ORDER BY created_at DESC LIMIT 1) AS last_event,
               (SELECT created_at FROM events WHERE lead_id = l.id
                ORDER BY created_at DESC LIMIT 1) AS last_event_at,
               (SELECT COUNT(*) FROM events WHERE lead_id = l.id) AS event_count
        FROM leads l
        INNER JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
        LEFT JOIN companies co ON l.company_id = co.id
        {join_tags}
        WHERE 1=1
    """
    params: list = [ws_id]
    if tag:
        params.append(normalize_tag(tag))
    if stage:
        query += " AND wl.status = ?"
        params.append(stage)
    if since:
        since_date = since.strip()
        if since_date.lower() == "today":
            since_date = datetime.now().strftime("%Y-%m-%d")
        query += " AND (l.created_at >= ? OR l.updated_at >= ?)"
        params.extend([since_date, since_date])
    query += " ORDER BY l.updated_at DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    truncated = len(rows) >= limit
    return rows, truncated


def export_leads(
    *,
    workspace: str,
    tag: Optional[str] = None,
    stage: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5000,
    fmt: str = "csv",
    file_path: Optional[str] = None,
) -> dict:
    rows, truncated = query_leads_for_export(
        workspace=workspace, tag=tag, stage=stage, since=since, limit=limit,
    )
    enriched = enrich_lead_rows(rows, workspace=workspace)
    for row in enriched:
        if row.get("workspace_latest_sender"):
            row["latest_sender"] = row["workspace_latest_sender"]
    if fmt == "json":
        if file_path:
            out = resolve_project_path(file_path, kind="export", for_write=True)
            out.write_text(json.dumps(enriched, indent=2), encoding="utf-8")
            result = {"status": "exported", "format": "json", "file": str(out), "count": len(enriched)}
        else:
            result = {"count": len(enriched), "leads": enriched}
        if truncated:
            result["truncated"] = True
            result["limit"] = limit
        return result
    flat = [_flatten_lead_for_csv(lead) for lead in enriched]
    pers_cols = sorted({k for r in flat for k in r if k.startswith("personalized_")})
    fieldnames = list(_EXPORT_CSV_BASE_COLUMNS) + pers_cols
    if not file_path:
        ws_slug = workspace
        tag_part = normalize_tag(tag) if tag else "all"
        date_part = datetime.now().strftime("%Y-%m-%d")
        file_path = f"{ws_slug}-{tag_part}-{date_part}.csv"
    out = resolve_project_path(file_path, kind="export", for_write=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    result = {"status": "exported", "format": "csv", "file": str(out), "count": len(flat)}
    if truncated:
        result["truncated"] = True
        result["limit"] = limit
    return result


def get_stage_counts():
    conn = get_conn()
    rows = conn.execute("SELECT stage, COUNT(*) as count FROM leads GROUP BY stage ORDER BY count DESC").fetchall()
    conn.close()
    return {r["stage"]: r["count"] for r in rows}


def get_campaign_stats():
    conn = get_conn()
    breakdown_rows = conn.execute(
        """SELECT e.campaign_id,
                  e.event_type,
                  e.direction,
                  e.channel,
                  COUNT(*) AS event_count
           FROM events e
           WHERE e.campaign_id IS NOT NULL
           GROUP BY e.campaign_id, e.event_type, e.direction, e.channel
           ORDER BY e.campaign_id, event_count DESC, e.event_type"""
    ).fetchall()
    last_event_rows = conn.execute(
        """SELECT e.campaign_id, MAX(e.created_at) AS last_event_at
           FROM events e
           WHERE e.campaign_id IS NOT NULL
           GROUP BY e.campaign_id"""
    ).fetchall()
    rows = conn.execute(
        """SELECT c.id AS campaign_id,
                  c.name AS campaign,
                  (SELECT COUNT(*) FROM events e WHERE e.campaign_id = c.id) AS event_count,
                  (SELECT COUNT(*) FROM campaign_leads cl WHERE cl.campaign_id = c.id) AS lead_count,
                  (
                    SELECT COUNT(DISTINCT cl2.lead_id)
                    FROM campaign_leads cl2
                    JOIN leads l ON l.id = cl2.lead_id
                    WHERE cl2.campaign_id = c.id
                      AND l.stage = 'interested'
                  ) AS interested_count
           FROM campaigns c
           ORDER BY event_count DESC, c.name"""
    ).fetchall()
    no_campaign_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE campaign_id IS NULL"
    ).fetchone()[0]
    conn.close()
    breakdowns: dict[int, dict[str, dict[str, int]]] = {}
    for row in breakdown_rows:
        campaign_id = int(row["campaign_id"])
        campaign_bucket = breakdowns.setdefault(
            campaign_id,
            {
                "event_type_counts": {},
                "normalized_event_type_counts": {},
                "direction_counts": {},
                "channel_counts": {},
            },
        )
        event_type = row["event_type"] or "unknown"
        direction = row["direction"] or "unknown"
        channel = row["channel"] or "unknown"
        count = int(row["event_count"] or 0)
        normalized_type = normalize_campaign_event_type(event_type, direction, channel)
        campaign_bucket["event_type_counts"][event_type] = (
            campaign_bucket["event_type_counts"].get(event_type, 0) + count
        )
        campaign_bucket["normalized_event_type_counts"][normalized_type] = (
            campaign_bucket["normalized_event_type_counts"].get(normalized_type, 0) + count
        )
        campaign_bucket["direction_counts"][direction] = (
            campaign_bucket["direction_counts"].get(direction, 0) + count
        )
        campaign_bucket["channel_counts"][channel] = (
            campaign_bucket["channel_counts"].get(channel, 0) + count
        )
    last_event_by_campaign = {
        int(row["campaign_id"]): row["last_event_at"] for row in last_event_rows if row["campaign_id"] is not None
    }

    def _format_counts(counts: dict[str, int]) -> str:
        return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))

    campaigns = []
    for row in rows:
        item = dict(row)
        campaign_id = int(item.get("campaign_id") or 0)
        breakdown = breakdowns.get(campaign_id, {})
        event_type_counts = breakdown.get("event_type_counts", {})
        normalized_event_type_counts = breakdown.get("normalized_event_type_counts", {})
        direction_counts = breakdown.get("direction_counts", {})
        channel_counts = breakdown.get("channel_counts", {})
        event_types = [
            {"event_type": event_type, "count": count}
            for event_type, count in sorted(
                event_type_counts.items(),
                key=lambda kv: (-int(kv[1]), kv[0]),
            )
        ]
        normalized_event_types = [
            {"event_type": event_type, "count": count}
            for event_type, count in sorted(
                normalized_event_type_counts.items(),
                key=lambda kv: (-int(kv[1]), kv[0]),
            )
        ]
        summary_parts = []
        if event_type_counts:
            summary_parts.append(f"types: {_format_counts(event_type_counts)}")
        if normalized_event_type_counts and normalized_event_type_counts != event_type_counts:
            summary_parts.append(f"normalized: {_format_counts(normalized_event_type_counts)}")
        if direction_counts:
            summary_parts.append(f"flow: {_format_counts(direction_counts)}")
        if channel_counts:
            summary_parts.append(f"channels: {_format_counts(channel_counts)}")
        last_event_at = last_event_by_campaign.get(campaign_id)
        if last_event_at:
            summary_parts.append(f"latest: {last_event_at}")
        item["event_type_counts"] = event_type_counts
        item["event_types"] = event_types
        item["normalized_event_type_counts"] = normalized_event_type_counts
        item["normalized_event_types"] = normalized_event_types
        item["direction_counts"] = direction_counts
        item["channel_counts"] = channel_counts
        item["linkedin_connections_sent"] = int(
            normalized_event_type_counts.get("linkedin_connection_sent", 0)
        )
        item["linkedin_messages_sent"] = int(
            normalized_event_type_counts.get("linkedin_message_sent", 0)
        )
        item["linkedin_message_replies"] = int(
            normalized_event_type_counts.get("linkedin_message_reply", 0)
        )
        item["last_event_at"] = last_event_at
        item["event_summary"] = "; ".join(summary_parts) if summary_parts else "No events recorded."
        workspace = ""
        campaign_name = item.get("campaign") or ""
        if "|" in campaign_name:
            left, right = campaign_name.split("|", 1)
            workspace = left.strip()
            campaign_name = right.strip()
        item["workspace"] = workspace
        item["campaign_name"] = campaign_name
        item.pop("campaign_id", None)
        campaigns.append(item)
    return {"campaigns": campaigns, "no_campaign_events": no_campaign_events}


def get_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    reply_where = reply_event_sql_condition()
    reply_events = conn.execute(f"SELECT COUNT(*) FROM events WHERE {reply_where}").fetchone()[0]
    leads_with_replies = conn.execute(
        f"SELECT COUNT(DISTINCT lead_id) FROM events WHERE {reply_where}"
    ).fetchone()[0]
    stage_counts = get_stage_counts()
    active = sum(v for k, v in stage_counts.items() if k not in ("won", "lost"))
    recent = conn.execute("SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')").fetchone()[0]
    conn.close()
    stats = {"total_leads": total, "total_events": events, "active_pipeline": active,
             "won": stage_counts.get("won", 0), "lost": stage_counts.get("lost", 0),
             "events_7d": recent, "stages": stage_counts,
             "reply_events": reply_events, "replied_leads": leads_with_replies}
    stats.update(get_campaign_stats())
    return stats

def get_lead_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


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
            f"run: pipeline.py workspace sync"
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

    conn = get_conn()
    config = get_org_routing_config(conn, org_id)
    local_ws = conn.execute(
        "SELECT name, slug FROM workspaces WHERE org_id = ?", (org_id,)
    ).fetchall()
    local_maps = conn.execute(
        "SELECT id, campaign_name_normalized, campaign_id, match_strategy FROM campaign_workspace_map WHERE org_id = ? AND is_active = 1",
        (org_id,),
    ).fetchall()
    conn.close()

    pending_ws = []
    for row in local_ws:
        slug = row["slug"]
        if config.mode == WORKSPACE_ROUTING_MULTI and slug == "default":
            continue
        if slug not in cloud_ws_slugs:
            pending_ws.append({"name": row["name"], "slug": slug})

    pending_maps = []
    for row in local_maps:
        if row["id"] not in cloud_map_ids:
            pending_maps.append({
                "id": row["id"],
                "label": row["campaign_name_normalized"] or row["campaign_id"] or "rule",
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
           WHERE metadata_json NOT LIKE '%"source": "relay"%'
             AND metadata_json NOT LIKE '%"source":"relay"%'
             AND metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND metadata_json NOT LIKE '%"source":"agent_sync"%'"""
    ).fetchone()["n"]
    pending_lead_core_count = conn2.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    pending_workspace_count = conn2.execute(
        "SELECT COUNT(*) AS n FROM workspace_leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    pending_quarantine_count = conn2.execute(
        """SELECT COUNT(*) AS n FROM unmapped_campaign_queue
           WHERE cloud_pending = 1 AND status IN ('skipped', 'assigned')"""
    ).fetchone()["n"]
    conn2.close()

    pending_total = len(pending_ws) + len(pending_maps)
    cloud_pending = (
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
        "cloud_pending_lead_core": pending_lead_core_count,
        "cloud_pending_lead_workspaces": pending_workspace_count,
        "cloud_pending_leads": pending_lead_core_count + pending_workspace_count,
        "pending_quarantine_resolutions": pending_quarantine_count,
        "pending_total": pending_total + cloud_pending,
        "synced": pending_total == 0 and cloud_pending == 0,
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
    cloud_pending_core = status.get("cloud_pending_lead_core", 0)
    cloud_pending_ws = status.get("cloud_pending_lead_workspaces", 0)
    relay_untracked = status.get("relay_untracked_leads", 0)
    if ws:
        names = ", ".join(w["name"] for w in ws[:3])
        suffix = f" (+{len(ws) - 3} more)" if len(ws) > 3 else ""
        parts.append(f"{len(ws)} workspace{'s' if len(ws) != 1 else ''} ({names}{suffix})")
    if rules:
        parts.append(f"{len(rules)} routing rule{'s' if len(rules) != 1 else ''}")
    if local_events:
        parts.append(f"{local_events} agent event{'s' if local_events != 1 else ''}")
    if cloud_pending_core:
        parts.append(f"{cloud_pending_core} lead core snapshot{'s' if cloud_pending_core != 1 else ''}")
    if cloud_pending_ws:
        parts.append(
            f"{cloud_pending_ws} workspace snapshot{'s' if cloud_pending_ws != 1 else ''}"
        )
    out = ""
    if parts:
        out = f"\n⚠ Not synced to cloud: {', '.join(parts)}. Run pipeline.py sync."
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
           WHERE metadata_json NOT LIKE '%"source": "relay"%'
             AND metadata_json NOT LIKE '%"source":"relay"%'
             AND metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND metadata_json NOT LIKE '%"source":"agent_sync"%'"""
    ).fetchone()["n"]
    cloud_pending_core = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    cloud_pending_workspaces = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    conn.close()
    return {
        "workspaces": unsynced_ws,
        "rules": unsynced_rules,
        "local_agent_events": local_events,
        "cloud_pending_lead_core": cloud_pending_core,
        "cloud_pending_lead_workspaces": cloud_pending_workspaces,
        "cloud_pending_leads": cloud_pending_core + cloud_pending_workspaces,
        "total": unsynced_ws + unsynced_rules + local_events + cloud_pending_core + cloud_pending_workspaces,
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
    if counts.get("cloud_pending_leads"):
        n = counts["cloud_pending_leads"]
        parts.append(f"{n} lead snapshot{'s' if n != 1 else ''}")
    return f"\n⚠ Not synced: {', '.join(parts)}. Run: pipeline.py sync"


def sync_all(org_id: str = DEFAULT_ORG_ID, *, no_health_report: bool = False) -> dict:
    """Push pending workspaces, rules, and lead snapshots to the cloud.

    Network push only runs when the user invokes `pipeline.py sync` (never on
    import, init, pull, or show). Requires a configured agent key.
    """
    tok = get_agent_key()
    if not routing_cloud.cloud_routing_enabled(load_config, tok):
        return {"status": "error", "error": "No cloud token configured."}

    api_base = routing_cloud.get_api_base(load_config)
    status = get_sync_status(org_id)
    if not status.get("can_sync"):
        return {"status": "error", "error": status.get("reason", "Cannot sync.")}
    results: dict = {"workspaces_synced": [], "rules_synced": [], "errors": []}
    if status.get("synced"):
        results["status"] = "ok"
        results["message"] = "Workspaces and rules already synced."
        # Still fall through — lead snapshots may need push (cloud_pending).

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
            """SELECT source_platform, campaign_id, campaign_name_normalized, match_strategy, priority,
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
                campaign_id=row["campaign_id"],
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

    total = len(results["workspaces_synced"]) + len(results["rules_synced"])
    results["status"] = "ok"

    local_events = status.get("local_agent_events", 0)

    parts = []
    if total:
        parts.append(f"Synced {total} item{'s' if total != 1 else ''} to cloud.")
    results["relay_push_settings"] = get_relay_push_settings()
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

        lead_push = _push_pending_lead_snapshots(agent_key)
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


def _relay_push_batches(agent_key: str, entries: list[dict], client_id: str) -> dict:
    """Push relay entries in batches and return diagnostics."""
    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}

    settings = get_relay_push_settings()
    batch_size = settings["batch_size"]
    timeout_seconds = settings["timeout_seconds"]
    max_attempts = settings["max_attempts"]
    retry_base_seconds = settings["retry_base_seconds"]

    total_pushed = 0
    last_error: Optional[str] = None
    throttled = False
    timeout_failures = 0
    total_batches = (len(entries) + batch_size - 1) // batch_size

    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(
            f"Pushing batch {batch_num}/{total_batches} ({len(batch)} entries)...",
            flush=True,
        )
        body = json.dumps({"client_id": client_id, "entries": batch}).encode()
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
                    print(
                        f"Batch {batch_num}/{total_batches} pushed ({total_pushed} total so far).",
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
                    last_error += f" — monthly event limit reached. Upgrade at {BILLING_UPGRADE_URL}"
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
        suggestion = max(10, min(batch_size // 2, 500))
        recommendation = (
            "Try smaller sync batches and/or longer timeout: "
            f"OUTREACHMAGIC_SYNC_BATCH_SIZE={suggestion} "
            f"OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS={min(timeout_seconds + 30, 300)}"
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
    export = export_local_changes()
    entries = export.get("entries") or []
    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}
    client_id = export.get("client_id", "unknown")
    return _relay_push_batches(agent_key, entries, client_id)


def _push_pending_lead_snapshots(agent_key: str) -> dict:
    """Push pending lead core + workspace snapshots to relay /push."""
    conn = get_conn()
    core_rows = conn.execute(
        "SELECT id, updated_at FROM leads WHERE cloud_pending = 1"
    ).fetchall()
    ws_rows = conn.execute(
        """SELECT wl.lead_id, wl.workspace_id, wl.updated_at, w.slug
           FROM workspace_leads wl
           JOIN workspaces w ON w.id = wl.workspace_id
           WHERE wl.cloud_pending = 1"""
    ).fetchall()
    if not core_rows and not ws_rows:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False}

    lead_ids = sorted({r["id"] for r in core_rows} | {r["lead_id"] for r in ws_rows})
    prefetch = _load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, lead_ids)
    client_id = get_or_create_client_id()

    core_entries: list[dict] = []
    core_mark_ids: list[int] = []
    for row in core_rows:
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
        core_mark_ids.append(lead_id)

    ws_entries: list[dict] = []
    ws_mark_keys: list[tuple[int, str]] = []
    for row in ws_rows:
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
        ws_mark_keys.append((lead_id, row["workspace_id"]))

    conn.close()
    total_pushed = 0
    last_result: dict = {"pushed": 0, "error": None, "throttled": False}

    if core_entries:
        print(f"Pushing {len(core_entries)} lead core snapshot(s)...", flush=True)
        last_result = _relay_push_batches(agent_key, core_entries, client_id)
        total_pushed += int(last_result.get("pushed", 0) or 0)
        if last_result.get("error"):
            last_result["pushed"] = total_pushed
            return last_result
        if int(last_result.get("pushed", 0) or 0) >= len(core_entries) and core_mark_ids:
            mark_conn = get_conn()
            ph = ",".join("?" for _ in core_mark_ids)
            mark_conn.execute(f"UPDATE leads SET cloud_pending = 0 WHERE id IN ({ph})", core_mark_ids)
            mark_conn.execute(
                f"UPDATE lead_personalization SET cloud_pending = 0 WHERE lead_id IN ({ph})",
                core_mark_ids,
            )
            mark_conn.commit()
            mark_conn.close()

    if ws_entries:
        print(f"Pushing {len(ws_entries)} lead workspace snapshot(s)...", flush=True)
        ws_result = _relay_push_batches(agent_key, ws_entries, client_id)
        total_pushed += int(ws_result.get("pushed", 0) or 0)
        last_result = ws_result
        if ws_result.get("error"):
            last_result["pushed"] = total_pushed
            return last_result
        if int(ws_result.get("pushed", 0) or 0) >= len(ws_entries) and ws_mark_keys:
            mark_conn = get_conn()
            for lead_id, workspace_id in ws_mark_keys:
                mark_conn.execute(
                    """UPDATE workspace_leads SET cloud_pending = 0
                       WHERE lead_id = ? AND workspace_id = ?""",
                    (lead_id, workspace_id),
                )
            mark_conn.commit()
            mark_conn.close()

    last_result["pushed"] = total_pushed
    return last_result


def _push_pending_company_updates(agent_key: str) -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT id, updated_at FROM companies WHERE cloud_pending = 1").fetchall()
    if not rows:
        conn.close()
        return {"pushed": 0, "error": None, "throttled": False}

    client_id = get_or_create_client_id()
    entries = []
    pushed_ids = []
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
        pushed_ids.append(row["id"])
    conn.close()
    if not entries:
        return {"pushed": 0, "error": None, "throttled": False}

    push_result = _relay_push_batches(agent_key, entries, client_id)
    if int(push_result.get("pushed", 0) or 0) <= 0:
        return push_result

    mark_count = min(int(push_result.get("pushed", 0) or 0), len(pushed_ids))
    mark_conn = get_conn()
    placeholders = ",".join("?" for _ in pushed_ids[:mark_count])
    mark_conn.execute(
        f"UPDATE companies SET cloud_pending = 0 WHERE id IN ({placeholders})",
        pushed_ids[:mark_count],
    )
    mark_conn.execute(
        f"UPDATE company_personalization SET cloud_pending = 0 WHERE company_id IN ({placeholders})",
        pushed_ids[:mark_count],
    )
    mark_conn.commit()
    mark_conn.close()
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
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: Optional[str] = None,
    priority: int = 100,
) -> dict:
    if not campaign_id and not campaign_name:
        return {"status": "error", "error": "provide --campaign-id or --campaign-name"}
    conn_check = get_conn()
    config = get_org_routing_config(conn_check, DEFAULT_ORG_ID)
    conn_check.close()
    if config.mode == WORKSPACE_ROUTING_MULTI and workspace_slug == "default":
        return {"status": "error", "error": "Cannot route to the default workspace in multi-workspace mode."}
    strategy = match_strategy or ("id_exact" if campaign_id else "name_exact")
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
                campaign_id=campaign_id,
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
        campaign_id=campaign_id,
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
            """SELECT id, source_platform, campaign_id, campaign_name_raw,
                      campaign_name_normalized, external_event_id, reason, status,
                      assigned_workspace, cloud_pending, received_at, resolved_at
               FROM unmapped_campaign_queue
               WHERE org_id = ?
               ORDER BY received_at DESC LIMIT ?""",
            (org_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, source_platform, campaign_id, campaign_name_raw,
                      campaign_name_normalized, external_event_id, reason, status,
                      assigned_workspace, cloud_pending, received_at, resolved_at
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
                    "campaign_id": item.get("campaign_id"),
                    "campaign_name": item.get("campaign_name_raw"),
                },
            )
            item["message"] = format_unmapped_campaign_message(ctx)
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
               COALESCE(NULLIF(campaign_name_raw, ''), NULLIF(campaign_id, ''), 'unknown') AS campaign,
               campaign_id,
               COUNT(*) AS event_count,
               MIN(received_at) AS oldest_received_at,
               MAX(received_at) AS newest_received_at
           FROM unmapped_campaign_queue
           WHERE org_id = ? AND status = ?
           GROUP BY source_platform, campaign
           ORDER BY event_count DESC, source_platform ASC, campaign ASC""",
        (org_id, status),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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

    total_events = sum(int(r.get("event_count") or 0) for r in campaigns)
    lines = [
        f"Pending quarantine: {total_events} event(s) across {len(campaigns)} campaign(s).",
        "",
        f"{'Platform':<{platform_w}}  {'Campaign':<{campaign_w}}  {'Events':>{count_w}}",
        "-" * (platform_w + campaign_w + count_w + 4),
    ]
    for row in campaigns:
        lines.append(
            f"{(row.get('source_platform') or ''):<{platform_w}}  "
            f"{(row.get('campaign') or 'unknown'):<{campaign_w}}  "
            f"{int(row.get('event_count') or 0):>{count_w}}"
        )

    if include_steps:
        lines.extend(
            [
                "",
                "Next steps to map campaigns to a workspace:",
                '1. Create one or more workspaces (if needed):  pipeline.py workspace create --name "Team Name"',
                "2. Ensure every campaign is covered by either a campaign rule or a manual mapping.",
                "3. Add campaign mappings (replace WORKSPACE_SLUG):",
            ]
        )
        seen_commands: set[str] = set()
        for row in campaigns:
            campaign_id = str(row.get("campaign_id") or "").strip()
            campaign_name = str(row.get("campaign") or "unknown").strip() or "unknown"
            if campaign_id:
                cmd = (
                    "   pipeline.py campaign-map add "
                    f"--workspace WORKSPACE_SLUG --campaign-id {campaign_id}"
                )
            else:
                escaped = campaign_name.replace('"', '\\"')
                cmd = (
                    "   pipeline.py campaign-map add "
                    f'--workspace WORKSPACE_SLUG --campaign-name "{escaped}"'
                )
            if cmd in seen_commands:
                continue
            seen_commands.add(cmd)
            lines.append(cmd)
        lines.extend(
            [
                "4. Replay quarantined events:  pipeline.py quarantine replay",
                "   (or skip junk: pipeline.py quarantine skip --id QUEUE_ID)",
                "   (or assign one: pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG; then sync + pull)",
            ]
        )

    return "\n".join(lines)


def print_quarantine_guidance() -> None:
    routing = get_workspace_routing()
    pending = int(routing.get("pending_quarantine") or 0)
    if routing.get("mode") != WORKSPACE_ROUTING_MULTI or pending <= 0:
        return
    print(MULTI_WORKSPACE_HOLD_MESSAGE, file=sys.stderr)
    print(format_quarantine_campaign_summary(get_quarantine_campaign_summary()), file=sys.stderr)


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
           SET status = 'skipped', resolved_at = ?, cloud_pending = 1
           WHERE id = ?""",
        (now, queue_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "id": queue_id, "relay_id": relay_id}


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
           SET status = 'assigned', assigned_workspace = ?, resolved_at = ?,
               cloud_pending = 1
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
    rows = conn.execute(
        """SELECT external_event_id, status, assigned_workspace, resolved_at
           FROM unmapped_campaign_queue
           WHERE cloud_pending = 1 AND status IN ('skipped', 'assigned')"""
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
    if succeeded:
        placeholders = ",".join("?" * len(succeeded))
        conn = get_conn()
        conn.execute(
            f"""UPDATE unmapped_campaign_queue SET cloud_pending = 0
                WHERE cloud_pending = 1 AND external_event_id IN ({placeholders})""",
            [str(rid) for rid in succeeded],
        )
        conn.commit()
        conn.close()

    return {"synced": len(succeeded), "errors": errors}


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
           SET status = 'replayed', resolved_at = datetime('now'), cloud_pending = 0
           WHERE id = ?""",
        (queue_id,),
    )
    conn.commit()
    conn.close()
    if lead_id is None:
        return {"status": "error", "error": "ingest failed", "queue_id": queue_id}
    return {"status": "ok", "queue_id": queue_id, "lead_id": lead_id}


def replay_pending_quarantine(workspace_slug: Optional[str] = None, limit: int = 100) -> dict:
    pending = list_quarantine(status="pending", limit=limit)
    replayed = skipped = 0
    slug_cache = qres.WorkspaceSlugCache()
    for item in pending:
        if workspace_slug:
            ws_id = slug_cache.workspace_id(workspace_slug)
        else:
            conn = get_conn()
            campaign_name = item.get("campaign_name_raw") or item.get("campaign_name_normalized")
            if not campaign_name and not item.get("campaign_id"):
                campaign_name = "unknown"
            ctx = extract_campaign_context(
                item["source_platform"],
                {},
                {
                    "campaign_id": item.get("campaign_id"),
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


def find_lead_by_identifier(conn: sqlite3.Connection, entity_key: str) -> Optional[int]:
    """Resolve entity_key (email, linkedin URL, or type:value identity) to a lead ID."""
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
                WHERE l.id NOT IN (
                    SELECT DISTINCT lead_id FROM relay_ingested
                    WHERE lead_id IS NOT NULL
                ) {workspace_filter}
                ORDER BY l.created_at ASC""",
            workspace_params,
        ).fetchall()

    entries = []
    lead_ids = set()
    for row in lead_rows:
        lead_id = row["id"]
        if row["cloud_pending"]:
            continue
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

    event_rows = conn.execute(
        """SELECT e.*, l.email, l.linkedin_url
           FROM events e
           JOIN leads l ON e.lead_id = l.id
           WHERE e.metadata_json NOT LIKE '%"source": "relay"%'
             AND e.metadata_json NOT LIKE '%"source":"relay"%'
             AND e.metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND e.metadata_json NOT LIKE '%"source":"agent_sync"%'
           ORDER BY e.created_at ASC""",
    ).fetchall()

    for row in event_rows:
        entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, row["lead_id"])
        if not entity_key:
            continue
        ws_slug = _lead_workspace_slug(conn, row["lead_id"])
        event_entry: dict = {
            "action": "event_log",
            "entity_key": entity_key,
            "timestamp": normalize_relay_timestamp(row["created_at"]),
            "payload": {
                "event_type": row["event_type"],
                "direction": row["direction"],
                "channel": row["channel"],
            },
        }
        if ws_slug:
            event_entry["workspace"] = ws_slug
        if row["subject"]:
            event_entry["payload"]["subject"] = row["subject"]
        if row["body_preview"]:
            event_entry["payload"]["body_preview"] = row["body_preview"]
        entries.append(event_entry)

    conn.close()
    return {
        "version": 1,
        "client_id": client_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }


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
    out_path = Path(path).expanduser()
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for entry in lead_entries:
            writer.writerow(entry.get("payload", {}))
    print(json.dumps({"status": "exported", "file": str(out_path), "leads": len(lead_entries)}))


def ingest_agent_entry(
    event: dict,
    quiet: bool = False,
) -> Optional[int]:
    """Replay an agent-originated mutation from another client during pull."""
    action = event.get("action", "")
    payload = event.get("payload", {})
    client_id = event.get("client_id", "")
    entity_key = event.get("entity_key", "")
    timestamp = event.get("timestamp", "")
    workspace_slug = event.get("workspace")

    local_client_id = get_or_create_client_id()
    if client_id == local_client_id:
        return None

    dedupe_key = f"agent:{client_id}:{entity_key}:{action}:{timestamp}"
    if relay_already_ingested(dedupe_key):
        return None

    conn = get_conn()
    lead_id = None
    company_id = None
    try:
        org_id = DEFAULT_ORG_ID
        routing_config = get_org_routing_config(conn, org_id)
        workspace_id = None

        if routing_config.mode == WORKSPACE_ROUTING_SINGLE:
            workspace_id = routing_config.default_workspace_id
        elif routing_config.mode == WORKSPACE_ROUTING_MULTI:
            if workspace_slug:
                ws_row = resolve_workspace_identity(conn, workspace_slug)
                workspace_id = ws_row["id"] if ws_row else None

        if action == "lead_core_update":
            lead_id = find_lead_by_identifier(conn, entity_key) if entity_key else None
            conn.close()
            if not lead_id:
                result = resolve_lead_from_agent_sync(entity_key, payload)
                if result.get("status") == "error":
                    mark_relay_ingested(dedupe_key, None)
                    return None
                lead_id = result.get("id")
            if lead_id:
                apply_agent_lead_core_payload(
                    lead_id, payload, org_id=org_id, entity_key=entity_key,
                )
        elif action == "lead_workspace_update":
            if not workspace_id:
                conn.close()
                mark_relay_ingested(dedupe_key, None)
                return None
            lead_id = find_lead_by_identifier(conn, entity_key) if entity_key else None
            conn.close()
            if not lead_id:
                result = resolve_lead_from_agent_sync(entity_key, {})
                if result.get("status") == "error":
                    mark_relay_ingested(dedupe_key, None)
                    return None
                lead_id = result.get("id")
            if lead_id:
                apply_agent_lead_workspace_payload(
                    lead_id, payload, org_id=org_id, workspace_id=workspace_id,
                )
        elif action == "company_update":
            company_id = resolve_company_from_entity_key(conn, entity_key) if entity_key else None
            conn.close()
            if company_id:
                apply_agent_company_sync_payload(company_id, payload)
        # Workspace-scoped actions: skip if no workspace (don't quarantine)
        elif action == "stage_change":
            if not workspace_id:
                conn.close()
                mark_relay_ingested(dedupe_key, None)
                return None
            lead_id = find_lead_by_identifier(conn, entity_key)
            conn.close()
            if lead_id and payload.get("stage"):
                try:
                    update_lead_stage(lead_id, payload["stage"], payload.get("next_action"))
                except ValueError:
                    pass
        elif action == "event_log":
            if not workspace_id:
                conn.close()
                mark_relay_ingested(dedupe_key, None)
                return None
            lead_id = find_lead_by_identifier(conn, entity_key)
            conn.close()
            if lead_id:
                log_event(
                    lead_id,
                    event_type=payload.get("event_type", "email_sent"),
                    direction=payload.get("direction", "outbound"),
                    channel=payload.get("channel", "email"),
                    subject=payload.get("subject"),
                    body_preview=payload.get("body_preview"),
                    metadata={"source": "agent_sync", "origin_client": client_id},
                )
        else:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise

    if lead_id is not None or action in ("company_update", "lead_core_update", "lead_workspace_update"):
        mark_relay_ingested(dedupe_key, lead_id)
    return lead_id


RELAY_PULL_PAGE_SIZE = 1000
RELAY_PULL_HTTP_TIMEOUT = 60
RELAY_PULL_HTTP_RETRIES = 2


def _estimate_relay_pages(pending: Optional[int]) -> Optional[int]:
    if pending is None or pending <= 0:
        return None
    return max(1, (pending + RELAY_PULL_PAGE_SIZE - 1) // RELAY_PULL_PAGE_SIZE)


def _pull_failure_message(exc: Exception) -> str:
    msg = str(exc).strip()
    if "routing api" in msg.lower():
        return (
            f"{msg}\n\nRouting sync failed. Retry without cloud routing sync:\n"
            "  pipeline.py pull --skip-routing-sync"
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
        f"Imported {imported} new events. {lead_count} leads in local database.",
        (
            f"Already-processed: {dupes} duplicate relay events skipped"
            + (f", {filtered} filtered" if filtered else "")
            + (f", {errors} errors" if errors else "")
            + " (normal on replay)."
        ),
    ]
    snap_records = int(stats.get("snapshot_records_seen") or 0)
    if snap_records:
        lines.append(
            f"Snapshot phase: {snap_records} core/workspace/company snapshot record(s) applied."
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
) -> dict:
    """Pull org events from relay (cursor-only: after_id / snapshot_after_id)."""
    params = []
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
            with urllib.request.urlopen(req, timeout=RELAY_PULL_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
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
) -> dict:
    imported = skipped = skipped_duplicates = skipped_filtered = skipped_errors = 0
    skipped_resolved = assigned_resolved = 0
    newest_relay_id_seen = 0
    resolutions = resolution_map or {}
    ws_cache = slug_cache or qres.WorkspaceSlugCache()

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
                try:
                    ingested = ingest_relay_event(
                        event,
                        force_workspace_id=ws_id,
                        debug_sentiment=debug_sentiment,
                        quiet=quiet,
                    )
                except Exception as exc:
                    if not quiet:
                        print(f"Warning: skipped relay event {relay_id}: {exc}")
                    skipped += 1
                    skipped_errors += 1
                    continue
                if ingested is None:
                    skipped += 1
                    skipped_filtered += 1
                else:
                    imported += 1
                    assigned_resolved += 1
                continue

        dedupe_key = relay_dedupe_key(event)
        was_duplicate = relay_already_ingested(dedupe_key)
        try:
            ingested = ingest_relay_event(event, debug_sentiment=debug_sentiment, quiet=quiet)
        except Exception as exc:
            if not quiet:
                print(f"Warning: skipped relay event {event.get('relay_id') or '?'}: {exc}")
            skipped += 1
            skipped_errors += 1
            continue
        if ingested is None:
            skipped += 1
            if was_duplicate:
                skipped_duplicates += 1
            else:
                skipped_filtered += 1
        else:
            imported += 1
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


def sync_from_relay_org(
    agent_key: str,
    after_id: Optional[int] = None,
    full: bool = False,
    debug_sentiment: bool = False,
    quiet: bool = False,
    stats: Optional[dict] = None,
    *,
    skip_routing_sync: bool = False,
) -> tuple[int, int]:
    """Import relay events for the org. Cursors: last_max_id (events), snapshot cursors (core/workspace/company)."""
    if not skip_routing_sync:
        try:
            maybe_sync_routing_from_cloud(quiet=quiet)
        except RuntimeError as exc:
            raise RuntimeError(_pull_failure_message(exc)) from exc
    elif not quiet:
        print("Skipped routing config sync (--skip-routing-sync).", flush=True)
    if not quiet:
        print("Contacting relay to pull new events...", flush=True)

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
    resolution_map: dict[int, dict] = {}
    slug_cache = qres.WorkspaceSlugCache()
    skipped_resolved = assigned_resolved = 0

    while True:
        event_pages += 1
        result = pull_events_org(
            agent_key,
            after_id=page_after_id or None,
            include_pending=event_pages == 1,
            include_queue_resolutions=event_pages == 1,
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
            est_event_pages = _estimate_relay_pages(pending_events)
            if not quiet and pending_events > 0:
                pages_hint = f"~{est_event_pages} pages" if est_event_pages else "multiple pages"
                print(
                    f"Relay events: ~{pending_events} pending ({pages_hint} @ {RELAY_PULL_PAGE_SIZE}/page)...",
                    flush=True,
                )
        elif (
            event_pages == 1
            and pending_events is None
            and not quiet
            and len(events) >= RELAY_PULL_PAGE_SIZE
        ):
            print(
                f"Relay events: first page has {len(events)} records "
                f"(@ {RELAY_PULL_PAGE_SIZE}/page max — more pages follow)...",
                flush=True,
            )

        relay_events_seen += len(events)
        if not quiet:
            page_label = f"page {event_pages}"
            if est_event_pages:
                page_label = f"page {event_pages} of ~{est_event_pages}"
            elif len(events) >= RELAY_PULL_PAGE_SIZE:
                page_label = f"page {event_pages} (more pages likely)"
            progress = f"{relay_events_seen}/{pending_events}" if pending_events else str(relay_events_seen)
            print(
                f"Relay events: {page_label} — {len(events)} this page, {progress} records...",
                flush=True,
            )

        batch = _ingest_relay_page(
            events,
            debug_sentiment=debug_sentiment,
            quiet=True,
            resolution_map=resolution_map,
            slug_cache=slug_cache,
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
        if len(events) >= RELAY_PULL_PAGE_SIZE and next_after_id <= page_after_id:
            cursor_stalled = True
            break
        page_after_id = next_after_id
        if len(events) < RELAY_PULL_PAGE_SIZE:
            break

    pending_snapshots: Optional[int] = None
    est_snap_pages: Optional[int] = None
    for snap_kind in ("core", "workspace", "company"):
        kind_pages = 0
        if not quiet:
            print(f"Pulling {snap_kind} snapshot records...", flush=True)
        while True:
            snap_pages += 1
            kind_pages += 1
            snap_result = pull_events_org(
                agent_key,
                snapshot_after_id=snapshot_cursors[snap_kind] or None,
                snapshot_kind=snap_kind,
                snapshots_only=True,
                include_pending=snap_pages == 1 and kind_pages == 1,
            )
            if snap_result.get("error"):
                raise RuntimeError(snap_result.get("message", "snapshot pull failed"))
            snap_events = snap_result.get("events") or []
            if not snap_events:
                break

            if snap_pages == 1 and kind_pages == 1 and snap_result.get("pending_snapshot_count") is not None:
                pending_snapshots = int(snap_result["pending_snapshot_count"])
                est_snap_pages = _estimate_relay_pages(pending_snapshots)
                if not quiet and pending_snapshots > 0:
                    pages_hint = f"~{est_snap_pages} pages" if est_snap_pages else "multiple pages"
                    print(
                        f"Snapshot records: ~{pending_snapshots} pending ({pages_hint} @ {RELAY_PULL_PAGE_SIZE}/page)...",
                        flush=True,
                    )

            snap_total += len(snap_events)
            if not quiet:
                print(
                    f"Snapshot {snap_kind}: page {kind_pages} — {len(snap_events)} records "
                    f"({snap_total} total)...",
                    flush=True,
                )
            batch = _ingest_relay_page(snap_events, debug_sentiment=debug_sentiment, quiet=True)
            imported += batch["imported"]
            skipped += batch["skipped"]
            skipped_duplicates += batch["skipped_duplicates"]
            skipped_filtered += batch["skipped_filtered"]
            skipped_errors += batch["skipped_errors"]
            newest_relay_id_seen = max(newest_relay_id_seen, batch["newest_relay_id_seen"])
            snapshot_cursors[snap_kind] = int(snap_result.get("max_snapshot_id") or 0)
            if not snap_result.get("has_more_snapshots"):
                break

    if page_after_id:
        set_last_max_id(page_after_id)
    for kind, cursor in snapshot_cursors.items():
        if cursor:
            set_snapshot_cursor(cursor, kind)
    set_last_pull(datetime.now(timezone.utc).isoformat())

    pull_hint = None
    if not full and relay_events_seen == 0 and not cursor_stalled and page_after_id == initial_after_id:
        pull_hint = "no new relay events — run `pull --full` once or clear last_max_id in config"

    cursor_advanced = bool(page_after_id > initial_after_id)
    if stats is not None:
        stats.update({
            "mode": "full" if full else "incremental",
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
        print_quarantine_guidance()
    return imported, skipped


REFRESH_WARNING = """
⚠ LOCAL DATABASE REFRESH — destructive, use rarely

This will:
  1. Push pending local changes to the relay (sync) unless you pass --skip-sync
  2. Back up your local SQLite file
  3. Delete the local database and re-import everything from api.outreachmagic.io

You will lose any local-only data that was NOT synced to the relay.
pull --full alone does NOT refresh — it still skips rows already in relay_ingested.

Re-run with: pipeline.py refresh --yes

If refresh times out during the relay pull, the database may be partial. Run pull again —
it resumes from saved cursors: pipeline.py pull --full
""".strip()


def _clear_pull_cursors() -> None:
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
    if not yes:
        return {
            "status": "error",
            "error": "confirmation_required",
            "message": REFRESH_WARNING,
        }

    result: dict = {"status": "ok", "steps": []}

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

    db_path = get_db_path()
    backup_path = Path(backup).expanduser() if backup else db_path.with_suffix(
        f".backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    )
    if db_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup_path)
        result["backup"] = str(backup_path)
        result["steps"].append("backup")
        db_path.unlink()
        result["steps"].append("delete_db")
    else:
        result["steps"].append("no_existing_db")

    _clear_pull_cursors()
    result["steps"].append("clear_pull_cursors")

    init_db()
    result["steps"].append("init")

    agent_key = get_agent_key()
    if not agent_key:
        return {
            "status": "error",
            "error": "no_agent_key",
            "message": "Database wiped but no agent key to pull from relay. Run login, then pull --full.",
            **result,
        }

    try:
        imported, skipped = sync_from_relay_org(
            agent_key,
            full=True,
            quiet=quiet,
        )
    except RuntimeError as exc:
        return {
            "status": "error",
            "error": "pull_failed",
            "message": (
                f"{exc}\n\nDatabase may be partial. Resume with: pipeline.py pull --full "
                "(continues from saved cursors)."
            ),
            **result,
        }

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
):
    """Connect this machine via browser device authorization (GitHub CLI-style)."""
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
            flow = device_login.start_device_authorization(load_config, platform=platform)
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
            _save_agent_key_and_validate(str(claim.get("access_token") or ""))
            print("STATUS=success")
            return
        if status == "pending":
            print("STATUS=pending")
            return
        print(f"STATUS={status}")
        sys.exit(1)

    try:
        agent_key = device_login.run_device_login(load_config, platform=platform)
    except RuntimeError as exc:
        print(f"\nLogin failed: {exc}")
        sys.exit(1)
    _save_agent_key_and_validate(agent_key)


def logout():
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


def _save_agent_key_and_validate(agent_key: str):
    if not agent_key.startswith("om_agent_"):
        print("Invalid key format. Agent keys start with 'om_agent_'.")
        sys.exit(1)

    cfg = load_config()
    cfg["agent_key"] = agent_key
    cfg.pop("token", None)
    save_config(cfg)

    print("\nValidating key...")
    result = pull_events_org(agent_key)
    if result.get("error"):
        status = result.get("status", "")
        message = result.get("message", "")
        if "401" in str(status) or "Invalid" in message or "revoked" in message.lower():
            print(f"Authentication failed: {message}")
            print("Run: pipeline.py login")
            cfg.pop("agent_key", None)
            save_config(cfg)
            sys.exit(1)
        print(f"Warning: could not reach relay ({message}). Key saved — will retry on next pull.")
    else:
        count = result.get("count", 0)
        org_id = result.get("organization_id", "")
        print(f"Connected to org {org_id} — {count} events available.")

    try:
        maybe_sync_routing_from_cloud(quiet=True)
    except Exception:
        pass

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
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))

    project_root = ensure_project_layout()
    print()
    print(f"Project folders: {project_root}")
    print(f"  input/  export/  agent_resources/")
    print()
    print("Connected. Run 'pull' to sync events, 'show' to view pipeline.")


# Connection management (via app API) — platform labels/hints from platform_registry.py


def cmd_platform_map(platform: Optional[str] = None) -> None:
    """Print platform and event mapping registry (agent discovery)."""
    data = platform_map_json(platform)
    print(json.dumps(data, indent=2))


def _require_agent_key() -> str:
    key = get_agent_key()
    if not key:
        print("No agent key configured. Run: pipeline.py login")
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


def cmd_status():
    """Dashboard-style status: plan, connections, usage, routing."""
    agent_key = _require_agent_key()
    api_base = routing_cloud.get_api_base(load_config)

    try:
        data = connections_cloud.fetch_status(api_base, agent_key)
    except RuntimeError as exc:
        print(f"Could not fetch status: {exc}")
        sys.exit(1)

    plan = (data.get("plan") or "free").capitalize()
    events_used = int(data.get("eventsUsed", 0) or 0)
    events_limit = data.get("eventsLimit")
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
    print(f"Plan: {plan}{plan_suffix}  |  Events this month: {usage_str}  |  Resets: {resets_label}")

    if events_limit:
        if events_used >= events_limit:
            print(f"⚠  Monthly limit reached. New relay events are rejected (HTTP 429). Upgrade: {upgrade_url}")
        elif pct >= USAGE_WARNING_PERCENT:
            remaining = max(0, events_limit - events_used)
            print(f"⚠  {pct}% of monthly limit used ({remaining} remaining). Upgrade: {upgrade_url}")
    print()

    connections = data.get("connections", [])
    active = [c for c in connections if c.get("status") == "active"]
    print(f"Connections ({len(active)} active)")
    if not connections:
        print("  No connections. Run: pipeline.py connect-platform --platform smartlead")
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
        print("No connections. Run: pipeline.py connect-platform --platform smartlead")
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


# ──────────────────────────────────────────────────────────────────────
# Personalization (mail-merge fields)
# ──────────────────────────────────────────────────────────────────────

_LEAD_SOURCE_FIELDS = {"first_name": "name"}
_COMPANY_SOURCE_FIELDS = {"company_name": "name"}


def is_company_personalization_field(field_name: str) -> bool:
    return field_name == "company_name" or field_name.startswith("company_")


def resolve_company_id(
    conn: sqlite3.Connection,
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[int]:
    if company_id:
        row = conn.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
        return company_id if row else None
    dom = normalize_company_domain(domain)
    if dom and dom not in SHARED_EMAIL_DOMAINS:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (dom,)).fetchone()
        if row:
            return row["id"]
        return ensure_company(conn, domain=dom)
    if name and str(name).strip():
        return ensure_company(conn, name=str(name).strip())
    return None


def company_entity_key(conn: sqlite3.Connection, company_id: int) -> Optional[str]:
    row = conn.execute("SELECT name, domain FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        return None
    dom = (row["domain"] or "").strip().lower()
    if dom and dom not in SHARED_EMAIL_DOMAINS:
        return f"company:domain:{dom}"
    nm = (row["name"] or "").strip().lower()
    return f"company:name:{nm}" if nm else None


def resolve_company_from_entity_key(conn: sqlite3.Connection, entity_key: str) -> Optional[int]:
    if not entity_key.startswith("company:"):
        return None
    parts = entity_key.split(":", 2)
    if len(parts) != 3:
        return None
    kind, val = parts[1], parts[2]
    if kind == "domain":
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (val,)).fetchone()
        return row["id"] if row else ensure_company(conn, domain=val)
    if kind == "name":
        return ensure_company(conn, name=val)
    return None


def _lead_source_hash(lead_id: int, field_name: str) -> Optional[str]:
    col = _LEAD_SOURCE_FIELDS.get(field_name)
    if not col:
        return None
    conn = get_conn()
    row = conn.execute(f"SELECT {col} FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    if not row or not row[col]:
        return None
    return hashlib.md5(str(row[col]).encode()).hexdigest()[:8]


def _company_source_hash(company_id: int, field_name: str) -> Optional[str]:
    col = _COMPANY_SOURCE_FIELDS.get(field_name)
    if not col:
        return None
    conn = get_conn()
    row = conn.execute(f"SELECT {col} FROM companies WHERE id = ?", (company_id,)).fetchone()
    conn.close()
    if not row or not row[col]:
        return None
    return hashlib.md5(str(row[col]).encode()).hexdigest()[:8]


def _mark_company_cloud_pending(company_id: int, conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    if own:
        conn = get_conn()
    conn.execute(
        "UPDATE companies SET cloud_pending = 1, updated_at = datetime('now') WHERE id = ?",
        (company_id,),
    )
    if own:
        conn.commit()
        conn.close()


def _lead_personalization_dict(conn: sqlite3.Connection, lead_id: int) -> dict:
    rows = conn.execute(
        "SELECT field_name, field_value, field_date, processed_at FROM lead_personalization WHERE lead_id = ?",
        (lead_id,),
    ).fetchall()
    return {r["field_name"]: dict(r) for r in rows}


def _company_personalization_dict(conn: sqlite3.Connection, company_id: int) -> dict:
    rows = conn.execute(
        "SELECT field_name, field_value, field_date, processed_at FROM company_personalization WHERE company_id = ?",
        (company_id,),
    ).fetchall()
    return {r["field_name"]: dict(r) for r in rows}


def resolve_personalization(lead_id: int) -> dict:
    """Merged mail-merge values (company fields, then lead overrides)."""
    conn = get_conn()
    row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    merged: dict = {}
    if row and row["company_id"]:
        for fname, rec in _company_personalization_dict(conn, row["company_id"]).items():
            merged[fname] = rec["field_value"]
            if rec.get("field_date"):
                merged[f"{fname}_date"] = rec["field_date"]
    for fname, rec in _lead_personalization_dict(conn, lead_id).items():
        merged[fname] = rec["field_value"]
        if rec.get("field_date"):
            merged[f"{fname}_date"] = rec["field_date"]
        elif f"{fname}_date" in merged:
            del merged[f"{fname}_date"]
    conn.close()
    return merged


def _personalization_sync_payload(rows: dict) -> tuple[dict, dict, Optional[str]]:
    values = {k: v["field_value"] for k, v in rows.items()}
    dates = {k: v["field_date"] for k, v in rows.items() if v.get("field_date")}
    at = max((v["processed_at"] for v in rows.values()), default=None)
    return values, dates, at


def personalize_set(
    lead_id: int,
    field_name: str,
    field_value: str,
    *,
    field_date: Optional[str] = None,
) -> dict:
    if is_company_personalization_field(field_name):
        return {"status": "error", "error": f"{field_name} is company-scoped — use company-personalize-set"}
    conn = get_conn()
    conn.execute("""
        INSERT INTO lead_personalization (lead_id, field_name, field_value, field_date, source_hash, cloud_pending)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT (lead_id, field_name) DO UPDATE SET
            field_value = excluded.field_value,
            field_date = excluded.field_date,
            source_hash = excluded.source_hash,
            processed_at = datetime('now'),
            cloud_pending = 1
    """, (lead_id, field_name, field_value, field_date, _lead_source_hash(lead_id, field_name)))
    _mark_lead_cloud_pending(lead_id, conn=conn)
    conn.commit()
    conn.close()
    return {"status": "ok", "lead_id": lead_id, "field": field_name}


def personalize_set_batch(items: list[dict]) -> dict:
    written = 0
    err_list = []
    for item in items:
        lid = item.get("lead_id")
        fname = item.get("field")
        fval = item.get("value")
        if not lid or not fname or fval is None:
            err_list.append({"item": item, "error": "missing lead_id, field, or value"})
            continue
        if is_company_personalization_field(fname):
            err_list.append({"item": item, "error": f"{fname} is company-scoped"})
            continue
        personalize_set(lid, fname, str(fval), field_date=item.get("date"))
        written += 1
    return {"status": "ok", "written": written, "errors": err_list}


def company_personalize_set(
    field_name: str,
    field_value: str,
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
    field_date: Optional[str] = None,
) -> dict:
    if not is_company_personalization_field(field_name):
        return {"status": "error", "error": f"{field_name} is not a company personalization field"}
    conn = get_conn()
    cid = resolve_company_id(conn, company_id=company_id, domain=domain, name=name)
    if not cid:
        conn.close()
        return {"status": "error", "error": "company not found"}
    conn.execute("""
        INSERT INTO company_personalization (company_id, field_name, field_value, field_date, source_hash, cloud_pending)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT (company_id, field_name) DO UPDATE SET
            field_value = excluded.field_value,
            field_date = excluded.field_date,
            source_hash = excluded.source_hash,
            processed_at = datetime('now'),
            cloud_pending = 1
    """, (cid, field_name, field_value, field_date, _company_source_hash(cid, field_name)))
    _mark_company_cloud_pending(cid, conn=conn)
    conn.commit()
    conn.close()
    return {"status": "ok", "company_id": cid, "field": field_name}


def company_personalize_set_batch(items: list[dict]) -> dict:
    written = 0
    errors = []
    for item in items:
        fname = item.get("field")
        fval = item.get("value")
        if not fname or fval is None:
            errors.append({"item": item, "error": "missing field or value"})
            continue
        result = company_personalize_set(
            fname, str(fval),
            company_id=item.get("company_id"),
            domain=item.get("domain"),
            name=item.get("name") or item.get("company"),
            field_date=item.get("date"),
        )
        if result.get("status") == "ok":
            written += 1
        else:
            errors.append({"item": item, "error": result.get("error")})
    return {"status": "ok", "written": written, "errors": errors}


def personalize_get(lead_id: int, *, layer: str = "merged") -> dict:
    conn = get_conn()
    if layer == "lead":
        rows = _lead_personalization_dict(conn, lead_id)
    elif layer == "company":
        row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        rows = _company_personalization_dict(conn, row["company_id"]) if row and row["company_id"] else {}
    else:
        conn.close()
        return resolve_personalization(lead_id)
    conn.close()
    out: dict = {}
    for fname, rec in rows.items():
        out[fname] = rec["field_value"]
        if rec.get("field_date"):
            out[f"{fname}_date"] = rec["field_date"]
    return out


def company_personalize_get(
    *,
    company_id: Optional[int] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    conn = get_conn()
    cid = resolve_company_id(conn, company_id=company_id, domain=domain, name=name)
    if not cid:
        conn.close()
        return {}
    rows = _company_personalization_dict(conn, cid)
    conn.close()
    out: dict = {}
    for fname, rec in rows.items():
        out[fname] = rec["field_value"]
        if rec.get("field_date"):
            out[f"{fname}_date"] = rec["field_date"]
    return out


def personalize_pending(fields: list[str], limit: int = 50) -> list[dict]:
    lead_fields = [f for f in fields if not is_company_personalization_field(f)]
    if not lead_fields:
        return []
    conn = get_conn()
    conditions = " OR ".join(
        "l.id NOT IN (SELECT lead_id FROM lead_personalization WHERE field_name = ?)"
        for _ in lead_fields
    )
    rows = conn.execute(
        f"SELECT l.id, l.name, l.email, l.company FROM leads l WHERE {conditions} LIMIT ?",
        (*lead_fields, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def company_personalize_pending(fields: list[str], limit: int = 50) -> list[dict]:
    company_fields = [f for f in fields if is_company_personalization_field(f)]
    if not company_fields:
        return []
    conn = get_conn()
    conditions = " OR ".join(
        """c.id NOT IN (SELECT company_id FROM company_personalization WHERE field_name = ?)"""
        for _ in company_fields
    )
    rows = conn.execute(
        f"""SELECT c.id AS company_id, c.name, c.domain,
                   (SELECT COUNT(*) FROM leads l WHERE l.company_id = c.id) AS lead_count
            FROM companies c
            WHERE ({conditions})
            ORDER BY lead_count DESC
            LIMIT ?""",
        (*company_fields, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def personalize_status() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    with_lead = conn.execute("SELECT COUNT(DISTINCT lead_id) FROM lead_personalization").fetchone()[0]
    stale = 0
    for row in conn.execute(
        "SELECT lead_id, field_name, source_hash FROM lead_personalization WHERE source_hash IS NOT NULL"
    ).fetchall():
        if _lead_source_hash(row["lead_id"], row["field_name"]) != row["source_hash"]:
            stale += 1
    conn.close()
    return {"total_leads": total, "personalized": with_lead, "pending": total - with_lead, "stale": stale}


def company_personalize_status() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    with_co = conn.execute("SELECT COUNT(DISTINCT company_id) FROM company_personalization").fetchone()[0]
    stale = 0
    for row in conn.execute(
        "SELECT company_id, field_name, source_hash FROM company_personalization WHERE source_hash IS NOT NULL"
    ).fetchall():
        if _company_source_hash(row["company_id"], row["field_name"]) != row["source_hash"]:
            stale += 1
    conn.close()
    return {"total_companies": total, "personalized": with_co, "pending": total - with_co, "stale": stale}


def personalize_clear(lead_id: Optional[int] = None, field: Optional[str] = None, clear_all: bool = False) -> dict:
    conn = get_conn()
    count = 0
    if clear_all:
        count += conn.execute("DELETE FROM lead_personalization").rowcount
        count += conn.execute("DELETE FROM company_personalization").rowcount
    elif lead_id and field:
        count = conn.execute(
            "DELETE FROM lead_personalization WHERE lead_id = ? AND field_name = ?", (lead_id, field),
        ).rowcount
    elif lead_id:
        count = conn.execute("DELETE FROM lead_personalization WHERE lead_id = ?", (lead_id,)).rowcount
    elif field:
        if is_company_personalization_field(field):
            count = conn.execute("DELETE FROM company_personalization WHERE field_name = ?", (field,)).rowcount
        else:
            count = conn.execute("DELETE FROM lead_personalization WHERE field_name = ?", (field,)).rowcount
    else:
        conn.close()
        return {"status": "error", "error": "Specify --lead-id, --field, or --all"}
    conn.commit()
    conn.close()
    return {"status": "ok", "deleted": count}


def build_company_sync_payload(conn: sqlite3.Connection, company_id: int) -> dict:
    row = conn.execute(
        "SELECT name, domain, industry, headcount FROM companies WHERE id = ?", (company_id,),
    ).fetchone()
    if not row:
        return {}
    payload: dict = {"name": row["name"]}
    if row["domain"]:
        payload["domain"] = row["domain"]
    if row["industry"]:
        payload["industry"] = row["industry"]
    if row["headcount"]:
        payload["headcount"] = row["headcount"]
    pers = _company_personalization_dict(conn, company_id)
    if pers:
        values, dates, at = _personalization_sync_payload(pers)
        payload["personalization"] = values
        if dates:
            payload["personalization_dates"] = dates
        if at:
            payload["personalization_at"] = at
    return payload


def apply_agent_company_sync_payload(company_id: int, payload: dict) -> None:
    _apply_personalization_payload(
        company_id, payload, table="company_personalization", id_col="company_id", entity_id=company_id,
    )


def _apply_personalization_payload(
    _entity_id_unused: int,
    payload: dict,
    *,
    table: str,
    id_col: str,
    entity_id: int,
) -> None:
    pers = payload.get("personalization") or {}
    if not pers:
        return
    dates = payload.get("personalization_dates") or {}
    p_at = payload.get("personalization_at", datetime.now(timezone.utc).isoformat())
    conn = get_conn()
    for fname, fval in pers.items():
        conn.execute(f"""
            INSERT INTO {table} ({id_col}, field_name, field_value, field_date, processed_at, cloud_pending)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT ({id_col}, field_name) DO UPDATE SET
                field_value = excluded.field_value,
                field_date = excluded.field_date,
                processed_at = excluded.processed_at,
                cloud_pending = 0
            WHERE excluded.processed_at > {table}.processed_at
        """, (entity_id, fname, fval, dates.get(fname), p_at))
    conn.commit()
    conn.close()


def cleanup_campaign_rules(dry_run: bool = False) -> dict:
    conn = get_conn()
    bad_rows = conn.execute("""
        SELECT id, workspace_id, source_platform, created_at
        FROM campaign_workspace_map
        WHERE campaign_id IS NULL AND campaign_name_normalized IS NULL
    """).fetchall()
    count = len(bad_rows)
    if not dry_run and count > 0:
        conn.execute("""
            DELETE FROM campaign_workspace_map
            WHERE campaign_id IS NULL AND campaign_name_normalized IS NULL
        """)
        conn.commit()
    conn.close()
    return {"status": "ok", "removed": count if not dry_run else 0, "found": count, "dry_run": dry_run}


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Outreach Magic — Pipeline visibility for Hermes")
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("init", help="Initialize the database")
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

    show_p = sub.add_parser("show", help="Show pipeline")
    show_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    show_p.add_argument("--stage")
    show_p.add_argument("--sentiment", choices=("positive", "negative", "neutral", "invalid"),
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
    lead_table_p.add_argument("--stage")
    lead_table_p.add_argument("--sentiment", choices=("positive", "negative", "neutral", "invalid"),
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
    stats_p.add_argument("--json", action="store_true")

    camp_p = sub.add_parser("campaigns", help="Event and lead counts by campaign name")
    camp_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    camp_p.add_argument("--json", action="store_true")

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
    imp_p.add_argument("--overwrite", action="store_true", help="Overwrite non-empty profile fields")
    imp_p.add_argument("--channel", default="email")
    imp_p.add_argument("--stage", default="prospecting")
    imp_p.add_argument("--notes")
    imp_p.add_argument("--workspace", help="Workspace slug/ID to associate imported leads with")
    imp_p.add_argument("--sender-profile", dest="sender_profile", help="LinkedIn sender profile URL for connection status tracking")
    imp_p.add_argument("--source-detail", dest="source_detail", help="Attribution source detail (e.g. list name)")
    imp_p.add_argument(
        "--import-batch-id",
        dest="import_batch_id",
        help="Stable batch id for name-only rows (import_key dedupe within batch)",
    )

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
    ver_p.add_argument("--batch", action="store_true", help="Read JSON array from --json")
    ver_p.add_argument("--json", dest="json_input", help="JSON array for batch mode")

    vers_p = sub.add_parser("verify-status", help="Check verification status for a lead")
    vers_p.add_argument("--lead-id", type=int)
    vers_p.add_argument("--email")

    verp_p = sub.add_parser("verify-pending", help="List leads needing email verification")
    verp_p.add_argument("--limit", type=int, default=50)
    verp_p.add_argument("--json", action="store_true")

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

    export_p = sub.add_parser("export", help="Export leads with personalization, tags, and sender")
    export_p.add_argument("--workspace", required=True, help="Workspace slug")
    export_p.add_argument("--tag", help="Filter by workspace tag")
    export_p.add_argument("--stage", help="Filter by workspace stage")
    export_p.add_argument("--since", help="Created/updated on or after date (YYYY-MM-DD or today)")
    export_p.add_argument("--limit", type=int, default=5000)
    export_p.add_argument("--format", choices=("csv", "json"), default="csv")
    export_p.add_argument("--file", help="Output path under project export/ (default auto-named)")

    agent_export_p = sub.add_parser("agent-changes", help="Show agent-created leads and events not yet synced")
    agent_export_p.add_argument("--json", action="store_true", help="Output as JSON (default)")
    agent_export_p.add_argument("--file", help="Write CSV to file (import-profiles compatible)")
    agent_export_p.add_argument("--all", action="store_true", help="Include all leads, not just locally-created")
    agent_export_p.add_argument("--workspace", help="Filter export to a specific workspace")

    up_p = sub.add_parser("update-stage", help="Update lead stage")
    up_p.add_argument("--id", type=int, required=True); up_p.add_argument("--stage", required=True)
    up_p.add_argument("--next-action")
    up_p.add_argument("--workspace", help="Workspace for this stage change (required in multi-workspace mode)")

    log_p = sub.add_parser("log-event", help="Log an outreach event")
    log_p.add_argument("--lead-id", type=int, required=True)
    log_p.add_argument("--type", dest="event_type", required=True)
    log_p.add_argument("--direction", default="outbound"); log_p.add_argument("--channel", default="email")
    log_p.add_argument("--subject"); log_p.add_argument("--body")
    log_p.add_argument("--workspace", help="Workspace for this event (required in multi-workspace mode)")

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
    sub.add_parser("logout", help="Clear local agent credentials")

    pull_p = sub.add_parser("pull", help="Pull events from relay to local database")
    pull_p.add_argument("--cron", action="store_true", help="Silent mode for cron")
    pull_p.add_argument("--full", action="store_true", help="Re-import all relay events (after DB reset)")
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

    refresh_p = sub.add_parser(
        "refresh",
        help="DANGER: sync, backup, wipe local DB, and pull --full from relay (rare)",
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

    sub.add_parser("status", help="Dashboard-style status: plan, connections, usage, routing")
    sync_p = sub.add_parser("sync", help="Push pending workspaces and routing rules to the webapp")
    sync_p.add_argument("--status", action="store_true", help="Show what needs syncing without pushing")
    sync_p.add_argument(
        "--inspect",
        metavar="EMAIL",
        help="Compare local activity vs sync payload for one lead (requires --workspace)",
    )
    sync_p.add_argument(
        "--workspace",
        help="Workspace slug for --inspect",
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
    archive_p = sub.add_parser("archive", help="Export workspace data to a separate SQLite file")
    archive_p.add_argument("--workspace", required=True, help="Workspace slug")
    archive_p.add_argument("--output", help="Output .db path (required unless --dry-run)")
    archive_p.add_argument("--dry-run", action="store_true", help="Show counts only")
    archive_p.add_argument("--purge", action="store_true", help="Remove exported data from main DB (requires --output)")
    archive_p.add_argument("--vacuum", action="store_true", help="Run VACUUM after --purge")

    conn_p = sub.add_parser("connections", help="List connected platforms with webhook URLs and stats")
    conn_p.add_argument("--json", action="store_true")

    cp_p = sub.add_parser("connect-platform", help="Generate a webhook URL for a platform")
    cp_p.add_argument("--platform", required=True,
                       help="Platform id (smartlead, instantly, heyreach, plusvibe, emailbison, etc.)")

    dp_p = sub.add_parser("disconnect-platform", help="Delete a platform webhook token (URL stops working)")
    dp_p.add_argument("--platform", required=True,
                       help="Platform id to disconnect")
    dp_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    hist_p = sub.add_parser("history", help="Show event history for a lead")
    hist_p.add_argument("--id", type=int, help="Lead ID")
    hist_p.add_argument("--email", help="Find lead by email")
    hist_p.add_argument("--linkedin", help="Find lead by LinkedIn URL or profile slug")
    hist_p.add_argument("--name", help="Find lead by name (partial match)")
    hist_p.add_argument("--workspace", help="Filter lead lookup by workspace name or slug")

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
        choices=("positive", "negative", "neutral", "invalid"),
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
    ws_sub.add_parser("list", help="List workspaces")
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

    cmap_p = sub.add_parser("campaign-map", help="Campaign to workspace routing")
    cmap_sub = cmap_p.add_subparsers(dest="campaign_map_cmd")
    cmap_sub.add_parser("list", help="List campaign maps")
    cmap_add = cmap_sub.add_parser("add", help="Add campaign map")
    cmap_add.add_argument("--platform", default="*")
    cmap_add.add_argument("--workspace", required=True, help="Workspace slug")
    cmap_add.add_argument("--campaign-id")
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
    q_skip = q_sub.add_parser("skip", help="Skip quarantined event (syncs to relay)")
    q_skip.add_argument("--id", required=True, help="Queue item id")
    q_assign = q_sub.add_parser("assign", help="Assign workspace (syncs to relay; ingested on next pull)")
    q_assign.add_argument("--id", required=True, help="Queue item id")
    q_assign.add_argument("--workspace", required=True, help="Workspace slug")
    q_replay = q_sub.add_parser("replay", help="Replay pending items locally after campaign-map rules")
    q_replay.add_argument("--workspace")
    q_replay.add_argument("--limit", type=int, default=100)

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

    # Check-only update notice (never downloads). At most once per hour.
    if args.command not in (None, "update", "version"):
        notify_update_available(quiet=getattr(args, "cron", False))

    if args.command == "version":
        print(f"outreachmagic {__version__}")
        return

    if args.command == "paths":
        payload: dict = {
            "install_dir": str(get_install_dir()),
            "data_root": str(get_data_root()),
            "skill_home": str(get_skill_home()),
            "database": str(get_db_path()),
            "config": str(get_config_path()),
            "project_root": str(get_project_root()),
        }
        warn = hermes_profile_copy_warning()
        if warn:
            payload["warning"] = warn
        print(json.dumps(payload, indent=2))
        if warn:
            print(f"\n⚠ {warn}", file=sys.stderr)
        return

    if args.command == "update":
        if args.check:
            if not check_skill_update(quiet=False):
                sys.exit(1)
            print(f"Up to date ({__version__})")
            return
        try:
            result = update_skill(explicit_tag=args.tag)
            print(f"Updated to v{result['version']} from {result['source']} in {result['path']}")
            print("Files:", ", ".join(result["files"]))
        except Exception as e:
            print(f"Update failed: {e}")
            sys.exit(1)
        return

    if args.command == "init":
        init_db()
        project_root = ensure_project_layout()
        print(f"Outreach Magic v{__version__} installed.")
        print(f"Database initialized: {get_db_path()}")
        print(f"Project root: {project_root}")
        print(f"  input/            → {get_input_dir()}")
        print(f"  export/           → {get_export_dir()}")
        print(f"  agent_resources/  → {get_agent_resources_dir()}")
        print()
        print("Next: run 'pipeline.py login' to connect your agent to Outreach Magic.")
        return

    # Commands that only talk to the app API (no local DB required)
    if args.command == "status":
        cmd_status()
        return

    if args.command == "refresh":
        cmd_refresh(args)
        return

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
            print(json.dumps(get_sync_status(), indent=2))
        else:
            if getattr(args, "full_snapshot_v2", False):
                mark_all_lead_snapshots_pending()
                print("Marked all leads and workspace memberships for snapshot v2 push.", flush=True)
            result = sync_all(no_health_report=getattr(args, "no_health_report", False))
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
                _mark_lead_cloud_pending(args.lead_id)
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

    if args.command == "login":
        login(
            platform=getattr(args, "platform", None),
            generate_url=getattr(args, "generate_url", False),
            claim_token=getattr(args, "claim_token", False),
            device_code=getattr(args, "device_code", None),
            wait_seconds=getattr(args, "wait", 30),
        )
        return
    if args.command == "logout":
        logout()
        return

    if not db_exists():
        print("Database not initialized. Run: pipeline.py init")
        sys.exit(1)

    migrate_db()
    sync_workspace_routing_mode_from_config()

    if args.command == "pull":
        agent_key = _require_agent_key()
        pull_stats = {}

        try:
            imported, skipped = sync_from_relay_org(
                agent_key,
                after_id=None if args.full else get_last_max_id(),
                full=args.full,
                debug_sentiment=args.debug_sentiment,
                quiet=args.cron,
                stats=pull_stats,
                skip_routing_sync=getattr(args, "skip_routing_sync", False),
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
            print("Run 'pipeline.py show' to see your updated pipeline.")
        return

    if args.command in ("show", "lead-table", "stats", "campaigns") and getattr(args, "pull", False):
        agent_key = get_agent_key()
        if agent_key:
            try:
                imported, _ = sync_from_relay_org(
                    agent_key, after_id=get_last_max_id(), quiet=True,
                )
                if imported:
                    print(f"Pulled from relay: {imported} new events imported.")
                else:
                    print("Pulled from relay: 0 new events imported.")
            except RuntimeError:
                pass
        print()

    if args.command == "show":
        auto_reply = None
        if getattr(args, "auto_reply", None) is not None:
            auto_reply = args.auto_reply == "true"
        try:
            leads = get_pipeline(
                stage_filter=args.stage,
                limit=args.limit,
                sentiment=getattr(args, "sentiment", None),
                auto_reply=auto_reply,
                lead_status=getattr(args, "lead_status", None),
                sort=getattr(args, "sort", "updated_at"),
                order=getattr(args, "order", "desc"),
                workspace=getattr(args, "workspace", None),
                since=getattr(args, "since", None),
                email=getattr(args, "email", None),
                name=getattr(args, "name", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        if getattr(args, "json", False):
            leads = enrich_lead_rows(leads, workspace=getattr(args, "workspace", None))
            print(json.dumps(leads, indent=2))
        else:
            print(format_pipeline_table(leads))
    elif args.command == "lead-table":
        auto_reply = None
        if getattr(args, "auto_reply", None) is not None:
            auto_reply = args.auto_reply == "true"
        try:
            leads = get_pipeline(
                stage_filter=args.stage,
                limit=args.limit,
                sentiment=getattr(args, "sentiment", None),
                auto_reply=auto_reply,
                lead_status=getattr(args, "lead_status", None),
                sort=getattr(args, "sort", "updated_at"),
                order=getattr(args, "order", "desc"),
                workspace=getattr(args, "workspace", None),
                since=getattr(args, "since", None),
                email=getattr(args, "email", None),
                name=getattr(args, "name", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        if getattr(args, "json", False):
            leads = enrich_lead_rows(leads, workspace=getattr(args, "workspace", None))
            print(json.dumps(leads, indent=2))
        else:
            print(format_lead_table(leads, markdown=getattr(args, "markdown", False)))
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
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if args.format == "json" and not getattr(args, "file", None):
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
    elif args.command == "stats":
        stats = get_stats()
        print(json.dumps(stats, indent=0) if getattr(args, "json", False) else format_stats(stats))
    elif args.command == "campaigns":
        stats = get_campaign_stats()
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
            source_detail=getattr(args, "source_detail", None),
            import_batch_id=getattr(args, "import_batch_id", None),
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "tag":
        action = getattr(args, "tag_action", None)
        if action == "repair":
            if not db_exists():
                print(json.dumps({"error": "Database not initialized. Run: pipeline.py init"}))
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
            items = json.loads(getattr(args, "json_input", None) or "[]")
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
                conn, DEFAULT_ORG_ID, ws_row["id"], args.id, status=args.stage)
            stage_ts = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE workspace_leads SET status = ?, stage_entered_at = ? WHERE id = ?",
                (args.stage, stage_ts, ws_lead_id))
            conn.commit()
            conn.close()
            result["workspace"] = ws_row["slug"]
        print(json.dumps(result))
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

        log_event(lead_id=args.lead_id, event_type=args.event_type, direction=args.direction,
                  channel=args.channel, subject=args.subject, body_preview=args.body)

        result = {"status": "logged", "lead_id": args.lead_id}
        if ws_row:
            conn = get_conn()
            ws_lead_id = upsert_workspace_lead(
                conn, DEFAULT_ORG_ID, ws_row["id"], args.lead_id,
                status="contacted" if args.event_type == "email_sent" else "prospecting")
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
            summary = get_workspace_summary(args.workspace)
            if summary.get("error"):
                print(json.dumps(summary, indent=2) if getattr(args, "json", False) else summary["error"])
                sys.exit(1)
            if getattr(args, "json", False):
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
        else:
            print(json.dumps(list_workspaces(), indent=2))
    elif args.command == "campaign-map":
        if args.campaign_map_cmd == "add":
            print(json.dumps(
                add_campaign_map_cli(
                    args.platform,
                    args.workspace,
                    campaign_id=args.campaign_id,
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
            print(json.dumps(skip_quarantine(args.id), indent=2))
        elif args.quarantine_cmd == "assign":
            print(json.dumps(assign_quarantine(args.id, args.workspace), indent=2))
        elif args.quarantine_cmd == "replay":
            print(json.dumps(replay_pending_quarantine(args.workspace, args.limit), indent=2))
        else:
            status = getattr(args, "status", "pending") or "pending"
            if getattr(args, "json", False):
                raw_limit = getattr(args, "limit", 0) or 0
                limit = raw_limit if raw_limit > 0 else 1000000
                print(json.dumps(list_quarantine(status=status, limit=limit), indent=2))
            elif status == "pending":
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

    if args.command in ("workspace", "campaign-map", "quarantine", "pull", "enrich", "stage", "import-profiles", None):
        try:
            hint = format_local_sync_hint(get_local_pending_counts())
            if hint:
                print(hint, file=sys.stderr)
        except Exception:
            pass



if __name__ == "__main__":
    main()