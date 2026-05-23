#!/usr/bin/env python3
"""
Outreach Magic — Agent-First Lead Database for Hermes

One SQLite file. No MongoDB. No BigQuery. Just your leads, visible.

Architecture:
  ~/.hermes/outreach_magic.db    — Local SQLite database
  wbhk.org/{platform}/{key}      — Cloudflare Worker relay (optional)
  pipeline.py                    — CLI: show, pull, connect, log-event...

Usage:
  pipeline.py init                          # Create database
  pipeline.py connect --key abc123          # Connect to relay
  pipeline.py pull                          # Pull events from relay
  pipeline.py show                          # Print pipeline table
  pipeline.py add-lead --name "Jane" ...    # Add a lead
  pipeline.py import-profiles --file leads.csv  # Bulk enrich from CSV/JSON
  pipeline.py log-event --lead-id 1 ...     # Log outreach event
  pipeline.py history --id 1                # Show lead's event timeline
  pipeline.py history --email j@acme.com    # Look up by email
  pipeline.py history --name "Jane"         # Look up by name (partial)
  pipeline.py stats                         # Quick stats
  pipeline.py campaigns                   # Counts by campaign name
  pipeline.py update                        # Refresh skill scripts from GitHub
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


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

def get_db_path() -> Path:
    return get_hermes_home() / "outreach_magic.db"

def get_config_path() -> Path:
    return get_hermes_home() / "outreach_magic_config.json"

RELAY_URL = "https://wbhk.org"
DB_PATH = get_db_path()
CONFIG_PATH = get_config_path()

SKILL_NAME = "outreachmagic"
SKILL_SCRIPTS_DIR = f"skills/sales/{SKILL_NAME}/scripts"
UPDATE_SCRIPT_FILES = ("pipeline.py", "relay_extractors.py")
DEFAULT_UPDATE_BASE = "https://raw.githubusercontent.com/outreachmagic/hermes-agent/main/pipeline"


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


def update_base_url() -> str:
    cfg = load_config() if CONFIG_PATH.exists() else {}
    return (
        os.environ.get("OUTREACHMAGIC_UPDATE_URL")
        or cfg.get("update_url")
        or DEFAULT_UPDATE_BASE
    ).rstrip("/")


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"OutreachMagic/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_remote_version() -> Optional[str]:
    try:
        return _fetch_url(f"{update_base_url()}/VERSION").decode().strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


UPDATE_CHECK_INTERVAL = int(os.environ.get("OUTREACHMAGIC_UPDATE_INTERVAL", "3600"))


def auto_update_enabled() -> bool:
    if os.environ.get("OUTREACHMAGIC_SKIP_AUTO_UPDATE"):
        return False
    cfg = load_config() if CONFIG_PATH.exists() else {}
    return cfg.get("auto_update", True) is not False


def update_check_due() -> bool:
    cfg = load_config() if CONFIG_PATH.exists() else {}
    last = cfg.get("update_checked_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age >= UPDATE_CHECK_INTERVAL
    except (ValueError, TypeError):
        return True


def record_update_check():
    cfg = load_config()
    cfg["update_checked_at"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)


def maybe_auto_update(quiet: bool = False) -> bool:
    """Download newer scripts from GitHub if available. Returns True if files were updated."""
    if not auto_update_enabled() or not update_check_due():
        return False

    remote = fetch_remote_version()
    record_update_check()
    if not remote or parse_version(remote) <= parse_version(__version__):
        return False

    old = __version__
    try:
        result = update_skill()
        if not quiet:
            print(f"outreachmagic: auto-updated {old} → {result['version']}")
        return True
    except Exception as e:
        if not quiet:
            print(f"outreachmagic: auto-update failed ({e}), using {old}")
        return False


def check_skill_update(quiet: bool = False) -> bool:
    """Return True if installed scripts match or exceed remote VERSION."""
    remote = fetch_remote_version()
    if not remote or parse_version(remote) <= parse_version(__version__):
        return True
    if not quiet:
        print(f"Update available: {__version__} → {remote} (auto-update runs on next command)")
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


def update_skill() -> dict:
    """Copy or download latest pipeline scripts into this skill install, then migrate DB."""
    dest = skill_scripts_dir()
    dev_repo = os.environ.get("OUTREACHMAGIC_DEV_REPO")
    updated: list[str] = []

    if dev_repo:
        src = Path(dev_repo) / "pipeline"
        if not src.is_dir():
            raise FileNotFoundError(f"OUTREACHMAGIC_DEV_REPO has no pipeline/: {src}")
        for name in (*UPDATE_SCRIPT_FILES, "VERSION"):
            shutil.copy2(src / name, dest / name)
            updated.append(name)
    else:
        base = update_base_url()
        repo_base = base.rsplit("/pipeline", 1)[0]
        for name in (*UPDATE_SCRIPT_FILES, "VERSION"):
            (dest / name).write_bytes(_fetch_url(f"{base}/{name}"))
            updated.append(name)
        try:
            (dest.parent / "SKILL.md").write_bytes(_fetch_url(f"{repo_base}/skill/SKILL.md"))
            updated.append("SKILL.md")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass

    init_db()
    sync_skill_md_version()
    new_version = _read_version_file(dest / "VERSION")
    return {"status": "updated", "version": new_version, "files": updated, "path": str(dest)}

PIPELINE_STAGES = [
    "prospecting", "contacted", "replied", "interested",
    "proposal", "won", "lost",
]

STAGE_EMOJI = {
    "prospecting": "\u25cb", "contacted": "\u25cf", "replied": "\u2194",
    "interested": "\u2605", "proposal": "\u25a0", "won": "\u2714", "lost": "\u2716",
}

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

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def get_token() -> Optional[str]:
    return load_config().get("token")

def get_last_pull() -> Optional[str]:
    return load_config().get("last_pull")

def set_last_pull(ts: str):
    cfg = load_config()
    cfg["last_pull"] = ts
    save_config(cfg)


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
"""


# ──────────────────────────────────────────────────────────────────────
# Database Operations
# ──────────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    migrate_db(conn)
    conn.close()
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
    if own_conn:
        conn.commit()
        conn.close()


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


def find_lead(
    *,
    lead_id: Optional[int] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[dict]:
    conn = get_conn()
    row = None
    if lead_id:
        row = conn.execute(
            """SELECT l.*, COALESCE(c.name, l.company) AS company_display
               FROM leads l LEFT JOIN companies c ON l.company_id = c.id WHERE l.id = ?""",
            (lead_id,),
        ).fetchone()
    elif email:
        em = normalize_email(email)
        if em:
            row = conn.execute(
                """SELECT l.*, COALESCE(c.name, l.company) AS company_display
                   FROM leads l LEFT JOIN companies c ON l.company_id = c.id WHERE l.email = ?""",
                (em,),
            ).fetchone()
    elif linkedin:
        _, norm = normalize_linkedin(linkedin)
        if norm:
            row = conn.execute(
                """SELECT l.*, COALESCE(c.name, l.company) AS company_display
                   FROM leads l LEFT JOIN companies c ON l.company_id = c.id
                   WHERE l.linkedin_normalized = ?""",
                (norm,),
            ).fetchone()
    elif name:
        row = conn.execute(
            """SELECT l.*, COALESCE(c.name, l.company) AS company_display
               FROM leads l LEFT JOIN companies c ON l.company_id = c.id
               WHERE l.name LIKE ? LIMIT 1""",
            (f"%{name}%",),
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
    return DB_PATH.exists()

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


def get_copy_insights(lead_status: str = "interested", limit: int = 200) -> dict:
    """Analyze winning copy from current positive leads.

    Uses current lead status filter for "positives" and scores templates using the
    first outbound email sent to each lead (positive-hit count and hit rate).
    """
    positive_leads = get_pipeline(
        limit=limit,
        lead_status=lead_status,
        sort="updated_at",
        order="desc",
    )
    positive_by_id = {int(lead["id"]): lead for lead in positive_leads}
    positive_ids = sorted(positive_by_id.keys())

    conn = get_conn()
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
        "filter": {"lead_status": lead_status, "limit": limit},
        "counts": {
            "positive_leads": len(positive_leads),
            "positive_with_copy": len(positive_copy),
            "templates_seen": len(ranked_templates),
        },
        "positive_leads_copy": positive_copy,
        "templates_ranked": ranked_templates,
        "best_template": ranked_templates[0] if ranked_templates else None,
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
        {company_join}
        INNER JOIN ranked_status rs ON rs.lead_id = l.id AND rs.rn = 1
        WHERE 1=1
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
        {company_join}
        WHERE 1=1
        """
    params: list = []
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

def get_campaign_stats():
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.name AS campaign,
                  (SELECT COUNT(*) FROM events e WHERE e.campaign_id = c.id) AS event_count,
                  (SELECT COUNT(*) FROM campaign_leads cl WHERE cl.campaign_id = c.id) AS lead_count
           FROM campaigns c
           ORDER BY event_count DESC, c.name"""
    ).fetchall()
    no_campaign_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE campaign_id IS NULL"
    ).fetchone()[0]
    conn.close()
    campaigns = [dict(r) for r in rows]
    return {"campaigns": campaigns, "no_campaign_events": no_campaign_events}


def get_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    stage_counts = get_stage_counts()
    active = sum(v for k, v in stage_counts.items() if k not in ("won", "lost"))
    recent = conn.execute("SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')").fetchone()[0]
    conn.close()
    stats = {"total_leads": total, "total_events": events, "active_pipeline": active,
             "won": stage_counts.get("won", 0), "lost": stage_counts.get("lost", 0),
             "events_7d": recent, "stages": stage_counts}
    stats.update(get_campaign_stats())
    return stats

def get_lead_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ──────────────────────────────────────────────────────────────────────
# Relay Integration (wbhk.org)
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
) -> tuple[int, int]:
    """Import relay events locally. Relay keeps all rows; we skip already-ingested keys."""
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
            if ingest_relay_event(event, debug_sentiment=debug_sentiment) is None:
                skipped += 1
            else:
                imported += 1

        if ack and result.get("max_id"):
            ack_events(token, result["max_id"])

        if len(events) < 1000:
            break
        after_id = result.get("max_id") or 0

    set_last_pull(datetime.now(timezone.utc).isoformat())
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

def ingest_relay_event(event: dict, debug_sentiment: bool = False) -> Optional[int]:
    """Take a relay event and write it to the local SQLite database. Returns None if duplicate."""
    dedupe_key = relay_dedupe_key(event)
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

    profile = profile_from_relay_lead(lead_fields, identity, display_name)
    upsert_result = upsert_lead_profile(
        profile,
        channel=channel,
        stage="prospecting",
        notes=f"Auto-imported from {platform} via relay",
        enrich_name=display_name if lead_fields.get("first_name") else None,
    )
    if upsert_result.get("status") == "error":
        return None
    lead_id = upsert_result["id"]

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

    mark_relay_ingested(dedupe_key, lead_id)
    return lead_id


def connect(token: str):
    """Connect to the relay. Saves token and tests connection."""
    cfg = load_config()
    cfg["token"] = token
    save_config(cfg)

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
    print("    --command 'cd ~/.hermes/skills/sales/outreachmagic/scripts && python3 pipeline.py pull --cron'")


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

def format_stats(stats):
    lines = [
        f"Pipeline: {stats['active_pipeline']} active | {stats['won']} won | "
        f"{stats['lost']} lost | {stats['total_leads']} total leads",
        f"Events: {stats['total_events']} total | {stats['events_7d']} in last 7 days",
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
    name_w = max((len(c["campaign"]) for c in campaigns), default=12)
    name_w = max(name_w, len("(no campaign)"), 12)
    lines.append(f"{'Campaign':<{name_w}}  {'Events':>7}  {'Leads':>6}")
    lines.append("-" * (name_w + 18))
    for row in campaigns:
        lines.append(
            f"{row['campaign']:<{name_w}}  {row['event_count']:>7}  {row['lead_count']:>6}"
        )
    if no_campaign:
        lines.append(f"{'(no campaign)':<{name_w}}  {no_campaign:>7}")
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


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Outreach Magic — Pipeline visibility for Hermes")
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("init", help="Initialize the database")
    sub.add_parser("version", help="Print installed outreachmagic version")

    update_p = sub.add_parser("update", help="Update skill scripts from GitHub (or OUTREACHMAGIC_DEV_REPO)")
    update_p.add_argument("--check", action="store_true", help="Only check for updates, do not install")

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
    show_p.add_argument("--json", action="store_true")

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
    connect_p = sub.add_parser("connect", help="Connect to wbhk.org relay")
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
    copy_p.add_argument("--json", action="store_true")

    args = parser.parse_args()

    # Auto-update from GitHub (default on, checks at most once per hour). Re-exec so this run uses new code.
    if (
        args.command not in (None, "update", "version")
        and not os.environ.get("OUTREACHMAGIC_REEXEC")
    ):
        if maybe_auto_update(quiet=getattr(args, "cron", False)):
            os.environ["OUTREACHMAGIC_REEXEC"] = "1"
            os.execv(sys.executable, [sys.executable, *sys.argv])

    if args.command == "version":
        print(f"outreachmagic {__version__}")
        return

    if args.command == "update":
        if args.check:
            remote = fetch_remote_version()
            if remote and parse_version(remote) > parse_version(__version__):
                print(f"Update available: {__version__} → {remote}")
                sys.exit(1)
            print(f"Up to date ({__version__})")
            return
        try:
            result = update_skill()
            print(f"Updated to v{result['version']} in {result['path']}")
            print("Files:", ", ".join(result["files"]))
        except Exception as e:
            print(f"Update failed: {e}")
            sys.exit(1)
        return

    if args.command == "init":
        init_db()
        print(f"Database initialized: {DB_PATH}")
        return

    if not db_exists():
        print("Database not initialized. Run: pipeline.py init")
        sys.exit(1)

    migrate_db()

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
        leads = get_pipeline(
            stage_filter=args.stage,
            limit=args.limit,
            sentiment=getattr(args, "sentiment", None),
            auto_reply=auto_reply,
            lead_status=getattr(args, "lead_status", None),
            sort=getattr(args, "sort", "updated_at"),
            order=getattr(args, "order", "desc"),
        )
        print(json.dumps(leads, indent=2) if getattr(args, "json", False) else format_pipeline_table(leads))
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
        lead = find_lead(
            lead_id=args.id, email=args.email,
            linkedin=getattr(args, "linkedin", None), name=args.name,
        )
        if not lead:
            print(json.dumps({"error": "Lead not found"}))
            sys.exit(1)
        events = get_lead_events(lead["id"], args.limit)
        print(json.dumps({"lead": lead, "events": events}, indent=2)
              if args.json else format_event_timeline(lead, events))
    elif args.command == "copy-insights":
        insights = get_copy_insights(
            lead_status=args.lead_status,
            limit=args.limit,
        )
        print(json.dumps(insights, indent=2) if args.json else format_copy_insights(insights))
    else:
        if not db_exists():
            init_db()
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))


if __name__ == "__main__":
    main()