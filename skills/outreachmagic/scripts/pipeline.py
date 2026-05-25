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
  pipeline.py connect --key abc123          # Connect to relay
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

import sqlite3
import json
import os
import sys
import csv
import argparse
import hashlib
import re
import shutil
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
    DEFAULT_ORG_ID,
    VALID_WORKSPACE_ROUTING_MODES,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    append_workspace_event,
    assign_campaign_map,
    collect_identities_from_event,
    ensure_default_org_workspace,
    ensure_organization,
    extract_campaign_context,
    find_lead_by_identity,
    format_unmapped_campaign_message,
    MULTI_WORKSPACE_HOLD_MESSAGE,
    get_org_routing_config,
    quarantine_event,
    replay_quarantine_item,
    resolve_workspace,
    resolve_workspace_for_ingest,
    upsert_identity_alias,
    upsert_workspace_lead,
)

import routing_cloud


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

from om_paths import get_config_path, get_db_path, get_skill_home

SKILL_NAME = "outreachmagic"
RELAY_URL = "https://api.outreachmagic.io"

SKILL_SCRIPTS_DIR = f"skills/{SKILL_NAME}/scripts"
UPDATE_SCRIPT_FILES = ("pipeline.py", "relay_extractors.py", "workspace_routing.py", "routing_cloud.py", "om_paths.py")
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

def get_token() -> Optional[str]:
    return load_config().get("token")

def get_last_pull() -> Optional[str]:
    return load_config().get("last_pull")

def set_last_pull(ts: str):
    cfg = load_config()
    cfg["last_pull"] = ts
    save_config(cfg)


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
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    company             TEXT,
    title               TEXT,
    industry            TEXT,
    headcount           TEXT,
    email               TEXT,
    email_domain        TEXT,
    linkedin_url        TEXT,
    linkedin_normalized TEXT,
    channel             TEXT NOT NULL DEFAULT 'email',
    stage               TEXT NOT NULL DEFAULT 'prospecting',
    notes               TEXT,
    tags                TEXT DEFAULT '[]',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_contact_at     TEXT,
    next_action         TEXT,
    next_action_at      TEXT
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique ON leads(linkedin_normalized) WHERE linkedin_normalized IS NOT NULL;
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
    workspace_routing_mode  TEXT NOT NULL DEFAULT 'multi',
    default_workspace_id    TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
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
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'prospecting',
    owner_user_id   TEXT,
    stage_entered_at TEXT,
    last_activity_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
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
    """)
    for col, col_type in [
        ("industry", "TEXT"), ("headcount", "TEXT"), ("email_domain", "TEXT"),
        ("company_id", "INTEGER"), ("linkedin_normalized", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """UPDATE leads SET email_domain = lower(substr(email, instr(email, '@') + 1))
           WHERE email LIKE '%@%' AND (email_domain IS NULL OR email_domain = '')"""
    )
    conn.execute(
        """UPDATE leads SET linkedin_normalized = lower(trim(replace(replace(
               replace(linkedin_url, 'https://', ''), 'http://', ''), 'www.', '')))
           WHERE linkedin_url IS NOT NULL AND linkedin_url != ''
             AND (linkedin_normalized IS NULL OR linkedin_normalized = '')"""
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
           ON leads(linkedin_normalized) WHERE linkedin_normalized IS NOT NULL"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL"
    )
    backfill_companies_from_leads(conn)
    backfill_campaigns_from_events(conn)
    backfill_plusvibe_status_metadata(conn)
    for col, col_type in [
        ("workspace_routing_mode", "TEXT NOT NULL DEFAULT 'multi'"),
        ("default_workspace_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE organizations ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    backfill_workspace_routing(conn)
    if own_conn:
        conn.commit()
        conn.close()


def backfill_workspace_routing(conn: sqlite3.Connection):
    """Identity aliases for all leads; workspace_leads/maps only in single-workspace mode."""
    ensure_organization(conn)
    config = get_org_routing_config(conn, DEFAULT_ORG_ID)

    leads = conn.execute(
        "SELECT id, email, linkedin_normalized FROM leads"
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
        if lead["linkedin_normalized"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       id, org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, ?, 'linkedin_url', ?, 'backfill', 1, datetime('now')
                   )""",
                (f"id_li_{lid}", DEFAULT_ORG_ID, lid, lead["linkedin_normalized"]),
            )

    if config.mode == WORKSPACE_ROUTING_MULTI:
        return

    workspace_id = config.default_workspace_id or ensure_default_org_workspace(conn)
    for lead in leads:
        lid = lead["id"]
        stage_row = conn.execute("SELECT stage FROM leads WHERE id = ?", (lid,)).fetchone()
        status = stage_row["stage"] if stage_row else "prospecting"
        upsert_workspace_lead(conn, DEFAULT_ORG_ID, workspace_id, lid, status=status)

    platforms = ("smartlead", "heyreach", "instantly", "plusvibe", "emailbison", "clay", "prosp")
    campaigns = conn.execute("SELECT name FROM campaigns").fetchall()
    for row in campaigns:
        name = (row["name"] or "").strip()
        if not name:
            continue
        for platform in platforms:
            assign_campaign_map(
                conn,
                DEFAULT_ORG_ID,
                source_platform=platform,
                workspace_id=workspace_id,
                campaign_name=name,
                match_strategy="name_exact",
            )


def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def normalize_linkedin(url: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (raw_url_stored, normalized_key) for matching."""
    if not url or not str(url).strip():
        return None, None
    raw = str(url).strip()
    norm = raw.lower()
    for prefix in ("https://", "http://"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if norm.startswith("www."):
        norm = norm[4:]
    norm = norm.rstrip("/")
    return raw, norm or None


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
            _update_company_fields(conn, cid, name, industry, headcount)
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
            _update_company_fields(conn, cid, None, industry, headcount)
            return cid
    display_name = name or (domain or "Unknown")
    cid = conn.execute(
        """INSERT INTO companies (name, domain, industry, headcount)
           VALUES (?, ?, ?, ?)""",
        (display_name, domain, industry, headcount),
    ).lastrowid
    return cid


def _update_company_fields(
    conn: sqlite3.Connection,
    company_id: int,
    name: Optional[str],
    industry: Optional[str],
    headcount: Optional[str],
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
        "SELECT id FROM leads WHERE linkedin_normalized = ?", (linkedin_norm,)
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
        _, norm = normalize_linkedin(linkedin)
        if norm:
            params = [*workspace_params, norm]
            row = conn.execute(
                f"""SELECT l.*, COALESCE(c.name, l.company) AS company_display
                   FROM leads l LEFT JOIN companies c ON l.company_id = c.id
                   {workspace_join}
                   WHERE l.linkedin_normalized = ?""",
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
        linkedin_url = keep["linkedin_url"] or other["linkedin_url"]
        _, linkedin_norm = normalize_linkedin(linkedin_url)
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
               linkedin_normalized = COALESCE(linkedin_normalized, ?),
               company_id = COALESCE(company_id, ?),
               company = COALESCE(NULLIF(trim(company), ''), ?),
               title = COALESCE(NULLIF(trim(title), ''), ?),
               industry = COALESCE(NULLIF(trim(industry), ''), ?),
               headcount = COALESCE(NULLIF(trim(headcount), ''), ?),
               stage = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (
                email, domain, linkedin_url, linkedin_norm, company_id,
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
    tags: Optional[list] = None,
    enrich_name: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    auto_merge: bool = True,
) -> dict:
    """Match or create lead by email and/or LinkedIn; optionally auto-merge duplicates."""
    email_norm = normalize_email(email)
    li_raw, li_norm = normalize_linkedin(linkedin_url)
    if not email_norm and not li_norm:
        return {"status": "error", "error": "email or linkedin required"}

    conn = get_conn()
    by_email = find_lead_by_email(conn, email_norm) if email_norm else None
    by_li = find_lead_by_linkedin(conn, li_norm) if li_norm else None

    if by_email and by_li and by_email != by_li and auto_merge and not dry_run:
        keep_id, merge_id = _pick_merge_keep_id(conn, by_email, by_li)
        conn.close()
        merge_leads(keep_id, merge_id, reason="auto_dual_identifier")
        conn = get_conn()
        lead_id = keep_id
        created = False
    elif by_email:
        lead_id, created = by_email, False
    elif by_li:
        lead_id, created = by_li, False
    else:
        lead_id, created = None, True

    if dry_run:
        conn.close()
        if created:
            return {
                "status": "created", "id": None, "email": email_norm,
                "linkedin": li_norm, "dry_run": True,
            }
        return {
            "status": "matched", "id": lead_id, "email": email_norm,
            "linkedin": li_norm, "dry_run": True,
        }

    if created:
        domain = email_domain(email_norm)
        company_id = ensure_company(
            conn, name=company, domain=domain, industry=industry, headcount=headcount,
        )
        cur = conn.execute(
            """INSERT INTO leads (name, company_id, company, title, industry, headcount,
               email, email_domain, linkedin_url, linkedin_normalized, channel, stage, notes, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, company_id, company, title, industry, headcount,
                email_norm, domain, li_raw, li_norm, channel, stage, notes,
                json.dumps(tags or []),
            ),
        )
        lead_id = cur.lastrowid
        conn.commit()
    else:
        sets, params = [], []
        if email_norm:
            sets.extend(["email = COALESCE(email, ?)", "email_domain = COALESCE(email_domain, ?)"])
            params.extend([email_norm, email_domain(email_norm)])
        if li_norm:
            sets.extend(["linkedin_url = COALESCE(linkedin_url, ?)",
                         "linkedin_normalized = COALESCE(linkedin_normalized, ?)"])
            params.extend([li_raw, li_norm])
        if sets:
            sets.append("updated_at = datetime('now')")
            params.append(lead_id)
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()

    conn.close()
    name_for_enrich = enrich_name if enrich_name is not None else name
    filled = enrich_lead(
        lead_id, name=name_for_enrich, title=title, industry=industry,
        company=company, headcount=headcount, overwrite=overwrite,
    )
    if email_norm:
        ensure_lead_domain(lead_id, email_norm)
    conn = get_conn()
    link_lead_company(conn, lead_id, company=company, email=email_norm,
                      industry=industry, headcount=headcount)
    conn.commit()
    conn.close()

    return {
        "status": "created" if created else "matched",
        "id": lead_id,
        "email": email_norm,
        "linkedin": li_norm,
        "filled": filled,
    }


def db_exists():
    return get_db_path().exists()

def add_lead(name, company=None, title=None, industry=None, headcount=None,
             email=None, linkedin_url=None,
             channel="email", stage="prospecting", notes=None, tags=None):
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
        tags=tags,
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
    tags: Optional[list] = None,
    enrich_name: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """Match by email and/or LinkedIn; create if missing; enrich profile and company link."""
    email = profile.get("email")
    linkedin = profile.get("linkedin")
    if not normalize_email(email) and not normalize_linkedin(linkedin)[1]:
        return {"status": "error", "error": "email or linkedin required"}

    name = profile.get("name")
    if not name:
        em = normalize_email(email)
        name = name_from_email(em) if em else "Unknown"

    return resolve_lead(
        email=email,
        linkedin_url=linkedin,
        name=name,
        company=profile.get("company"),
        title=profile.get("title"),
        industry=profile.get("industry"),
        headcount=profile.get("headcount"),
        channel=channel,
        stage=stage,
        notes=notes,
        tags=tags,
        enrich_name=enrich_name,
        dry_run=dry_run,
        overwrite=overwrite,
    )


def import_profiles(
    rows: list[dict],
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    channel: str = "email",
    stage: str = "prospecting",
    notes: Optional[str] = None,
) -> dict:
    """Import many profile rows (CSV dicts or JSON objects). Match key: email and/or linkedin."""
    summary: dict = {
        "processed": 0,
        "created": 0,
        "matched": 0,
        "enriched": 0,
        "domain_synced": 0,
        "errors": [],
        "results": [],
    }
    for i, raw in enumerate(rows):
        profile = normalize_profile_row(raw)
        if not profile.get("email") and not profile.get("linkedin"):
            summary["errors"].append({"row": i + 1, "error": "missing email or linkedin"})
            continue
        summary["processed"] += 1
        try:
            result = upsert_lead_profile(
                profile,
                channel=channel,
                stage=stage,
                notes=notes,
                dry_run=dry_run,
                overwrite=overwrite,
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
        if result.get("domain_synced"):
            summary["domain_synced"] += 1
    return summary


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


def update_lead_stage(lead_id, stage, next_action=None):
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Valid: {PIPELINE_STAGES}")
    conn = get_conn()
    conn.execute(
        """UPDATE leads SET stage = ?, updated_at = datetime('now'),
           next_action = CASE WHEN ? IS NOT NULL THEN ? ELSE next_action END WHERE id = ?""",
        (stage, next_action, next_action, lead_id),
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


def log_event(lead_id, event_type, direction="outbound", channel="email",
              subject=None, body_preview=None, metadata=None, campaign=None):
    meta = dict(metadata or {})
    campaign_name = campaign or meta.get("campaign")
    conn = get_conn()
    campaign_id = None
    if campaign_name and str(campaign_name).strip():
        campaign_id = ensure_campaign(conn, str(campaign_name).strip(), lead_id)
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, subject, body_preview,
                               metadata_json, campaign_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, event_type, direction, channel, subject, (body_preview or "")[:200],
         json.dumps(meta), campaign_id),
    )
    conn.execute(
        "UPDATE leads SET updated_at = datetime('now'), last_contact_at = datetime('now') WHERE id = ?",
        (lead_id,),
    )
    conn.commit()
    conn.close()

def get_lead_events(lead_id, limit=50):
    """Get all events for a lead, newest first."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, event_type, direction, channel, subject, body_preview,
                  metadata_json, created_at
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
                    SELECT COUNT(*)
                    FROM events e
                    WHERE e.campaign_id = c.id
                      AND lower(coalesce(json_extract(e.metadata_json, '$.lead_status_raw'), '')) = 'interested'
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


def maybe_sync_routing_from_cloud(token: Optional[str] = None, *, quiet: bool = False) -> bool:
    """Pull routing config from wbhk-app when a relay token is configured."""
    tok = token or get_token()
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
    tok = get_token()
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


def create_workspace(name: str, slug: Optional[str] = None, org_id: str = DEFAULT_ORG_ID) -> dict:
    slug = slug or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    tok = get_token()
    if routing_cloud.cloud_routing_enabled(load_config, tok):
        try:
            bundle = routing_cloud.push_workspace_create(
                routing_cloud.get_api_base(load_config),
                tok,
                name=name,
                slug=slug,
            )
            _apply_cloud_routing_bundle(bundle, org_id)
            created = next((w for w in bundle.get("workspaces") or [] if w.get("slug") == slug), None)
            if created:
                return {"status": "created", "id": created["id"], "name": created["name"], "slug": created["slug"]}
            return {"status": "created", "name": name, "slug": slug}
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}
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
    return {"status": "created", "id": ws_id, "name": name, "slug": slug}


def list_campaign_maps(org_id: str = DEFAULT_ORG_ID, platform: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    q = """SELECT m.*, w.name AS workspace_name FROM campaign_workspace_map m
           JOIN workspaces w ON w.id = m.workspace_id WHERE m.org_id = ?"""
    params: list = [org_id]
    if platform:
        q += " AND m.source_platform = ?"
        params.append(platform)
    q += " ORDER BY m.source_platform, m.priority, m.campaign_name_normalized"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_campaign_map_cli(
    platform: str,
    workspace_slug: str,
    *,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: Optional[str] = None,
    priority: int = 100,
) -> dict:
    if not campaign_id and not campaign_name:
        return {"status": "error", "error": "provide --campaign-id or --campaign-name"}
    strategy = match_strategy or ("id_exact" if campaign_id else "name_exact")
    tok = get_token()
    if routing_cloud.cloud_routing_enabled(load_config, tok):
        try:
            bundle = routing_cloud.push_campaign_map(
                routing_cloud.get_api_base(load_config),
                tok,
                source_platform=platform,
                workspace_slug=workspace_slug,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                match_strategy=strategy,
                priority=priority,
            )
            _apply_cloud_routing_bundle(bundle, DEFAULT_ORG_ID)
            return {"status": "created", "workspace_slug": workspace_slug, "match_strategy": strategy}
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}
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
    conn.commit()
    conn.close()
    return {"status": "created", "map_id": map_id, "workspace_id": ws["id"]}


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
            platform = str(row.get("source_platform") or "").strip() or "unknown"
            campaign_id = str(row.get("campaign_id") or "").strip()
            campaign_name = str(row.get("campaign") or "unknown").strip() or "unknown"
            if campaign_id:
                cmd = (
                    "   pipeline.py campaign-map add "
                    f"--platform {platform} --workspace WORKSPACE_SLUG --campaign-id {campaign_id}"
                )
            else:
                escaped = campaign_name.replace('"', '\\"')
                cmd = (
                    "   pipeline.py campaign-map add "
                    f'--platform {platform} --workspace WORKSPACE_SLUG --campaign-name "{escaped}"'
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
    result = replay_quarantine_item(conn, queue_id, ws["id"])
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


def pull_events(token: str, since: Optional[str] = None, after_id: Optional[int] = None) -> dict:
    """Pull events from the relay (events stay on Cloudflare; client dedupes)."""
    params = []
    if since:
        params.append(f"since={urllib.parse.quote(since)}")
    if after_id:
        params.append(f"after_id={after_id}")
    qs = f"?{'&'.join(params)}" if params else ""
    url = f"{RELAY_URL}/pull/{token}{qs}"

    req = urllib.request.Request(url, headers={"User-Agent": f"OutreachMagic/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": True, "status": e.code, "message": body}
    except urllib.error.URLError as e:
        return {"error": True, "message": str(e.reason)}

def sync_from_relay(
    token: str,
    since: Optional[str] = None,
    full: bool = False,
    ack: bool = False,
    debug_sentiment: bool = False,
    quiet: bool = False,
) -> tuple[int, int]:
    """Import relay events locally. Relay keeps all rows; we skip already-ingested keys."""
    maybe_sync_routing_from_cloud(token, quiet=quiet)
    imported = skipped = 0
    after_id = 0
    page_since = None if full else since

    while True:
        result = pull_events(
            token,
            since=page_since,
            after_id=after_id if after_id else None,
        )
        if result.get("error"):
            raise RuntimeError(result.get("message", "pull failed"))

        events = result.get("events") or []
        if not events:
            break

        for event in events:
            if ingest_relay_event(
                event,
                debug_sentiment=debug_sentiment,
                quiet=True,  # print one aggregate quarantine summary at end of pull
            ) is None:
                skipped += 1
            else:
                imported += 1

        if ack and result.get("max_id"):
            ack_events(token, result["max_id"])

        if len(events) < 1000:
            break
        after_id = result.get("max_id") or 0

    set_last_pull(datetime.now(timezone.utc).isoformat())
    if not quiet:
        print_quarantine_guidance()
    return imported, skipped


def ack_events(token: str, max_id: int):
    """Optional: hide events on relay (default pull does not ack — relay keeps archive)."""
    url = f"{RELAY_URL}/pull/{token}/ack"
    data = json.dumps({"max_id": max_id}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "User-Agent": "OutreachMagic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"error": True}

def ingest_relay_event(
    event: dict,
    debug_sentiment: bool = False,
    force_workspace_id: Optional[str] = None,
    quiet: bool = False,
) -> Optional[int]:
    """Take a relay event and write it to the local SQLite database. Returns None if duplicate."""
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
    envelope_event_type = event.get("event_type", "unknown")
    platform = event.get("platform", "unknown")
    sender = event.get("sender", "")
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
    upsert_result = upsert_lead_profile(
        profile,
        channel=channel,
        stage="prospecting",
        notes=f"Auto-imported from {platform} via relay",
        enrich_name=display_name if lead_fields.get("first_name") else None,
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
            conn.execute(
                """INSERT INTO lead_merge_jobs (id, org_id, keep_lead_id, merge_lead_id,
                       status, reason, audit_json)
                   VALUES (?, ?, ?, ?, 'pending', 'identity_conflict', ?)""",
                (
                    f"merge_{lead_id}_{val[:8]}",
                    DEFAULT_ORG_ID,
                    lead_id,
                    find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, val) or lead_id,
                    json.dumps({"identity_type": itype, "value": val}),
                ),
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
        local_type = event_type_map.get(envelope_event_type, envelope_event_type)
        direction = (
            "inbound"
            if envelope_event_type in (
                "email_reply", "email_open", "email_click",
                "linkedin_connection_accepted", "linkedin_reply",
            )
            else "outbound"
        )

    subject = event_fields.get("subject") or f"{platform}: {envelope_event_type}"
    body = event_fields.get("body") or ""
    body_preview = body[:200] if body else (f"From {sender}" if sender else "")

    metadata = {
        "source": "relay",
        "platform": platform,
        "relay_received_at": received_at,
    }
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
    )

    target_stage = relay_target_stage(
        platform, envelope_event_type, local_type, raw, metadata
    )
    if target_stage:
        update_lead_stage(lead_id, target_stage)

    ws_status = target_stage or "prospecting"
    conn = get_conn()
    ws_lead_id = upsert_workspace_lead(
        conn, DEFAULT_ORG_ID, workspace_id, lead_id, status=ws_status
    )
    if target_stage:
        conn.execute(
            "UPDATE workspace_leads SET status = ?, stage_entered_at = datetime('now') WHERE id = ?",
            (target_stage, ws_lead_id),
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
    conn.commit()
    conn.close()

    mark_relay_ingested(dedupe_key, lead_id)
    return lead_id


def connect(token: str):
    """Connect to the relay. Saves token and tests connection."""
    cfg = load_config()
    cfg["token"] = token
    save_config(cfg)

    api_base = routing_cloud.get_api_base(load_config)
    print(f"Routing API: {api_base}/api/routing-config")
    print(f"Relay pull:  {RELAY_URL}/pull/{{token}}")
    print()

    try:
        maybe_sync_routing_from_cloud(token)
    except RuntimeError as exc:
        print(f"Warning: could not sync routing config from app ({exc}). Using local routing.")

    result = pull_events(token)
    if result.get("error"):
        print(f"Connection test failed: {result.get('message', 'unknown error')}")
        print("Is your token correct?")
        sys.exit(1)

    count = result.get("count", 0)
    print(f"Connected! Found {count} buffered events on the relay.")
    print()
    print("Webhook URLs to paste into your platforms:")
    platforms = ["smartlead", "heyreach", "instantly", "plusvibe", "emailbison"]
    for p in platforms:
        print(f"  {p}: {RELAY_URL}/{p}/{token}")
    print()

    if count > 0:
        print("Importing events from relay (archive stays on Cloudflare)...")
        try:
            imported, skipped = sync_from_relay(token, since=get_last_pull(), full=not get_last_pull())
            print(f"Imported {imported} new, {skipped} already on disk.\n")
        except RuntimeError as e:
            print(f"Import failed: {e}\n")
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))
    else:
        print("No events yet. Run 'pipeline.py pull' after your platforms start sending webhooks.")

    print()
    print("Tip: Add a cron job to auto-pull every 15 minutes:")
    print("  hermes cron create --name 'outreach-pull' --schedule '*/15 * * * *' \\")
    print("    --command 'cd ~/.hermes/skills/outreachmagic/scripts && python3 pipeline.py pull --cron'")


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
                last = f"{delta.days}d ago" if delta.days else f"{delta.seconds//3600}h ago"
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
    show_p.add_argument("--json", action="store_true")

    lead_table_p = sub.add_parser("lead-table", help="Show canonical lead information table")
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
    lead_table_p.add_argument("--markdown", action="store_true", help="Render as markdown table")
    lead_table_p.add_argument("--json", action="store_true")

    stats_p = sub.add_parser("stats", help="Pipeline statistics")
    stats_p.add_argument("--json", action="store_true")

    camp_p = sub.add_parser("campaigns", help="Event and lead counts by campaign name")
    camp_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add-lead", help="Add a lead")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--company"); add_p.add_argument("--title")
    add_p.add_argument("--industry"); add_p.add_argument("--headcount")
    add_p.add_argument("--email"); add_p.add_argument("--linkedin")
    add_p.add_argument("--channel", default="email"); add_p.add_argument("--stage", default="prospecting")
    add_p.add_argument("--notes"); add_p.add_argument("--tags")

    imp_p = sub.add_parser(
        "import-profiles",
        help="Bulk import/enrich leads from CSV or JSON (match by email)",
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

    up_p = sub.add_parser("update-stage", help="Update lead stage")
    up_p.add_argument("--id", type=int, required=True); up_p.add_argument("--stage", required=True)
    up_p.add_argument("--next-action")

    log_p = sub.add_parser("log-event", help="Log an outreach event")
    log_p.add_argument("--lead-id", type=int, required=True)
    log_p.add_argument("--type", dest="event_type", required=True)
    log_p.add_argument("--direction", default="outbound"); log_p.add_argument("--channel", default="email")
    log_p.add_argument("--subject"); log_p.add_argument("--body")

    # ── Relay commands ──
    connect_p = sub.add_parser("connect", help="Connect to OutreachMagic webhook relay")
    connect_p.add_argument("--key", required=True, help="Your Outreach Magic token")

    pull_p = sub.add_parser("pull", help="Pull events from relay to local database")
    pull_p.add_argument("--key", help="Override token")
    pull_p.add_argument("--cron", action="store_true", help="Silent mode for cron")
    pull_p.add_argument("--full", action="store_true", help="Re-import all relay events (after DB reset)")
    pull_p.add_argument("--ack", action="store_true", help="Mark events pulled on relay (legacy; default keeps archive)")
    pull_p.add_argument(
        "--debug-sentiment",
        action="store_true",
        help="Print raw vs normalized sentiment mapping during ingest",
    )

    webhook_p = sub.add_parser("webhook-url", help="Show webhook URLs for your platforms")

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
    cmap_add.add_argument("--platform", required=True)
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
        print(f"Database initialized: {get_db_path()}")
        return

    if not db_exists():
        print("Database not initialized. Run: pipeline.py init")
        sys.exit(1)

    migrate_db()
    sync_workspace_routing_mode_from_config()

    if args.command == "connect":
        connect(args.key)
        return

    if args.command == "webhook-url":
        tok = get_token()
        if not tok:
            print("Not connected. Run: pipeline.py connect --key YOUR_TOKEN")
            sys.exit(1)
        print(f"Relay: {RELAY_URL}")
        print(f"Token: {tok}")
        print()
        for p in ["smartlead", "heyreach", "instantly", "plusvibe", "emailbison"]:
            print(f"  {RELAY_URL}/{p}/{tok}")
        return

    if args.command == "pull":
        tok = args.key or get_token()
        if not tok:
            print("Not connected. Run: pipeline.py connect --key YOUR_TOKEN")
            sys.exit(1)

        try:
            imported, skipped = sync_from_relay(
                tok,
                since=None if args.full else get_last_pull(),
                full=args.full,
                ack=args.ack,
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
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(leads, indent=2) if getattr(args, "json", False) else format_pipeline_table(leads))
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
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        if getattr(args, "json", False):
            print(json.dumps(leads, indent=2))
        else:
            print(format_lead_table(leads, markdown=getattr(args, "markdown", False)))
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
        tags = json.loads(args.tags) if args.tags else None
        print(json.dumps(add_lead(name=args.name, company=args.company, title=args.title,
                                   industry=args.industry, headcount=args.headcount,
                                   email=args.email, linkedin_url=args.linkedin,
                                   channel=args.channel, stage=args.stage, notes=args.notes, tags=tags)))
    elif args.command == "import-profiles":
        rows: list[dict] = []
        if args.file and args.json_data:
            print(json.dumps({"error": "Use --file or --json, not both"}))
            sys.exit(1)
        if args.file:
            path = Path(args.file).expanduser()
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
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "update-stage":
        update_lead_stage(args.id, args.stage, args.next_action)
        print(json.dumps({"status": "updated", "id": args.id, "stage": args.stage}))
    elif args.command == "log-event":
        log_event(lead_id=args.lead_id, event_type=args.event_type, direction=args.direction,
                  channel=args.channel, subject=args.subject, body_preview=args.body)
        print(json.dumps({"status": "logged", "lead_id": args.lead_id}))
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
        print(json.dumps({"lead": lead, "events": events}, indent=2)
              if args.json else format_event_timeline(lead, events))
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
            print(json.dumps(create_workspace(args.name, args.slug), indent=2))
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
            print(json.dumps(list_campaign_maps(platform=getattr(args, "platform", None)), indent=2))
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
    else:
        if not db_exists():
            init_db()
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))


if __name__ == "__main__":
    main()