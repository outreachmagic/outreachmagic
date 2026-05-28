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
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relay_extractors import (
    build_display_name,
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
    replay_quarantine_item,
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
import workspace_archive


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

from om_paths import (
    ensure_project_layout,
    get_agent_resources_dir,
    get_config_path,
    get_db_path,
    get_export_dir,
    get_input_dir,
    get_project_root,
    get_skill_home,
    resolve_project_path,
)

SKILL_NAME = "outreachmagic"
RELAY_URL = "https://api.outreachmagic.io"

# Max chars stored in events.metadata_json["body"] (full copy for history / copy-insights).
# body_preview stays 200 chars. Prevents runaway HTML/base64 from bloating SQLite.
MAX_EVENT_BODY_STORAGE_CHARS = 65536

SKILL_SCRIPTS_DIR = f"skills/{SKILL_NAME}/scripts"
UPDATE_SCRIPT_FILES = (
    "pipeline.py",
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
SKILL_REPO_PATH = "skills/outreachmagic"
GITHUB_REPO = "outreachmagic/outreachmagic-skill"
GITHUB_RELEASES_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


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


def raw_repo_base_for_tag(tag: str) -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{normalize_release_tag(tag)}"


def scripts_base_for_tag(tag: str) -> str:
    return f"{raw_repo_base_for_tag(tag)}/{SKILL_REPO_PATH}/scripts"


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"OutreachMagic/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_latest_release() -> Optional[dict]:
    """Return latest GitHub release metadata or None if unavailable."""
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_LATEST,
            headers={
                "User-Agent": f"OutreachMagic/{__version__}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
        return None

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "tag": normalize_release_tag(tag),
        "version": release_tag_version(tag),
        "base": scripts_base_for_tag(tag),
    }


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


def fetch_update_manifest(repo_base: str) -> Optional[dict]:
    url = f"{repo_base.rstrip('/')}/{SKILL_REPO_PATH}/update-manifest.json"
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

    if explicit_tag:
        norm = normalize_release_tag(explicit_tag)
        return None, scripts_base_for_tag(norm), raw_repo_base_for_tag(norm), norm

    release = fetch_latest_release()
    if not release:
        raise RuntimeError(
            "No GitHub release found. Publish a release tag (e.g. v1.4.5), use "
            "pipeline.py update --tag v1.4.5, or set dev_repo in config for local development."
        )
    repo_base = release["base"].rsplit(f"/{SKILL_REPO_PATH}/scripts", 1)[0]
    return None, release["base"], repo_base, release["tag"]


def update_skill(explicit_tag: Optional[str] = None) -> dict:
    """Download or copy a tagged release into this skill install, then migrate DB."""
    dest = skill_scripts_dir()
    local_src, scripts_base, repo_base, source_label = resolve_update_source(explicit_tag)
    updated: list[str] = []
    manifest = None if local_src else fetch_update_manifest(repo_base)

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
            skill_md_url = f"{repo_base.rstrip('/')}/{SKILL_REPO_PATH}/SKILL.md"
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

PIPELINE_STAGES = [
    "prospecting", "contacted", "replied", "interested",
    "proposal", "won", "lost",
]

STAGE_EMOJI = {
    "prospecting": "○", "contacted": "●", "replied": "↔",
    "interested": "★", "proposal": "■", "won": "✔", "lost": "✖",
}

ATTRIBUTE_INSIGHT_FIELDS = ("title", "industry", "headcount")

# Personal inboxes — skip domain-wide company sync (would touch unrelated leads)
SHARED_EMAIL_DOMAINS = frozenset({
    "126.com", "163.com", "aim.com", "alice.it", "aol.com", "ameritech.net", "att.net",
    "bellsouth.net", "bigpond.com", "btinternet.com", "charter.net", "comcast.net", "cox.net", "cs.com",
    "daum.net", "earthlink.net", "email.com", "excite.com", "facebook.com", "flash.net", "free.fr",
    "frontier.com", "gmail.com", "gmx.com", "gmx.net", "googlemail.com", "hanmail.net", "hey.com",
    "hotmail.com", "hushmail.com", "icloud.com", "inbox.com", "instagram.com", "interia.pl", "juno.com",
    "laposte.net", "libero.it", "linkedin.com", "live.com", "lycos.com", "mac.com", "mail.com",
    "mail.ru", "mailfence.com", "me.com", "mindspring.com", "msn.com", "naver.com", "netscape.net",
    "netzero.net", "ntlworld.com", "o2.pl", "onet.pl", "optonline.net", "orange.fr", "outlook.com",
    "pacbell.net", "pm.me", "prodigy.net", "proton.me", "protonmail.com", "qq.com", "rediffmail.com",
    "roadrunner.com", "rocketmail.com", "rogers.com", "runbox.com", "sbcglobal.net", "sfr.fr", "shaw.ca",
    "sina.com", "sky.com", "swbell.net", "sympatico.ca", "talktalk.net", "t-online.de", "tuta.io",
    "tutanota.com", "twc.com", "verizon.net", "virgilio.it", "virginmedia.com", "wanadoo.fr", "web.de",
    "windstream.net", "wp.pl", "yahoo.com", "yandex.com", "yandex.ru", "ymail.com",
})


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
    resolved_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_status ON unmapped_campaign_queue(org_id, status, received_at);
CREATE INDEX IF NOT EXISTS idx_quarantine_campaign ON unmapped_campaign_queue(
    org_id, source_platform, campaign_id, status
);

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
    source_hash     TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    cloud_pending   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (lead_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_personalization_pending ON lead_personalization(cloud_pending) WHERE cloud_pending = 1;

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
"""


# ──────────────────────────────────────────────────────────────────────
# Database Operations
# ──────────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(str(get_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

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
            resolved_at TEXT
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
    repair_malformed_tags(conn)
    conn.commit()
    if own_conn:
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


_LINKEDIN_PLATFORMS = frozenset({"prosp", "heyreach"})

_CONNECTION_SENT_TYPES = frozenset({
    "send_connection", "linkedin_connection_sent", "linkedin_connect",
    "connection_request_sent",
})
_CONNECTION_ACCEPTED_TYPES = frozenset({
    "linkedin_connection_accepted", "connection_request_accepted",
    "connection_accepted", "linkedin_invite_accepted",
})


def normalize_event_sender(platform: str, sender: str) -> Optional[str]:
    """Normalize relay sender for storage; None if missing or unknown."""
    raw = (sender or "").strip()
    if not raw or raw.lower() == "unknown":
        return None
    plat = (platform or "").lower()
    if plat in _LINKEDIN_PLATFORMS:
        return normalize_linkedin(raw)
    return raw.lower()


def map_relay_local_event_type(envelope_event_type: str) -> str:
    """Map vendor webhook labels to local event_type for logging and LinkedIn status."""
    et = (envelope_event_type or "unknown").strip().lower()
    if et in _CONNECTION_SENT_TYPES:
        return "linkedin_connect"
    if et in _CONNECTION_ACCEPTED_TYPES:
        return "linkedin_connection_accepted"
    return et


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
                       ?, ?, ?, 1,
                       ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                name, company_id, company, title, industry, headcount, parse_headcount_numeric(headcount),
                email_norm, domain_from_email, li_public,
                location_city, location_state, location_country,
                channel, stage, notes,
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
    "headcount": ("headcount", "company_size", "employees", "employee_count"),
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
)

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

        p_items = []
        for key, val in extra.items():
            if key.startswith("mailmerge_") and val:
                p_items.append({
                    "lead_id": lead_id,
                    "field": key[len("mailmerge_"):],
                    "value": val,
                })
        if p_items:
            personalize_set_batch(p_items)
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


def _extract_bounce_details(raw: dict, platform: str) -> tuple:
    """Extract (bounce_type, bounce_reason) with platform-aware field mapping."""
    bounce_type = (
        raw.get("bounce_type")
        or raw.get("type")
        or "unknown"
    )
    if isinstance(bounce_type, str):
        bounce_type = bounce_type.strip().lower()
    else:
        bounce_type = "unknown"
    bounce_reason = (
        raw.get("bounce_reason")
        or raw.get("reason")
        or raw.get("error")
        or ""
    )
    if isinstance(bounce_reason, str):
        bounce_reason = bounce_reason.strip()
    else:
        bounce_reason = ""
    if "hard" in bounce_type:
        bounce_type = "hard"
    elif "soft" in bounce_type or "temporary" in bounce_type:
        bounce_type = "soft"
    return bounce_type, bounce_reason


def _compute_verification_status(conn: sqlite3.Connection, lead_id: int):
    """Compute consolidated verification status from all sources and materialize on leads."""
    rows = conn.execute(
        """SELECT status, sub_status, source, verified_at FROM lead_email_verification
           WHERE lead_id = ? ORDER BY verified_at DESC""",
        (lead_id,),
    ).fetchall()
    if not rows:
        return
    tool_rows = [r for r in rows if r["source"] != "platform_bounce"]
    bounce_rows = [r for r in rows if r["source"] == "platform_bounce"]
    status, verified_at = None, None
    if tool_rows:
        latest_tool = tool_rows[0]
        status, verified_at = latest_tool["status"], latest_tool["verified_at"]
        if latest_tool["status"] == "valid" and bounce_rows:
            hard_after = [
                b for b in bounce_rows
                if "hard" in (b["sub_status"] or "")
                and b["verified_at"] > latest_tool["verified_at"]
            ]
            if hard_after:
                status, verified_at = "bounced", hard_after[0]["verified_at"]
    elif bounce_rows:
        latest = bounce_rows[0]
        if "hard" in (latest["sub_status"] or ""):
            status, verified_at = "bounced", latest["verified_at"]
        else:
            status, verified_at = "soft_bounce", latest["verified_at"]
    if status:
        conn.execute(
            """UPDATE leads SET email_verification_status = ?, email_verified_at = ?,
               updated_at = datetime('now') WHERE id = ?""",
            (status, verified_at, lead_id),
        )


def _record_platform_bounce(
    conn: sqlite3.Connection,
    lead_id: int,
    email: str,
    platform: str,
    bounce_type: str,
    bounce_reason: str,
    event_at: Optional[str] = None,
):
    """Record a platform bounce in lead_email_verification and recompute materialized status."""
    org_id = DEFAULT_ORG_ID
    sub = "hard_bounce" if bounce_type == "hard" else "soft_bounce"
    ver_id = f"ver_{lead_id}_platform_bounce"
    now_ts = event_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, sub_status, source, source_detail,
            bounce_message, verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (org_id, lead_id, source) DO UPDATE SET
               status = excluded.status,
               sub_status = excluded.sub_status,
               source_detail = excluded.source_detail,
               bounce_message = excluded.bounce_message,
               verified_at = excluded.verified_at""",
        (ver_id, org_id, lead_id, email or "",
         "bounced" if bounce_type == "hard" else "soft_bounce",
         sub, "platform_bounce", f"{platform}:{bounce_type}",
         bounce_reason, now_ts),
    )
    _compute_verification_status(conn, lead_id)


def verify_email(
    lead_id: int,
    status: str,
    source: str,
    *,
    sub_status: Optional[str] = None,
    source_detail: Optional[str] = None,
    free_email: Optional[bool] = None,
    mx_found: Optional[bool] = None,
    smtp_provider: Optional[str] = None,
) -> dict:
    """Record an email verification result (from ZeroBounce, NeverBounce, etc.)."""
    conn = get_conn()
    row = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "error": f"Lead {lead_id} not found"}
    email = row["email"] or ""
    org_id = DEFAULT_ORG_ID
    ver_id = f"ver_{lead_id}_{source}"
    now_ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, sub_status, source, source_detail,
            free_email, mx_found, smtp_provider, verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (org_id, lead_id, source) DO UPDATE SET
               email = excluded.email,
               status = excluded.status,
               sub_status = excluded.sub_status,
               source_detail = excluded.source_detail,
               free_email = excluded.free_email,
               mx_found = excluded.mx_found,
               smtp_provider = excluded.smtp_provider,
               verified_at = excluded.verified_at""",
        (ver_id, org_id, lead_id, email, status, sub_status, source, source_detail,
         1 if free_email else (0 if free_email is not None else None),
         1 if mx_found else (0 if mx_found is not None else None),
         smtp_provider, now_ts),
    )
    _compute_verification_status(conn, lead_id)
    conn.commit()
    conn.close()
    return {"status": "recorded", "lead_id": lead_id, "verification_status": status, "source": source}


def verify_email_batch(results: list[dict]) -> dict:
    """Record multiple verification results at once."""
    conn = get_conn()
    org_id = DEFAULT_ORG_ID
    recorded = 0
    errors = []
    for item in results:
        lid = item.get("lead_id")
        if not lid:
            errors.append({"error": "missing lead_id", "item": item})
            continue
        row = conn.execute("SELECT email FROM leads WHERE id = ?", (lid,)).fetchone()
        if not row:
            errors.append({"error": f"Lead {lid} not found", "lead_id": lid})
            continue
        email = item.get("email") or row["email"] or ""
        status = item.get("status", "unknown")
        source = item.get("source", "unknown")
        ver_id = f"ver_{lid}_{source}"
        now_ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO lead_email_verification
               (id, org_id, lead_id, email, status, sub_status, source, source_detail,
                free_email, mx_found, smtp_provider, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (org_id, lead_id, source) DO UPDATE SET
                   email = excluded.email,
                   status = excluded.status,
                   sub_status = excluded.sub_status,
                   source_detail = excluded.source_detail,
                   free_email = excluded.free_email,
                   mx_found = excluded.mx_found,
                   smtp_provider = excluded.smtp_provider,
                   verified_at = excluded.verified_at""",
            (ver_id, org_id, lid, email, status, item.get("sub_status"),
             source, item.get("source_detail"),
             item.get("free_email"), item.get("mx_found"),
             item.get("smtp_provider"), now_ts),
        )
        _compute_verification_status(conn, lid)
        recorded += 1
    conn.commit()
    conn.close()
    return {"status": "batch_recorded", "recorded": recorded, "errors": errors}


def verify_status(lead_id: Optional[int] = None, email: Optional[str] = None) -> dict:
    """Check verification status for a lead."""
    conn = get_conn()
    if lead_id:
        row = conn.execute(
            "SELECT email_verification_status, email_verified_at FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "error": f"Lead {lead_id} not found"}
        records = conn.execute(
            """SELECT status, sub_status, source, source_detail, bounce_message,
                      verified_at FROM lead_email_verification
               WHERE lead_id = ? ORDER BY verified_at DESC""",
            (lead_id,),
        ).fetchall()
    elif email:
        email = normalize_email(email)
        row = conn.execute(
            "SELECT id, email_verification_status, email_verified_at FROM leads WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "error": f"No lead with email {email}"}
        lead_id = row["id"]
        records = conn.execute(
            """SELECT status, sub_status, source, source_detail, bounce_message,
                      verified_at FROM lead_email_verification
               WHERE lead_id = ? ORDER BY verified_at DESC""",
            (lead_id,),
        ).fetchall()
    else:
        conn.close()
        return {"status": "error", "error": "Provide --lead-id or --email"}
    conn.close()
    return {
        "lead_id": lead_id,
        "consolidated_status": row["email_verification_status"],
        "verified_at": row["email_verified_at"],
        "records": [dict(r) for r in records],
    }


def verify_pending(limit: int = 50) -> list[dict]:
    """List leads that have no verification record."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT l.id, l.email, l.name, l.company
           FROM leads l
           WHERE l.email IS NOT NULL
             AND l.email_verification_status IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM lead_email_verification v WHERE v.lead_id = l.id
             )
           ORDER BY l.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    """Truncate stored email/HTML body to MAX_EVENT_BODY_STORAGE_CHARS. Returns (text, was_truncated)."""
    if not body:
        return "", False
    limit = MAX_EVENT_BODY_STORAGE_CHARS
    if len(body) <= limit:
        return body, False
    return body[:limit], True


def log_event(lead_id, event_type, direction="outbound", channel="email",
              subject=None, body_preview=None, metadata=None, campaign=None,
              event_at=None, sender=None):
    meta = dict(metadata or {})
    if meta.get("body"):
        raw_body = str(meta["body"])
        original_len = len(raw_body)
        capped, truncated = cap_event_body(raw_body)
        meta["body"] = capped
        if truncated:
            meta["body_truncated"] = True
            meta["body_original_length"] = original_len
    campaign_name = campaign or meta.get("campaign")
    conn = get_conn()
    campaign_id = None
    if campaign_name and str(campaign_name).strip():
        campaign_id = ensure_campaign(conn, str(campaign_name).strip(), lead_id)
    preview = (body_preview or "")[:200]
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
    conn.commit()
    conn.close()


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
                "personalization", "personalization_at", "tags", "linkedin_status",
                "latest_sender", "latest_sender_platform", "linkedin",
                "lead_status", "lead_sentiment", "contact_order", "workspace_stage",
                "external_id", "company_domain", "hq_city", "hq_state", "hq_country",
            ):
                if key in snap and snap[key] is not None:
                    row[key] = snap[key]
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


def normalize_campaign_event_type(event_type: str, direction: str, channel: str) -> str:
    """Map raw event labels to reporting-friendly campaign event buckets."""
    et = (event_type or "unknown").strip().lower()
    flow = (direction or "").strip().lower()
    medium = (channel or "").strip().lower()
    if medium == "linkedin":
        if et in ("send_connection", "linkedin_connect", "linkedin_connection_sent"):
            return "linkedin_connection_sent"
        if et == "linkedin_connection_accepted":
            return "linkedin_connection_accepted"
        if et == "linkedin_reply":
            return "linkedin_message_reply"
        if et == "linkedin_message_sent":
            return "linkedin_message_sent"
        if et == "linkedin_message":
            return "linkedin_message_reply" if flow == "inbound" else "linkedin_message_sent"
    return et or "unknown"


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
    reply_events = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE lower(event_type) IN ('email_reply', 'linkedin_reply', 'linkedin_message')
              OR (lower(direction) = 'inbound' AND lower(event_type) = 'email')"""
    ).fetchone()[0]
    leads_with_replies = conn.execute(
        """SELECT COUNT(DISTINCT lead_id) FROM events
           WHERE lower(event_type) IN ('email_reply', 'linkedin_reply', 'linkedin_message')
              OR (lower(direction) = 'inbound' AND lower(event_type) = 'email')"""
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
    pending_lead_count = conn2.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    conn2.close()

    pending_total = len(pending_ws) + len(pending_maps)
    local_changes = local_lead_count + local_event_count + pending_lead_count
    return {
        "can_sync": True,
        "pending_workspaces": pending_ws,
        "pending_rules": pending_maps,
        "pending_local_leads": local_lead_count,
        "pending_local_events": local_event_count,
        "pending_lead_updates": pending_lead_count,
        "pending_total": pending_total + local_changes,
        "synced": pending_total == 0 and local_changes == 0,
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
    local_leads = status.get("pending_local_leads", 0)
    local_events = status.get("pending_local_events", 0)
    local_total = local_leads + local_events
    if ws:
        names = ", ".join(w["name"] for w in ws[:3])
        suffix = f" (+{len(ws) - 3} more)" if len(ws) > 3 else ""
        parts.append(f"{len(ws)} workspace{'s' if len(ws) != 1 else ''} ({names}{suffix})")
    if rules:
        parts.append(f"{len(rules)} routing rule{'s' if len(rules) != 1 else ''}")
    if local_total:
        parts.append(f"{local_total} agent event{'s' if local_total != 1 else ''}")
    items = ", ".join(parts)
    return f"\n⚠ Not synced to cloud: {items}. Run pipeline.py sync to push them."


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
    pending_lead_updates = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    conn.close()
    return {
        "workspaces": unsynced_ws,
        "rules": unsynced_rules,
        "events": local_events,
        "lead_updates": pending_lead_updates,
        "total": unsynced_ws + unsynced_rules + local_events + pending_lead_updates,
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
    if counts["events"]:
        parts.append(f"{counts['events']} agent event{'s' if counts['events'] != 1 else ''}")
    if counts.get("lead_updates"):
        parts.append(f"{counts['lead_updates']} lead update{'s' if counts['lead_updates'] != 1 else ''}")
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

    local_leads = status.get("pending_local_leads", 0)
    local_events = status.get("pending_local_events", 0)
    local_total = local_leads + local_events

    parts = []
    if total:
        parts.append(f"Synced {total} item{'s' if total != 1 else ''} to cloud.")
    agent_key = get_agent_key()
    if local_total and agent_key:
        pushed = _push_agent_events_to_relay(agent_key)
        results["agent_events_pushed"] = pushed
        if pushed > 0:
            parts.append(f"Pushed {pushed} agent event{'s' if pushed != 1 else ''} to relay.")
        elif local_total:
            parts.append(f"{local_total} agent event{'s' if local_total != 1 else ''} could not be pushed.")
    elif local_total:
        parts.append(
            f"{local_total} agent event{'s' if local_total != 1 else ''} pending — "
            f"no agent key configured to push them."
        )

    if agent_key:
        leads_pushed = _push_pending_lead_updates(agent_key)
        results["lead_updates_pushed"] = leads_pushed
        if leads_pushed > 0:
            parts.append(f"Pushed {leads_pushed} lead update{'s' if leads_pushed != 1 else ''} to relay.")

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


def _load_lead_sync_prefetch(
    conn: sqlite3.Connection,
    org_id: str,
    lead_ids: list[int],
) -> dict:
    """Bulk-load rows used by build_lead_sync_payload for many leads at once."""
    if not lead_ids:
        return {
            "leads": {},
            "identities": {},
            "external_ids": {},
            "workspace_slugs": {},
            "workspace_leads": {},
            "tags": {},
            "linkedin_status": {},
            "personalization": {},
        }

    placeholders = ",".join("?" for _ in lead_ids)
    leads = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT l.*,
                       co.domain AS company_domain,
                       co.hq_city AS hq_city,
                       co.hq_state AS hq_state,
                       co.hq_country AS hq_country,
                       COALESCE(co.name, l.company) AS company_display
                FROM leads l
                LEFT JOIN companies co ON l.company_id = co.id
                WHERE l.id IN ({placeholders})""",
            lead_ids,
        ).fetchall()
    }

    identities: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, identity_type, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders})
              AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id')""",
        [org_id, *lead_ids],
    ).fetchall():
        identities[r["lead_id"]].append(r)

    external_ids: dict[int, str] = {}
    for r in conn.execute(
        f"""SELECT lead_id, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders}) AND identity_type = 'external_id'""",
        [org_id, *lead_ids],
    ).fetchall():
        external_ids[r["lead_id"]] = r["identity_value_normalized"]

    workspace_slugs: dict[int, str] = {}
    for r in conn.execute(
        f"""SELECT wl.lead_id, w.slug
            FROM workspace_leads wl
            JOIN workspaces w ON wl.workspace_id = w.id
            WHERE wl.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        workspace_slugs.setdefault(r["lead_id"], r["slug"])

    workspace_leads: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, wl.status, wl.current_status_label,
                   wl.current_status_sentiment, wl.contact_priority
            FROM workspace_leads wl
            WHERE wl.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        workspace_leads.setdefault(r["lead_id"], r)

    tags: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT wl.lead_id, wlt.tag
            FROM workspace_lead_tags wlt
            JOIN workspace_leads wl ON wl.workspace_id = wlt.workspace_id AND wl.lead_id = wlt.lead_id
            WHERE wl.lead_id IN ({placeholders})
            ORDER BY wlt.created_at""",
        lead_ids,
    ).fetchall():
        tags[r["lead_id"]].append(r["tag"])

    linkedin_status: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT wl.lead_id, lis.sender_profile, lis.is_connected, lis.is_request_pending
            FROM workspace_lead_linkedin_status lis
            JOIN workspace_leads wl ON wl.workspace_id = lis.workspace_id AND wl.lead_id = lis.lead_id
            WHERE wl.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        linkedin_status[r["lead_id"]].append(r)

    personalization: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, field_name, field_value, processed_at
            FROM lead_personalization
            WHERE lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        personalization[r["lead_id"]].append(r)

    return {
        "leads": leads,
        "identities": identities,
        "external_ids": external_ids,
        "workspace_slugs": workspace_slugs,
        "workspace_leads": workspace_leads,
        "tags": tags,
        "linkedin_status": linkedin_status,
        "personalization": personalization,
    }


def _entity_key_from_prefetch(prefetch: dict, lead_id: int) -> str:
    row = prefetch["leads"].get(lead_id)
    if not row:
        return ""
    if row["email"]:
        return str(row["email"]).strip().lower()
    if row["linkedin_url"]:
        return str(row["linkedin_url"]).strip()
    id_rows = prefetch["identities"].get(lead_id) or []
    if id_rows:
        r = id_rows[0]
        return f"{r['identity_type']}:{r['identity_value_normalized']}"
    return ""


def _build_lead_sync_payload_from_prefetch(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    prefetch: dict,
    *,
    workspace_slug: Optional[str] = None,
) -> dict:
    """Same output as build_lead_sync_payload using preloaded maps."""
    row = prefetch["leads"].get(lead_id)
    if not row:
        return {}

    payload: dict = {}
    for field in (
        "name", "company", "title", "industry", "headcount", "stage", "notes",
        "location_city", "location_state", "location_country",
        "email_verification_status",
    ):
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if row["email"]:
        payload["email"] = row["email"]
    if row["linkedin_url"]:
        payload["linkedin"] = row["linkedin_url"]
    for id_row in prefetch["identities"].get(lead_id) or []:
        payload[id_row["identity_type"]] = id_row["identity_value_normalized"]
    if row["latest_sender"]:
        payload["latest_sender"] = row["latest_sender"]
    if row["latest_sender_platform"]:
        payload["latest_sender_platform"] = row["latest_sender_platform"]
    if row["email_verified_at"]:
        payload["email_verified_at"] = row["email_verified_at"]
    if row["company_domain"]:
        payload["company_domain"] = row["company_domain"]
    for hq in ("hq_city", "hq_state", "hq_country"):
        if row[hq]:
            payload[hq] = row[hq]
    ext = prefetch["external_ids"].get(lead_id)
    if ext:
        payload["external_id"] = ext
    if row["latest_source_detail"]:
        payload["list_source"] = row["latest_source_detail"]
    if row["original_source_detail"] and row["original_source_detail"] != row["latest_source_detail"]:
        payload["import_name"] = row["original_source_detail"]

    ws_id = None
    if workspace_slug:
        ws_row = resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if ws_id is None:
        wl = prefetch["workspace_leads"].get(lead_id)
        if wl:
            ws_id = wl["workspace_id"]

    if ws_id:
        wl_row = prefetch["workspace_leads"].get(lead_id)
        if wl_row:
            if wl_row["current_status_label"]:
                payload["lead_status"] = wl_row["current_status_label"]
            if wl_row["current_status_sentiment"]:
                payload["lead_sentiment"] = wl_row["current_status_sentiment"]
            if wl_row["contact_priority"] is not None:
                payload["contact_order"] = wl_row["contact_priority"]
            if wl_row["status"] and wl_row["status"] != row["stage"]:
                payload["workspace_stage"] = wl_row["status"]

        tag_rows = prefetch["tags"].get(lead_id) or []
        if tag_rows:
            payload["tags"] = tag_rows

        li_rows = prefetch["linkedin_status"].get(lead_id) or []
        if li_rows:
            payload["linkedin_status"] = [
                {
                    "sender_profile": r["sender_profile"],
                    "is_connected": bool(r["is_connected"]),
                    "is_request_pending": bool(r["is_request_pending"]),
                }
                for r in li_rows
            ]

    p_rows = prefetch["personalization"].get(lead_id) or []
    if p_rows:
        payload["personalization"] = {r["field_name"]: r["field_value"] for r in p_rows}
        payload["personalization_at"] = max(r["processed_at"] for r in p_rows)

    return payload


def build_lead_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: Optional[str] = None,
    prefetch: Optional[dict] = None,
) -> dict:
    """Full lead snapshot for relay push / agent replay (CSV import round-trip)."""
    if prefetch is not None:
        return _build_lead_sync_payload_from_prefetch(
            conn, org_id, lead_id, prefetch, workspace_slug=workspace_slug,
        )

    row = conn.execute(
        """SELECT l.*,
                  co.domain AS company_domain,
                  co.hq_city AS hq_city,
                  co.hq_state AS hq_state,
                  co.hq_country AS hq_country,
                  COALESCE(co.name, l.company) AS company_display
           FROM leads l
           LEFT JOIN companies co ON l.company_id = co.id
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    if not row:
        return {}

    payload: dict = {}
    for field in (
        "name", "company", "title", "industry", "headcount", "stage", "notes",
        "location_city", "location_state", "location_country",
        "email_verification_status",
    ):
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if row["email"]:
        payload["email"] = row["email"]
    if row["linkedin_url"]:
        payload["linkedin"] = row["linkedin_url"]
    for id_row in conn.execute(
        """SELECT identity_type, identity_value_normalized FROM lead_identities
           WHERE org_id = ? AND lead_id = ?
             AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id')""",
        (org_id, lead_id),
    ).fetchall():
        payload[id_row["identity_type"]] = id_row["identity_value_normalized"]
    if row["latest_sender"]:
        payload["latest_sender"] = row["latest_sender"]
    if row["latest_sender_platform"]:
        payload["latest_sender_platform"] = row["latest_sender_platform"]
    if row["email_verified_at"]:
        payload["email_verified_at"] = row["email_verified_at"]
    if row["company_domain"]:
        payload["company_domain"] = row["company_domain"]
    for hq in ("hq_city", "hq_state", "hq_country"):
        if row[hq]:
            payload[hq] = row[hq]
    ext = lead_external_id_value(conn, org_id, lead_id)
    if ext:
        payload["external_id"] = ext
    if row["latest_source_detail"]:
        payload["list_source"] = row["latest_source_detail"]
    if row["original_source_detail"] and row["original_source_detail"] != row["latest_source_detail"]:
        payload["import_name"] = row["original_source_detail"]

    ws_id = None
    if workspace_slug:
        ws_row = resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if ws_id is None:
        wl = conn.execute(
            "SELECT workspace_id FROM workspace_leads WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        if wl:
            ws_id = wl["workspace_id"]

    if ws_id:
        wl_row = conn.execute(
            """SELECT status, current_status_label, current_status_sentiment, contact_priority
               FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
        if wl_row:
            if wl_row["current_status_label"]:
                payload["lead_status"] = wl_row["current_status_label"]
            if wl_row["current_status_sentiment"]:
                payload["lead_sentiment"] = wl_row["current_status_sentiment"]
            if wl_row["contact_priority"] is not None:
                payload["contact_order"] = wl_row["contact_priority"]
            if wl_row["status"] and wl_row["status"] != row["stage"]:
                payload["workspace_stage"] = wl_row["status"]

        tag_rows = conn.execute(
            "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at",
            (ws_id, lead_id),
        ).fetchall()
        if tag_rows:
            payload["tags"] = [r["tag"] for r in tag_rows]

        li_rows = conn.execute(
            """SELECT sender_profile, is_connected, is_request_pending
               FROM workspace_lead_linkedin_status
               WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchall()
        if li_rows:
            payload["linkedin_status"] = [
                {
                    "sender_profile": r["sender_profile"],
                    "is_connected": bool(r["is_connected"]),
                    "is_request_pending": bool(r["is_request_pending"]),
                }
                for r in li_rows
            ]

    p_rows = conn.execute(
        "SELECT field_name, field_value, processed_at FROM lead_personalization WHERE lead_id = ?",
        (lead_id,),
    ).fetchall()
    if p_rows:
        payload["personalization"] = {r["field_name"]: r["field_value"] for r in p_rows}
        payload["personalization_at"] = max(r["processed_at"] for r in p_rows)

    return payload


def _resolve_lead_from_agent_sync(
    entity_key: str,
    payload: dict,
    *,
    stage: str = "prospecting",
) -> dict:
    """Create or match a lead from a relay agent entry (uses entity_key + full payload)."""
    extra = dict(import_extra_from_entity_key(entity_key))
    if payload.get("external_id"):
        extra["external_id"] = str(payload["external_id"])
    if payload.get("list_source"):
        extra["list_source"] = str(payload["list_source"])
    if payload.get("import_name"):
        extra["import_name"] = str(payload["import_name"])
    if payload.get("company_domain"):
        extra["company_domain"] = str(payload["company_domain"])
    profile = {
        k: payload[k]
        for k in ("email", "name", "company", "title", "industry", "headcount")
        if payload.get(k)
    }
    if payload.get("linkedin"):
        profile["linkedin"] = payload["linkedin"]
    return resolve_lead(
        email=payload.get("email"),
        linkedin_url=payload.get("linkedin"),
        name=payload.get("name", "Unknown"),
        company=payload.get("company"),
        title=payload.get("title"),
        industry=payload.get("industry"),
        headcount=payload.get("headcount"),
        stage=payload.get("stage") or payload.get("workspace_stage") or stage,
        notes=payload.get("notes"),
        company_domain=payload.get("company_domain"),
        location_city=payload.get("location_city"),
        location_state=payload.get("location_state"),
        location_country=payload.get("location_country"),
        hq_city=payload.get("hq_city"),
        hq_state=payload.get("hq_state"),
        hq_country=payload.get("hq_country"),
        import_extra=extra,
        import_batch=payload.get("import_batch_id"),
        source="agent_sync",
        source_platform="relay",
        overwrite=True,
    )


def apply_agent_lead_sync_payload(
    lead_id: int,
    payload: dict,
    *,
    org_id: str = DEFAULT_ORG_ID,
    workspace_id: Optional[str] = None,
    entity_key: Optional[str] = None,
) -> None:
    """Apply a full lead sync payload after create/match (import round-trip)."""
    update_fields = {
        k: v for k, v in payload.items()
        if k in ("name", "title", "industry", "company", "headcount") and v is not None
    }
    if update_fields:
        enrich_lead(lead_id, overwrite=True, **update_fields)

    loc_sets, loc_params = [], []
    for col in ("location_city", "location_state", "location_country"):
        if payload.get(col):
            loc_sets.append(f"{col} = ?")
            loc_params.append(payload[col])
    if loc_sets:
        loc_conn = get_conn()
        loc_params.append(lead_id)
        loc_conn.execute(
            f"UPDATE leads SET {', '.join(loc_sets)}, updated_at = datetime('now') WHERE id = ?",
            loc_params,
        )
        loc_conn.commit()
        loc_conn.close()

    if payload.get("company_domain") or any(payload.get(k) for k in ("hq_city", "hq_state", "hq_country")):
        c_conn = get_conn()
        domain = normalize_company_domain(payload.get("company_domain"))
        ensure_company(
            c_conn,
            name=payload.get("company"),
            domain=domain,
            industry=payload.get("industry"),
            headcount=payload.get("headcount"),
            hq_city=payload.get("hq_city"),
            hq_state=payload.get("hq_state"),
            hq_country=payload.get("hq_country"),
        )
        link_lead_company(
            c_conn, lead_id,
            company=payload.get("company"),
            email=payload.get("email"),
            industry=payload.get("industry"),
            headcount=payload.get("headcount"),
        )
        c_conn.commit()
        c_conn.close()

    id_conn = get_conn()
    identities: list[tuple[str, str]] = []
    if payload.get("external_id"):
        identities.append(("external_id", str(payload["external_id"])))
    itype, val = parse_entity_key(entity_key or "")
    if itype and val and itype != "email":
        if not any(t == itype and v == val for t, v in identities):
            identities.append((itype, val))
    if identities:
        upsert_all_identities(id_conn, org_id, lead_id, identities, source="agent_sync")
    id_conn.commit()
    id_conn.close()

    if workspace_id:
        status_label = (payload.get("lead_status") or "").strip().lower().replace("_", " ") or None
        status_sentiment = (payload.get("lead_sentiment") or "").strip().lower() or None
        contact_pri = None
        if payload.get("contact_order") is not None:
            try:
                contact_pri = int(payload["contact_order"])
            except (ValueError, TypeError):
                pass
        ws_conn = get_conn()
        ensure_organization(ws_conn)
        upsert_workspace_lead(
            ws_conn, org_id, workspace_id, lead_id,
            status=payload.get("workspace_stage") or payload.get("stage", "prospecting"),
            current_status_label=status_label,
            current_status_sentiment=status_sentiment,
            contact_priority=contact_pri,
        )
        if "tags" in payload:
            ws_conn.execute(
                "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
                (workspace_id, lead_id),
            )
            for tag in parse_tags_value(payload.get("tags")):
                tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
                ws_conn.execute(
                    """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                       VALUES (?, ?, ?, ?)""",
                    (tag_id, workspace_id, lead_id, tag),
                )
        for li in payload.get("linkedin_status") or []:
            sender = normalize_linkedin(li.get("sender_profile"))
            if not sender:
                continue
            is_connected = bool(li.get("is_connected"))
            is_pending = bool(li.get("is_request_pending"))
            if not is_connected and not is_pending:
                continue
            now_ts = datetime.now(timezone.utc).isoformat()
            li_id = f"lis_{workspace_id}_{lead_id}_{sender[:20]}"
            ws_conn.execute(
                """INSERT INTO workspace_lead_linkedin_status
                   (id, workspace_id, lead_id, sender_profile, is_connected,
                    is_request_pending, connected_at, request_sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (workspace_id, lead_id, sender_profile) DO UPDATE SET
                       is_connected = excluded.is_connected,
                       is_request_pending = excluded.is_request_pending,
                       updated_at = datetime('now')""",
                (li_id, workspace_id, lead_id, sender,
                 int(is_connected), int(is_pending),
                 now_ts if is_connected else None,
                 now_ts if is_pending else None),
            )
        ws_conn.commit()
        ws_conn.close()

    personalization = payload.get("personalization")
    if personalization:
        p_at = payload.get("personalization_at", datetime.now(timezone.utc).isoformat())
        p_conn = get_conn()
        for fname, fval in personalization.items():
            p_conn.execute("""
                INSERT INTO lead_personalization (lead_id, field_name, field_value, processed_at, cloud_pending)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT (lead_id, field_name) DO UPDATE SET
                    field_value = excluded.field_value,
                    processed_at = excluded.processed_at,
                    cloud_pending = 0
                WHERE excluded.processed_at > lead_personalization.processed_at
            """, (lead_id, fname, fval, p_at))
        p_conn.commit()
        p_conn.close()

    if payload.get("notes"):
        n_conn = get_conn()
        n_conn.execute(
            "UPDATE leads SET notes = ?, updated_at = datetime('now') WHERE id = ?",
            (payload["notes"], lead_id),
        )
        n_conn.commit()
        n_conn.close()

    if payload.get("email_verification_status"):
        verify_email(
            lead_id,
            str(payload["email_verification_status"]),
            "agent_sync",
        )


def _push_agent_events_to_relay(agent_key: str) -> int:
    """Push locally-created events to the Cloudflare relay /push endpoint."""
    export = export_local_changes()
    entries = export.get("entries") or []
    if not entries:
        return 0

    client_id = export.get("client_id", "unknown")
    total_pushed = 0

    for i in range(0, len(entries), 500):
        batch = entries[i : i + 500]
        body = json.dumps({"client_id": client_id, "entries": batch}).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/push",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {agent_key}",
                "User-Agent": f"OutreachMagic/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                total_pushed += result.get("pushed", 0)
        except Exception:
            break

    return total_pushed


def _push_pending_lead_updates(agent_key: str) -> int:
    """Push pending lead updates to Cloudflare relay /push endpoint."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT l.*, COALESCE(co.name, l.company) AS company_display
           FROM leads l
           LEFT JOIN companies co ON l.company_id = co.id
           WHERE l.cloud_pending = 1"""
    ).fetchall()
    if not rows:
        conn.close()
        return 0

    lead_ids = [row["id"] for row in rows]
    prefetch = _load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, lead_ids)

    client_id = get_or_create_client_id()
    entries = []
    pushed_ids = []
    for row in rows:
        entity_key = _entity_key_from_prefetch(prefetch, row["id"])
        if not entity_key:
            entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, row["id"])
        if not entity_key:
            continue
        ws_slug = prefetch["workspace_slugs"].get(row["id"]) or _lead_workspace_slug(conn, row["id"])
        payload = build_lead_sync_payload(
            conn, DEFAULT_ORG_ID, row["id"], workspace_slug=ws_slug, prefetch=prefetch,
        )
        entry: dict = {
            "action": "lead_update",
            "entity_key": entity_key,
            "timestamp": row["updated_at"],
            "payload": payload,
        }
        if ws_slug:
            entry["workspace"] = ws_slug
        entries.append(entry)
        pushed_ids.append(row["id"])

    conn.close()
    if not entries:
        return 0

    total_pushed = 0
    for i in range(0, len(entries), 500):
        batch = entries[i : i + 500]
        batch_ids = pushed_ids[i : i + 500]
        body = json.dumps({"client_id": client_id, "entries": batch}).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/push",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {agent_key}",
                "User-Agent": f"OutreachMagic/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                count = result.get("pushed", 0)
                total_pushed += count
                if count >= len(batch):
                    mark_conn = get_conn()
                    placeholders = ",".join("?" for _ in batch_ids)
                    mark_conn.execute(
                        f"UPDATE leads SET cloud_pending = 0 WHERE id IN ({placeholders})",
                        batch_ids,
                    )
                    mark_conn.execute(
                        f"UPDATE lead_personalization SET cloud_pending = 0 WHERE lead_id IN ({placeholders})",
                        batch_ids,
                    )
                    mark_conn.commit()
                    mark_conn.close()
                elif count > 0:
                    break
        except Exception:
            break

    return total_pushed


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


def list_quarantine(org_id: str = DEFAULT_ORG_ID, status: str = "pending", limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, source_platform, campaign_id, campaign_name_raw,
                  campaign_name_normalized, reason, status, received_at
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
                "   (or assign one manually: pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG)",
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


def assign_quarantine_and_replay(queue_id: str, workspace_slug: str) -> dict:
    conn = get_conn()
    ws = conn.execute(
        "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
        (DEFAULT_ORG_ID, workspace_slug),
    ).fetchone()
    if not ws:
        conn.close()
        return {"status": "error", "error": f"workspace not found: {workspace_slug}"}
    try:
        result = replay_quarantine_item(conn, queue_id, ws["id"])
    except ValueError as e:
        conn.close()
        return {"status": "error", "error": str(e)}
    if result.get("status") != "assigned":
        conn.close()
        return result
    row = conn.execute(
        "SELECT payload_json FROM unmapped_campaign_queue WHERE id = ?", (queue_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    if not row:
        return result
    try:
        event = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return {**result, "replay": "failed", "error": "invalid payload"}
    lead_id = ingest_relay_event(event, force_workspace_id=ws["id"])
    conn = get_conn()
    conn.execute(
        """UPDATE unmapped_campaign_queue SET status = 'replayed', resolved_at = datetime('now')
           WHERE id = ?""",
        (queue_id,),
    )
    conn.commit()
    conn.close()
    return {**result, "replay": "ok", "lead_id": lead_id}


def replay_pending_quarantine(workspace_slug: Optional[str] = None, limit: int = 100) -> dict:
    pending = list_quarantine(status="pending", limit=limit)
    replayed = skipped = 0
    for item in pending:
        if workspace_slug:
            r = assign_quarantine_and_replay(item["id"], workspace_slug)
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
            if not routing:
                conn.close()
                skipped += 1
                continue
            slug = conn.execute(
                "SELECT slug FROM workspaces WHERE id = ?", (routing.workspace_id,)
            ).fetchone()
            conn.close()
            if not slug:
                skipped += 1
                continue
            r = assign_quarantine_and_replay(item["id"], slug["slug"])
        if r.get("replay") == "ok":
            replayed += 1
        else:
            skipped += 1
    return {"replayed": replayed, "skipped": skipped}


# ──────────────────────────────────────────────────────────────────────
# Relay Integration (api.outreachmagic.io)
# ──────────────────────────────────────────────────────────────────────

PLUSVIBE_PLATFORMS = frozenset({"plusvibe"})

PLUSVIBE_REPLY_EVENTS = frozenset({
    "all_email_replies",
    "first_email_replies",
    "all_positive_replies",
})

PLUSVIBE_SENT_EVENTS = frozenset({"email_sent"})
PLUSVIBE_BOUNCE_EVENTS = frozenset({"bounced_email"})

AUTO_REPLY_LABELS = frozenset({
    "out_of_office",
    "ooo",
    "automatic_reply",
    "auto_reply",
})


def normalize_lead_status_display(label: str) -> str:
    """Underscores to spaces, matching backend normalize_lead_status_status."""
    if not label:
        return ""
    return label.strip().lower().replace("_", " ")


def is_auto_reply_label(label: str) -> bool:
    normalized = (label or "").strip().lower().replace(" ", "_")
    return normalized in AUTO_REPLY_LABELS or "out_of_office" in normalized


def normalize_plusvibe_event(event_type: str, raw: dict) -> tuple[str, str]:
    """Map PlusVibe webhook_event to local event type and direction."""
    et = (event_type or "").lower()
    label = (raw.get("label") or "").strip().lower()

    if et in PLUSVIBE_REPLY_EVENTS:
        return "email_reply", "inbound"
    if et in PLUSVIBE_SENT_EVENTS:
        return "email_sent", "outbound"
    if et in PLUSVIBE_BOUNCE_EVENTS:
        return "email_bounce", "outbound"
    if et.startswith("lead_marked_as_") or et.startswith("marked_as_"):
        return "lead_status_updated", "inbound"
    if label in ("interested", "not_interested", "out_of_office"):
        return "lead_status_updated", "inbound"
    if raw.get("direction", "").upper() == "IN":
        return et, "inbound"
    return et, "outbound"


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
            # Prefer explicit webhook event type over stale payload fields.
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
) -> Optional[str]:
    """Pipeline stage to apply after ingest; None = leave stage unchanged."""
    et = envelope_event_type.lower()
    label = (metadata.get("lead_status_raw") or raw.get("label") or "").lower()
    sentiment = (metadata.get("lead_status_sentiment") or "").lower()

    if platform in PLUSVIBE_PLATFORMS:
        # Bounce/invalid: record on event metadata only; do not force pipeline stage to lost.
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

    if local_type in ("email_reply", "linkedin_message") or et in (
        "email_reply",
        "linkedin_reply",
        "linkedin_message",
    ):
        return "replied"
    if local_type in ("email_sent",) or et in (
        "email_sent",
        "linkedin_connect",
        "linkedin_message_sent",
    ):
        return "contacted"
    return None


def relay_dedupe_key(event: dict) -> str:
    """Stable id so we can re-pull from the relay without duplicating local rows."""
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


def relay_already_ingested(dedupe_key: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM relay_ingested WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    conn.close()
    return row is not None


def mark_relay_ingested(dedupe_key: str, lead_id: int):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
        (dedupe_key, lead_id),
    )
    conn.commit()
    conn.close()


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
            # Full snapshot is sent via lead_update during explicit `pipeline.py sync`.
            continue
        lead_ids.add(lead_id)
        ws_slug = _lead_workspace_slug(conn, lead_id)
        entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, lead_id)
        entry = {
            "action": "lead_create",
            "entity_key": entity_key,
            "timestamp": row["created_at"],
        }
        if ws_slug:
            entry["workspace"] = ws_slug
        entry["payload"] = build_lead_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug=ws_slug,
        )
        entries.append(entry)

        if row["stage"] and row["stage"] != "prospecting":
            stage_entry: dict = {
                "action": "stage_change",
                "entity_key": entry["entity_key"],
                "timestamp": row["updated_at"],
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
            "timestamp": row["created_at"],
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
    lead_entries = [e for e in result.get("entries", []) if e["action"] == "lead_create"]
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

        # Org-wide actions: proceed without workspace
        if action in ("lead_create", "lead_update"):
            lead_id = find_lead_by_identifier(conn, entity_key) if entity_key else None
            conn.close()
            if not lead_id:
                result = _resolve_lead_from_agent_sync(entity_key, payload)
                if result.get("status") == "error":
                    mark_relay_ingested(dedupe_key, None)
                    return None
                lead_id = result.get("id")
            if lead_id:
                apply_agent_lead_sync_payload(
                    lead_id,
                    payload,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    entity_key=entity_key,
                )
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

    if lead_id:
        mark_relay_ingested(dedupe_key, lead_id)
    return lead_id


def pull_events_org(agent_key: str, since: Optional[str] = None, after_id: Optional[int] = None, platform: Optional[str] = None) -> dict:
    """Pull all org events from relay using an agent key (org-wide access)."""
    params = []
    if since:
        params.append(f"since={urllib.parse.quote(since)}")
    if after_id:
        params.append(f"after_id={after_id}")
    if platform:
        params.append(f"platform={urllib.parse.quote(platform)}")
    qs = f"?{'&'.join(params)}" if params else ""
    url = f"{RELAY_URL}/pull{qs}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"OutreachMagic/{__version__}",
            "Authorization": f"Bearer {agent_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": True, "status": e.code, "message": body}
    except urllib.error.URLError as e:
        return {"error": True, "message": str(e.reason)}

def sync_from_relay_org(
    agent_key: str,
    since: Optional[str] = None,
    after_id: Optional[int] = None,
    full: bool = False,
    debug_sentiment: bool = False,
    quiet: bool = False,
) -> tuple[int, int]:
    """Import relay events for the entire org using an agent key."""
    maybe_sync_routing_from_cloud(quiet=quiet)
    imported = skipped = 0
    page_after_id = None if full else (after_id or 0)

    pull_since = None if full else (since or get_last_pull())

    while True:
        result = pull_events_org(
            agent_key,
            since=pull_since,
            after_id=page_after_id if page_after_id else None,
        )
        if result.get("error"):
            raise RuntimeError(result.get("message", "pull failed"))

        events = result.get("events") or []
        if not events:
            break

        for event in events:
            try:
                ingested = ingest_relay_event(
                    event,
                    debug_sentiment=debug_sentiment,
                    quiet=True,
                )
            except Exception as exc:
                if not quiet:
                    rid = event.get("relay_id") or event.get("id") or "?"
                    print(f"Warning: skipped relay event {rid}: {exc}")
                skipped += 1
                continue
            if ingested is None:
                skipped += 1
            else:
                imported += 1

        page_after_id = result.get("max_id") or page_after_id

        if len(events) < 1000:
            break

    if page_after_id:
        set_last_max_id(page_after_id)
    set_last_pull(datetime.now(timezone.utc).isoformat())
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
""".strip()


def _clear_pull_cursors() -> None:
    cfg = load_config()
    cfg.pop("last_pull", None)
    cfg.pop("last_max_id", None)
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
            "message": str(exc),
            **result,
        }

    result["imported"] = imported
    result["skipped"] = skipped
    result["steps"].append("pull_full")
    result["message"] = (
        f"Refresh complete. Imported {imported} events, skipped {skipped} duplicates. "
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


def ingest_relay_event(
    event: dict,
    debug_sentiment: bool = False,
    force_workspace_id: Optional[str] = None,
    quiet: bool = False,
) -> Optional[int]:
    """Take a relay event and write it to the local SQLite database. Returns None if duplicate."""
    if event.get("platform") == "agent":
        return ingest_agent_entry(event, quiet=quiet)

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
    sender_norm = normalize_event_sender(platform, sender_raw)
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

    channel_map = {"smartlead": "email", "instantly": "email", "emailbison": "email",
                   "heyreach": "linkedin", "prosp": "linkedin",
                   "plusvibe": "email", "clay": "email"}
    channel = channel_map.get(platform, "email")

    campaign_ctx = extract_campaign_context(platform, event_fields, raw)
    workspace_id = force_workspace_id
    if not workspace_id:
        conn = get_conn()
        routing = resolve_workspace_for_ingest(conn, DEFAULT_ORG_ID, campaign_ctx)
        if not routing:
            quarantine_event(
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
                print(format_unmapped_campaign_message(campaign_ctx), file=sys.stderr)
            return None
        workspace_id = routing.workspace_id
        conn.close()

    profile = profile_from_relay_lead(lead_fields, identity, display_name)
    campaign_name_for_detail = event_fields.get("campaign") or campaign_ctx.campaign_name_raw
    upsert_result = upsert_lead_profile(
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
        identities = collect_identities_from_event(identity, raw, platform)
        if not identities:
            conn = get_conn()
            ensure_organization(conn)
            quarantine_event(
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
    if get_org_routing_config(conn, DEFAULT_ORG_ID).mode == WORKSPACE_ROUTING_SINGLE:
        ensure_default_org_workspace(conn)
    identities = collect_identities_from_event(identity, raw, platform)
    for itype, val in identities:
        try:
            upsert_identity_alias(conn, DEFAULT_ORG_ID, lead_id, itype, val, source=platform)
        except ValueError:
            enqueue_identity_conflict_merge(
                conn,
                DEFAULT_ORG_ID,
                lead_id,
                itype,
                val,
                source=platform,
            )
    conn.commit()
    conn.close()

    if platform in PLUSVIBE_PLATFORMS:
        local_type, direction = normalize_plusvibe_event(envelope_event_type, raw)
    else:
        event_type_map = {
            "email_sent": "email_sent", "email_open": "email_open",
            "email_reply": "email_reply", "email_bounce": "email_bounce",
            "email_click": "email_click", "email_unsubscribe": "email_unsubscribe",
            "linkedin_connect": "linkedin_connect",
            "linkedin_connection_accepted": "linkedin_connection_accepted",
            "linkedin_message": "linkedin_message",
            "linkedin_reply": "linkedin_message",
        }
        local_type = map_relay_local_event_type(envelope_event_type)
        if envelope_event_type in event_type_map:
            local_type = event_type_map[envelope_event_type]
        direction = (
            "inbound"
            if envelope_event_type in (
                "email_reply", "email_open", "email_click",
                "linkedin_connection_accepted", "linkedin_reply",
            )
            or local_type == "linkedin_connection_accepted"
            else "outbound"
        )

    subject = event_fields.get("subject") or f"{platform}: {envelope_event_type}"
    body = event_fields.get("body") or ""
    if body:
        body, _ = cap_event_body(body)
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

    if platform in PLUSVIBE_PLATFORMS:
        metadata.update(
            build_plusvibe_status_metadata(raw, signals, envelope_event_type)
        )

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

    log_event(
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
        platform, envelope_event_type, local_type, raw, metadata
    )
    if target_stage:
        update_lead_stage(lead_id, target_stage, event_at=event_time)

    ws_status = target_stage or "prospecting"
    conn = get_conn()
    ws_lead_id = upsert_workspace_lead(
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
    append_workspace_event(
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

    # Materialize status/sentiment on workspace_leads
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
        _update_lead_sender(conn, lead_id, workspace_id, sender_norm, platform, event_at_ts)

    if local_type in ("linkedin_connect", "linkedin_connection_accepted") and workspace_id:
        sender_li = sender_norm or normalize_linkedin(sender_raw)
        if sender_li:
            event_at_ts = received_at or datetime.now(timezone.utc).isoformat()
            upsert_linkedin_status(
                conn, workspace_id, lead_id, sender_li, local_type, event_at_ts
            )

    if local_type == "email_bounce":
        bounce_type, bounce_reason = _extract_bounce_details(raw, platform)
        _record_platform_bounce(
            conn, lead_id, email_hint, platform,
            bounce_type=bounce_type,
            bounce_reason=bounce_reason,
            event_at=received_at,
        )

    conn.commit()
    conn.close()

    mark_relay_ingested(dedupe_key, lead_id)
    return lead_id


def login(platform: Optional[str] = None):
    """Connect this machine via browser device authorization (GitHub CLI-style)."""
    try:
        import device_login
    except ModuleNotFoundError:
        # Allow `pipeline.py login` to work even when cwd/import paths differ.
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import device_login

    try:
        agent_key = device_login.run_device_login(load_config, platform=platform)
    except RuntimeError as exc:
        print(f"\nLogin failed: {exc}")
        sys.exit(1)
    _save_agent_key_and_validate(agent_key)


def setup():
    """Deprecated alias — use login."""
    print("Note: use 'pipeline.py login' for device authorization.\n")
    login()


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


# ──────────────────────────────────────────────────────────────────────
# Connection management (via app API)
# ──────────────────────────────────────────────────────────────────────

PLATFORM_LABELS = {
    "smartlead": "Smartlead",
    "instantly": "Instantly",
    "emailbison": "EmailBison",
    "plusvibe": "PlusVibe",
    "masterinbox": "MasterInbox",
    "heyreach": "HeyReach",
    "prosp": "Prosp",
    "clay": "Clay",
}


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
    events_used = data.get("eventsUsed", 0)
    events_limit = data.get("eventsLimit")
    resets_at = data.get("resetsAt", "")
    is_canceling = data.get("isCanceling", False)

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
    plan_suffix = ""
    if is_canceling:
        plan_suffix = " (canceling)"
    print(f"Plan: {plan}{plan_suffix}  |  Events this month: {usage_str}  |  Resets: {resets_label}")
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


PLATFORM_SETUP_HINTS = {
    "smartlead": "In Smartlead → Settings → Webhooks, paste the URL. Enable: Email Sent, Email Reply, Email Bounced.",
    "instantly": "In Instantly → Settings → Integrations → Webhooks, paste the URL. Enable all event types.",
    "heyreach": "In HeyReach → Settings → Webhooks, paste the URL. Enable all campaign events.",
    "plusvibe": "In PlusVibe → Settings → Webhooks, paste the URL. Subscribe to: ALL_EMAIL_REPLIES, LEAD_MARKED_AS_INTERESTED, LEAD_MARKED_AS_NOT_INTERESTED, LEAD_MARKED_AS_OUT_OF_OFFICE, and any other custom LEAD_MARKED_AS_<label> events you use. Set camp_ids to ALL. Optionally enable ignore_ooo and ignore_automatic to filter noise.",
    "emailbison": "In EmailBison → Integrations → Webhooks, paste the URL and enable relevant events.",
    "masterinbox": "In MasterInbox → Settings → Webhooks, paste the URL.",
    "prosp": "In Prosp → Settings → Webhooks, paste the URL.",
    "clay": "In Clay → Settings → Webhooks, paste the URL.",
}


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
# Formatting
# ──────────────────────────────────────────────────────────────────────

def format_pipeline_table(leads):
    if not leads:
        return "No leads in pipeline. Time to do some outreach!"
    lines = [f"{'Lead':<28} {'Company':<20} {'Stage':<14} {'Last':<12} {'Next Action'}", "-" * 95]
    for lead in leads:
        name = (lead["name"] or "")[:26]
        company = (lead.get("company_display") or lead.get("company") or "")[:18]
        stage = lead["stage"] or "?"
        emoji = STAGE_EMOJI.get(stage, "  ")
        last = lead.get("last_contact_at") or lead.get("last_event_at") or ""
        if last:
            try:
                dt = datetime.fromisoformat(last)
                now = datetime.now(timezone.utc)
                delta = now - dt.replace(tzinfo=timezone.utc)
                if delta.days:
                    last = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    last = f"{delta.seconds // 3600}h ago"
                else:
                    last = f"{delta.seconds // 60}m ago"
            except (ValueError, TypeError):
                last = last[:10]
        next_action = (lead.get("next_action") or "")[:30]
        status_bits = []
        if lead.get("current_sentiment"):
            status_bits.append(lead["current_sentiment"])
        if lead.get("current_is_auto_reply"):
            status_bits.append("auto")
        status_suffix = f" [{','.join(status_bits)}]" if status_bits else ""
        lines.append(
            f"{name:<28} {company:<20} {emoji} {stage:<12} {last:<12} {next_action}{status_suffix}"
        )
    return "\n".join(lines)


def format_lead_table(leads, markdown: bool = False):
    """Render stable lead rows from canonical show/get_pipeline fields."""
    if not leads:
        return "No leads found."

    headers = ["Lead", "Company", "Stage", "Last Event", "Last Event At", "Events", "Notes"]
    rows = []
    for lead in leads:
        rows.append(
            [
                (lead.get("name") or "—").strip() or "—",
                (lead.get("company_display") or lead.get("company") or "—").strip() or "—",
                (lead.get("stage") or "—").strip() or "—",
                (lead.get("last_event") or "—").strip() or "—",
                (lead.get("last_event_at") or "—").strip() or "—",
                str(int(lead.get("event_count") or 0)),
                (lead.get("notes") or "—").strip() or "—",
            ]
        )

    if markdown:
        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]
        for row in rows:
            safe_cells = [str(cell).replace("\n", " ").replace("|", "\\|") for cell in row]
            lines.append("| " + " | ".join(safe_cells) + " |")
        return "\n".join(lines)

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))
    lines = [
        "  ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(f"{str(cell):<{widths[i]}}" for i, cell in enumerate(row)))
    return "\n".join(lines)


def format_stats(stats):
    lines = [
        f"Pipeline: {stats['active_pipeline']} active | {stats['won']} won | "
        f"{stats['lost']} lost | {stats['total_leads']} total leads",
        f"Events: {stats['total_events']} total | {stats['events_7d']} in last 7 days",
        f"Replies: {stats.get('reply_events', 0)} events across {stats.get('replied_leads', 0)} leads "
        f"(stage 'replied' currently {stats.get('stages', {}).get('replied', 0)})",
        "Breakdown: " + ", ".join(f"{s}={c}" for s, c in stats.get("stages", {}).items()),
    ]
    campaign_lines = format_campaign_stats(stats, include_header=True)
    if campaign_lines:
        lines.append("")
        lines.extend(campaign_lines)
    return "\n".join(lines)


def format_campaign_stats(stats, include_header=False):
    campaigns = stats.get("campaigns") or []
    no_campaign = stats.get("no_campaign_events", 0)
    if not campaigns and not no_campaign:
        return []
    lines = []
    if include_header:
        lines.append("Campaigns:")
    workspace_w = max((len(c.get("workspace") or "-") for c in campaigns), default=9)
    workspace_w = max(workspace_w, len("Workspace"), 9)
    name_w = max((len(c.get("campaign_name") or c.get("campaign") or "") for c in campaigns), default=12)
    name_w = max(name_w, len("(no campaign)"), len("Campaign"), 12)
    lines.append(
        f"{'Workspace':<{workspace_w}}  {'Campaign':<{name_w}}  {'Events':>7}  {'Leads':>6}  {'Interested':>10}"
    )
    lines.append("-" * (workspace_w + name_w + 31))
    for row in campaigns:
        workspace = row.get("workspace") or "-"
        campaign_name = row.get("campaign_name") or row.get("campaign") or ""
        interested = int(row.get("interested_count") or 0)
        lines.append(
            f"{workspace:<{workspace_w}}  {campaign_name:<{name_w}}  "
            f"{row['event_count']:>7}  {row['lead_count']:>6}  {interested:>10}"
        )
    if no_campaign:
        lines.append(f"{'-':<{workspace_w}}  {'(no campaign)':<{name_w}}  {no_campaign:>7}  {'-':>6}  {'-':>10}")
    return lines

def format_event_timeline(lead, events):
    """Format a lead's event history as a timeline."""
    emoji = STAGE_EMOJI.get(lead.get("stage", ""), "")
    lines = [
        f"Lead:    {lead['name']} ({emoji} {lead.get('stage', '?')})",
        f"Title:   {lead.get('title') or '—'}",
        f"Email:   {lead.get('email') or '—'}",
        f"Company: {lead.get('company_display') or lead.get('company') or '—'}",
        f"Industry:{lead.get('industry') or '—'}  |  Headcount: {lead.get('headcount') or '—'}",
        f"Notes:   {lead.get('notes') or '—'}",
        "",
    ]
    if not events:
        lines.append("No events recorded yet.")
        return "\n".join(lines)

    lines.append(f"{'#':<4} {'When':<20} {'Event':<32} {'Details'}")
    lines.append("-" * 95)
    for i, e in enumerate(events, 1):
        created = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(created)
            now = datetime.now(timezone.utc)
            delta = now - dt.replace(tzinfo=timezone.utc)
            if delta.days:
                when = f"{delta.days}d ago"
            elif delta.seconds >= 3600:
                when = f"{delta.seconds // 3600}h ago"
            elif delta.seconds >= 60:
                when = f"{delta.seconds // 60}m ago"
            else:
                when = "just now"
        except (ValueError, TypeError):
            when = created[:16]

        direction = "←" if e.get("direction") == "inbound" else "→"
        evt = f"{direction} {e.get('event_type', '?')}"
        details = e.get("body_preview") or e.get("subject") or ""
        try:
            meta = json.loads(e.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        status_note = meta.get("lead_status_sentiment") or meta.get("lead_status_raw")
        if meta.get("is_auto_reply"):
            status_note = (status_note or "") + " auto_reply"
        if status_note:
            details = f"{status_note}: {details}" if details else str(status_note)
        if len(details) > 45:
            details = details[:42] + "..."
        lines.append(f"{i:<4} {when:<20} {evt:<32} {details}")

    return "\n".join(lines)


def format_copy_insights(insights: dict) -> str:
    counts = insights.get("counts") or {}
    best = insights.get("best_template")
    lines = [
        f"Positive leads: {counts.get('positive_leads', 0)}",
        f"Positive leads with copy captured: {counts.get('positive_with_copy', 0)}",
        f"Templates seen: {counts.get('templates_seen', 0)}",
        "",
        "Positive lead copy (full subject + body):",
        "-" * 95,
    ]
    for row in insights.get("positive_leads_copy") or []:
        lines.append(f"Lead #{row['lead_id']} — {row.get('lead_name') or 'Unknown'}")
        lines.append(f"Subject: {row.get('subject') or '—'}")
        lines.append("Body:")
        lines.append(row.get("body") or "—")
        lines.append("")

    lines.append("Template performance (first outbound email per lead):")
    lines.append("-" * 95)
    for t in (insights.get("templates_ranked") or [])[:10]:
        rate = round(100 * float(t.get("positive_rate") or 0), 1)
        lines.append(
            f"[{t['template_id']}] positives={t['positive_leads']}/{t['total_leads']} ({rate}%)"
        )
        lines.append(f"Subject template: {t.get('subject_template') or '—'}")
        lines.append("")

    if best:
        rate = round(100 * float(best.get("positive_rate") or 0), 1)
        lines.append("Best working template:")
        lines.append(f"- ID: {best['template_id']}")
        lines.append(
            f"- Performance: {best['positive_leads']}/{best['total_leads']} positive leads ({rate}%)"
        )
        lines.append(f"- Subject: {best.get('subject_template') or '—'}")
        lines.append("Body:")
        lines.append(best.get("body_template") or "—")

    return "\n".join(lines)


def format_segment_insights(insights: dict) -> str:
    counts = insights.get("counts") or {}
    lines = [
        f"Sent leads (at least one outbound email): {counts.get('sent_leads', 0)}",
        f"Positive leads matching filter: {counts.get('positive_leads_matching_filter', 0)}",
        f"Positive leads with sent email: {counts.get('positive_leads_with_sent_email', 0)}",
        "",
    ]

    insights_by_field = insights.get("insights_by_field") or {}
    for field in insights.get("filter", {}).get("fields") or []:
        rows = insights_by_field.get(field) or []
        lines.append(f"Best converting {field} values:")
        lines.append("-" * 95)
        if not rows:
            lines.append("No rows met your min-sent threshold.")
            lines.append("")
            continue
        for row in rows:
            rate = round(100 * float(row.get("conversion_rate") or 0), 1)
            lines.append(
                f"{row.get('value') or '—'}: {row.get('positive_leads', 0)}/{row.get('sent_leads', 0)} positive ({rate}%)"
            )
        lines.append("")

    titles = insights.get("recommended_job_titles") or []
    if titles:
        lines.append("Recommended job titles to source next:")
        lines.append("-" * 95)
        for title in titles[:10]:
            lines.append(f"- {title}")

    return "\n".join(lines).rstrip()


# ──────────────────────────────────────────────────────────────────────
# Personalization (mail-merge fields)
# ──────────────────────────────────────────────────────────────────────

_PERSONALIZATION_SOURCE_FIELDS = {
    "first_name": "name",
    "company_name": "company",
}


def _personalization_source_hash(lead_id: int, field_name: str) -> Optional[str]:
    source_col = _PERSONALIZATION_SOURCE_FIELDS.get(field_name)
    if not source_col:
        return None
    conn = get_conn()
    row = conn.execute(f"SELECT {source_col} FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    if not row or not row[source_col]:
        return None
    return hashlib.md5(row[source_col].encode()).hexdigest()[:8]


def personalize_set(lead_id: int, field_name: str, field_value: str) -> dict:
    source_hash = _personalization_source_hash(lead_id, field_name)
    conn = get_conn()
    conn.execute("""
        INSERT INTO lead_personalization (lead_id, field_name, field_value, source_hash, cloud_pending)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT (lead_id, field_name) DO UPDATE SET
            field_value = excluded.field_value,
            source_hash = excluded.source_hash,
            processed_at = datetime('now'),
            cloud_pending = 1
    """, (lead_id, field_name, field_value, source_hash))
    conn.commit()
    conn.close()
    return {"status": "ok", "lead_id": lead_id, "field": field_name}


def personalize_set_batch(items: list[dict]) -> dict:
    conn = get_conn()
    written = 0
    errors = []
    for item in items:
        lid = item.get("lead_id")
        fname = item.get("field")
        fval = item.get("value")
        if not lid or not fname or fval is None:
            errors.append({"item": item, "error": "missing lead_id, field, or value"})
            continue
        source_hash = _personalization_source_hash(lid, fname)
        conn.execute("""
            INSERT INTO lead_personalization (lead_id, field_name, field_value, source_hash, cloud_pending)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT (lead_id, field_name) DO UPDATE SET
                field_value = excluded.field_value,
                source_hash = excluded.source_hash,
                processed_at = datetime('now'),
                cloud_pending = 1
        """, (lid, fname, fval, source_hash))
        written += 1
    conn.commit()
    conn.close()
    return {"status": "ok", "written": written, "errors": errors}


def personalize_get(lead_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT field_name, field_value, source_hash, processed_at FROM lead_personalization WHERE lead_id = ?",
        (lead_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def personalize_pending(fields: list[str], limit: int = 50) -> list[dict]:
    conn = get_conn()
    conditions = " OR ".join(
        "l.id NOT IN (SELECT lead_id FROM lead_personalization WHERE field_name = ?)"
        for _ in fields
    )
    rows = conn.execute(
        f"SELECT l.id, l.name, l.email, l.company FROM leads l WHERE {conditions} LIMIT ?",
        (*fields, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def personalize_status() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    personalized_ids = conn.execute(
        "SELECT COUNT(DISTINCT lead_id) FROM lead_personalization"
    ).fetchone()[0]
    pending = total - personalized_ids

    stale = 0
    rows = conn.execute(
        "SELECT lead_id, field_name, source_hash FROM lead_personalization WHERE source_hash IS NOT NULL"
    ).fetchall()
    for row in rows:
        current = _personalization_source_hash(row["lead_id"], row["field_name"])
        if current and current != row["source_hash"]:
            stale += 1

    conn.close()
    return {"total_leads": total, "personalized": personalized_ids, "pending": pending, "stale": stale}


def personalize_clear(lead_id: Optional[int] = None, field: Optional[str] = None, clear_all: bool = False) -> dict:
    conn = get_conn()
    if clear_all:
        result = conn.execute("DELETE FROM lead_personalization")
        count = result.rowcount
    elif lead_id and field:
        result = conn.execute(
            "DELETE FROM lead_personalization WHERE lead_id = ? AND field_name = ?",
            (lead_id, field),
        )
        count = result.rowcount
    elif lead_id:
        result = conn.execute("DELETE FROM lead_personalization WHERE lead_id = ?", (lead_id,))
        count = result.rowcount
    elif field:
        result = conn.execute("DELETE FROM lead_personalization WHERE field_name = ?", (field,))
        count = result.rowcount
    else:
        conn.close()
        return {"status": "error", "error": "Specify --lead-id, --field, or --all"}
    conn.commit()
    conn.close()
    return {"status": "ok", "deleted": count}


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
    lead_table_p.add_argument("--markdown", action="store_true", help="Render as markdown table")
    lead_table_p.add_argument("--json", action="store_true")

    stats_p = sub.add_parser("stats", help="Pipeline statistics")
    stats_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    stats_p.add_argument("--json", action="store_true")

    camp_p = sub.add_parser("campaigns", help="Event and lead counts by campaign name")
    camp_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    camp_p.add_argument("--json", action="store_true")

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
    setup_p = sub.add_parser("setup", help="Alias for login (deprecated)")

    pull_p = sub.add_parser("pull", help="Pull events from relay to local database")
    pull_p.add_argument("--cron", action="store_true", help="Silent mode for cron")
    pull_p.add_argument("--full", action="store_true", help="Re-import all relay events (after DB reset)")
    pull_p.add_argument(
        "--debug-sentiment",
        action="store_true",
        help="Print raw vs normalized sentiment mapping during ingest",
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
        "--no-health-report",
        action="store_true",
        help="Skip aggregate local DB health POST to portal (lead sync still runs)",
    )
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
    q_list = q_sub.add_parser("list", help="List pending quarantined events by campaign")
    q_list.add_argument("--limit", type=int, default=0, help="Limit pending queue items in JSON mode (0 = all)")
    q_list.add_argument("--json", action="store_true", help="Output raw queue rows as JSON")
    q_assign = q_sub.add_parser("assign", help="Assign workspace and replay one item")
    q_assign.add_argument("--id", required=True, help="Queue item id")
    q_assign.add_argument("--workspace", required=True, help="Workspace slug")
    q_replay = q_sub.add_parser("replay", help="Replay assigned quarantine items")
    q_replay.add_argument("--workspace")
    q_replay.add_argument("--limit", type=int, default=100)

    pset = sub.add_parser("personalize-set", help="Write personalization values for a lead")
    pset.add_argument("--lead-id", type=int, help="Lead ID (single mode)")
    pset.add_argument("--field", help="Field name (single mode)")
    pset.add_argument("--value", help="Field value (single mode)")
    pset.add_argument("--batch", action="store_true", help="Read JSON array from --json")
    pset.add_argument("--json", dest="json_input", help="JSON array: [{lead_id, field, value}, ...]")

    pget = sub.add_parser("personalize-get", help="Read personalization for a lead")
    pget.add_argument("--lead-id", type=int, required=True)
    pget.add_argument("--json", action="store_true")

    ppend = sub.add_parser("personalize-pending", help="List leads missing personalization")
    ppend.add_argument("--fields", default="first_name,company_name", help="Comma-separated field names")
    ppend.add_argument("--limit", type=int, default=50)
    ppend.add_argument("--json", action="store_true")

    pstat = sub.add_parser("personalize-status", help="Personalization summary counts")
    pstat.add_argument("--json", action="store_true")

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
        if getattr(args, "status", False):
            print(json.dumps(get_sync_status(), indent=2))
        else:
            result = sync_all(no_health_report=getattr(args, "no_health_report", False))
            print(json.dumps(result, indent=2))
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

    if not db_exists():
        print("Database not initialized. Run: pipeline.py init")
        sys.exit(1)

    migrate_db()
    sync_workspace_routing_mode_from_config()

    if args.command == "login":
        login(platform=getattr(args, "platform", None))
        return
    if args.command == "setup":
        setup()
        return

    if args.command == "pull":
        agent_key = _require_agent_key()

        try:
            imported, skipped = sync_from_relay_org(
                agent_key,
                after_id=None if args.full else get_last_max_id(),
                full=args.full,
                debug_sentiment=args.debug_sentiment,
                quiet=args.cron,
            )
        except RuntimeError as e:
            if not args.cron:
                print(f"Pull failed: {e}")
            sys.exit(0)

        if imported == 0 and skipped == 0:
            if not args.cron:
                print("No events on relay.")
            sys.exit(0)

        if not args.cron:
            print(f"Pulled {imported} new, {skipped} already imported (relay archive unchanged).")
            if args.full:
                print("Full replay complete.")
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
        events = get_lead_events(lead["id"], args.limit)
        if args.json:
            enriched = enrich_lead_rows([lead], workspace=getattr(args, "workspace", None))
            lead_out = enriched[0] if enriched else dict(lead)
            print(json.dumps({"lead": lead_out, "events": events}, indent=2))
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
        if args.workspace_cmd == "create":
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
        if args.quarantine_cmd == "assign":
            print(json.dumps(assign_quarantine_and_replay(args.id, args.workspace), indent=2))
        elif args.quarantine_cmd == "replay":
            print(json.dumps(replay_pending_quarantine(args.workspace, args.limit), indent=2))
        else:
            if getattr(args, "json", False):
                raw_limit = getattr(args, "limit", 0) or 0
                limit = raw_limit if raw_limit > 0 else 1000000
                print(json.dumps(list_quarantine(limit=limit), indent=2))
            else:
                print(format_quarantine_campaign_summary(get_quarantine_campaign_summary()))
    elif args.command == "personalize-set":
        if args.batch:
            items = json.loads(args.json_input or "[]")
            print(json.dumps(personalize_set_batch(items), indent=2))
        else:
            if not args.lead_id or not args.field or args.value is None:
                print("Error: --lead-id, --field, and --value are required (or use --batch --json)")
                sys.exit(1)
            print(json.dumps(personalize_set(args.lead_id, args.field, args.value), indent=2))
    elif args.command == "personalize-get":
        result = personalize_get(args.lead_id)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            if not result:
                print(f"No personalization for lead {args.lead_id}")
            else:
                for row in result:
                    print(f"  {row['field_name']}: {row['field_value']}  (at {row['processed_at']})")
    elif args.command == "personalize-pending":
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        result = personalize_pending(fields, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} leads pending personalization (fields: {', '.join(fields)})")
            for r in result:
                print(f"  [{r['id']}] {r['name'] or '?'} — {r['email'] or ''} — {r['company'] or ''}")
    elif args.command == "personalize-status":
        result = personalize_status()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"Total leads: {result['total_leads']}")
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