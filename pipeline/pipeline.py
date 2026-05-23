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
  pipeline.py log-event --lead-id 1 ...     # Log outreach event
  pipeline.py history --id 1                # Show lead's event timeline
  pipeline.py history --email j@acme.com    # Look up by email
  pipeline.py history --name "Jane"         # Look up by name (partial)
  pipeline.py stats                         # Quick stats
"""

import sqlite3
import json
import os
import sys
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relay_extractors import build_display_name, extract_relay_fields, name_from_email


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
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    company         TEXT,
    title           TEXT,
    industry        TEXT,
    headcount       TEXT,
    email           TEXT,
    email_domain    TEXT,
    linkedin_url    TEXT,
    channel         TEXT NOT NULL DEFAULT 'email',
    stage           TEXT NOT NULL DEFAULT 'prospecting',
    notes           TEXT,
    tags            TEXT DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_contact_at TEXT,
    next_action     TEXT,
    next_action_at  TEXT
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
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
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
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
    # Migrate existing databases that predate newer columns
    for col, col_type in [("industry", "TEXT"), ("headcount", "TEXT"), ("email_domain", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute(
        """UPDATE leads SET email_domain = lower(substr(email, instr(email, '@') + 1))
           WHERE email LIKE '%@%' AND (email_domain IS NULL OR email_domain = '')"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_email_domain ON leads(email_domain)")
    conn.commit()
    conn.close()
    return True


def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


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


def sync_company_by_domain(domain: str, company=None, industry=None, headcount=None):
    """Fill empty company/industry/headcount on all leads sharing a business email domain."""
    if not domain or domain in SHARED_EMAIL_DOMAINS:
        return
    fields = [(c, v) for c, v in [("company", company), ("industry", industry), ("headcount", headcount)] if v]
    if not fields:
        return
    conn = get_conn()
    sets, params = [], []
    for col, val in fields:
        sets.append(f"{col} = CASE WHEN {col} IS NULL OR trim({col}) = '' THEN ? ELSE {col} END")
        params.append(val)
    sets.append("updated_at = datetime('now')")
    params.append(domain)
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE email_domain = ?", params)
    conn.commit()
    conn.close()

def db_exists():
    return DB_PATH.exists()

def add_lead(name, company=None, title=None, industry=None, headcount=None,
             email=None, linkedin_url=None,
             channel="email", stage="prospecting", notes=None, tags=None):
    conn = get_conn()
    domain = email_domain(email)
    if email:
        existing = conn.execute("SELECT id FROM leads WHERE email = ?", (email,)).fetchone()
        if existing:
            if domain:
                conn.execute(
                    "UPDATE leads SET email_domain = ? WHERE id = ? AND (email_domain IS NULL OR email_domain = '')",
                    (domain, existing["id"]),
                )
                conn.commit()
            conn.close()
            return {"status": "exists", "id": existing["id"], "email": email}
    cursor = conn.execute(
        """INSERT INTO leads (name, company, title, industry, headcount, email, email_domain, linkedin_url, channel, stage, notes, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, company, title, industry, headcount, email, domain, linkedin_url, channel, stage, notes, json.dumps(tags or [])),
    )
    lead_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"status": "created", "id": lead_id, "name": name}

def enrich_lead(lead_id, name=None, title=None, industry=None, company=None):
    """Fill empty lead profile fields from relay extraction (won't overwrite non-empty)."""
    conn = get_conn()
    row = conn.execute("SELECT name, email, title, industry, company FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        conn.close()
        return
    updates, params = [], []
    email = row["email"] or ""
    if name:
        current = (row["name"] or "").strip()
        derived = name_from_email(email) if email else ""
        if not current or current == derived:
            updates.append("name = ?")
            params.append(name)
    for col, val in [("title", title), ("industry", industry), ("company", company)]:
        if val and not (row[col] or "").strip():
            updates.append(f"{col} = ?")
            params.append(val)
    if updates:
        updates.append("updated_at = datetime('now')")
        conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", (*params, lead_id))
        conn.commit()
    conn.close()


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

def log_event(lead_id, event_type, direction="outbound", channel="email",
              subject=None, body_preview=None, metadata=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, subject, body_preview, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, event_type, direction, channel, subject, (body_preview or "")[:200],
         json.dumps(metadata or {})),
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

def get_pipeline(stage_filter=None, limit=50):
    conn = get_conn()
    query = """
        SELECT l.*,
               (SELECT event_type FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) as last_event,
               (SELECT created_at FROM events WHERE lead_id = l.id ORDER BY created_at DESC LIMIT 1) as last_event_at,
               (SELECT COUNT(*) FROM events WHERE lead_id = l.id) as event_count
        FROM leads l
    """
    params = []
    if stage_filter:
        query += " WHERE l.stage = ?"
        params.append(stage_filter)
    query += " ORDER BY l.updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stage_counts():
    conn = get_conn()
    rows = conn.execute("SELECT stage, COUNT(*) as count FROM leads GROUP BY stage ORDER BY count DESC").fetchall()
    conn.close()
    return {r["stage"]: r["count"] for r in rows}

def get_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    stage_counts = get_stage_counts()
    active = sum(v for k, v in stage_counts.items() if k not in ("won", "lost"))
    recent = conn.execute("SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')").fetchone()[0]
    conn.close()
    return {"total_leads": total, "total_events": events, "active_pipeline": active,
            "won": stage_counts.get("won", 0), "lost": stage_counts.get("lost", 0),
            "events_7d": recent, "stages": stage_counts}

def get_lead_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ──────────────────────────────────────────────────────────────────────
# Relay Integration (wbhk.org)
# ──────────────────────────────────────────────────────────────────────

def pull_events(token: str, since: Optional[str] = None) -> dict:
    """Pull buffered events from the relay."""
    url = f"{RELAY_URL}/pull/{token}"
    if since:
        url += f"?since={since}"

    req = urllib.request.Request(url, headers={"User-Agent": "OutreachMagic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": True, "status": e.code, "message": body}
    except urllib.error.URLError as e:
        return {"error": True, "message": str(e.reason)}

def ack_events(token: str, max_id: int):
    """Acknowledge pulled events so relay can clean them up."""
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

def ingest_relay_event(event: dict):
    """Take a relay event and write it to the local SQLite database."""
    lead_email = event.get("lead")
    event_type = event.get("event_type", "unknown")
    platform = event.get("platform", "unknown")
    sender = event.get("sender", "")
    received_at = event.get("received_at", "")
    raw = event.get("raw") or {}

    extracted = extract_relay_fields(platform, raw)
    lead_fields = extracted["lead"]
    event_fields = extracted["event"]

    display_name = build_display_name(lead_fields, lead_email)
    if not display_name and lead_email and "@" in lead_email:
        display_name = name_from_email(lead_email)
    elif not display_name:
        display_name = lead_email or f"Unknown ({platform})"

    # Determine channel from platform
    channel_map = {"smartlead": "email", "instantly": "email", "emailbison": "email",
                   "heyreach": "linkedin", "prosp": "linkedin",
                   "pipl": "email", "plusvibe": "email", "clay": "email"}
    channel = channel_map.get(platform, "email")

    # Auto-add lead if not exists; enrich profile from raw on every event
    if lead_email and "@" in lead_email:
        result = add_lead(
            name=display_name,
            company=lead_fields.get("company_name"),
            title=lead_fields.get("job_title"),
            industry=lead_fields.get("industry"),
            email=lead_email,
            channel=channel,
            stage="prospecting",
            notes=f"Auto-imported from {platform} via relay",
        )
        lead_id = result["id"]
    else:
        result = add_lead(
            name=display_name,
            company=lead_fields.get("company_name"),
            title=lead_fields.get("job_title"),
            industry=lead_fields.get("industry"),
            email=lead_email or f"unknown-{platform}@relay.local",
            channel=channel,
            stage="prospecting",
            notes=f"Auto-imported from {platform} via relay",
        )
        lead_id = result["id"]

    enrich_lead(
        lead_id,
        name=display_name if lead_fields.get("first_name") else None,
        title=lead_fields.get("job_title"),
        industry=lead_fields.get("industry"),
        company=lead_fields.get("company_name"),
    )

    if lead_email and "@" in lead_email:
        ensure_lead_domain(lead_id, lead_email)
        domain = email_domain(lead_email)
        if domain:
            sync_company_by_domain(
                domain,
                company=lead_fields.get("company_name"),
                industry=lead_fields.get("industry"),
                headcount=lead_fields.get("headcount"),
            )

    # Map relay event types to local event types
    event_type_map = {
        "email_sent": "email_sent", "email_open": "email_open",
        "email_reply": "email_reply", "email_bounce": "email_bounce",
        "email_click": "email_click", "email_unsubscribe": "email_unsubscribe",
        "linkedin_connect": "linkedin_connect",
        "linkedin_connection_accepted": "linkedin_connection_accepted",
        "linkedin_message": "linkedin_message",
        "linkedin_reply": "linkedin_message",
    }

    local_type = event_type_map.get(event_type, event_type)
    direction = "inbound" if event_type in ("email_reply", "email_open", "email_click",
                                             "linkedin_connection_accepted", "linkedin_reply") else "outbound"

    subject = event_fields.get("subject") or f"{platform}: {event_type}"
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

    log_event(
        lead_id=lead_id,
        event_type=local_type,
        direction=direction,
        channel=channel,
        subject=subject,
        body_preview=body_preview,
        metadata=metadata,
    )

    # Auto-update stage based on event type
    if event_type in ("email_reply", "linkedin_reply", "linkedin_message"):
        update_lead_stage(lead_id, "replied")
    elif event_type in ("email_sent", "linkedin_connect", "linkedin_message_sent"):
        update_lead_stage(lead_id, "contacted")

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

    # Auto-pull + show on first connect for simple onboarding
    if count > 0:
        print("Pulling events for the first time...")
        imported = 0
        for event in result.get("events", []):
            try:
                ingest_relay_event(event)
                imported += 1
            except Exception:
                pass
        print(f"Imported {imported} events.\n")
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))
    else:
        print("No events yet. Run 'pipeline.py pull' after your platforms start sending webhooks.")

    print()
    print("Tip: Add a cron job to auto-pull every 15 minutes:")
    print("  hermes cron create --name 'outreach-pull' --schedule '*/15 * * * *' \\")
    print("    --command 'cd ~/.hermes/skills/sales/outreach-magic && python3 pipeline.py pull --cron'")


# ──────────────────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────────────────

def format_pipeline_table(leads):
    if not leads:
        return "No leads in pipeline. Time to do some outreach!"
    lines = [f"{'Lead':<28} {'Company':<20} {'Stage':<14} {'Last':<12} {'Next Action'}", "-" * 95]
    for lead in leads:
        name = (lead["name"] or "")[:26]
        company = (lead["company"] or "")[:18]
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
        lines.append(f"{name:<28} {company:<20} {emoji} {stage:<12} {last:<12} {next_action}")
    return "\n".join(lines)

def format_stats(stats):
    return (
        f"Pipeline: {stats['active_pipeline']} active | {stats['won']} won | "
        f"{stats['lost']} lost | {stats['total_leads']} total leads\n"
        f"Events: {stats['total_events']} total | {stats['events_7d']} in last 7 days\n"
        f"Breakdown: " + ", ".join(f"{s}={c}" for s, c in stats.get("stages", {}).items())
    )

def format_event_timeline(lead, events):
    """Format a lead's event history as a timeline."""
    emoji = STAGE_EMOJI.get(lead.get("stage", ""), "")
    lines = [
        f"Lead:    {lead['name']} ({emoji} {lead.get('stage', '?')})",
        f"Title:   {lead.get('title') or '—'}",
        f"Email:   {lead.get('email') or '—'}",
        f"Company: {lead.get('company') or '—'}",
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
        if len(details) > 45:
            details = details[:42] + "..."
        lines.append(f"{i:<4} {when:<20} {evt:<32} {details}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Outreach Magic — Pipeline visibility for Hermes")
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("init", help="Initialize the database")

    show_p = sub.add_parser("show", help="Show pipeline")
    show_p.add_argument("--stage"); show_p.add_argument("--limit", type=int, default=50)
    show_p.add_argument("--json", action="store_true")

    stats_p = sub.add_parser("stats", help="Pipeline statistics")
    stats_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add-lead", help="Add a lead")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--company"); add_p.add_argument("--title")
    add_p.add_argument("--industry"); add_p.add_argument("--headcount")
    add_p.add_argument("--email"); add_p.add_argument("--linkedin")
    add_p.add_argument("--channel", default="email"); add_p.add_argument("--stage", default="prospecting")
    add_p.add_argument("--notes"); add_p.add_argument("--tags")

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

    webhook_p = sub.add_parser("webhook-url", help="Show webhook URLs for your platforms")

    hist_p = sub.add_parser("history", help="Show event history for a lead")
    hist_p.add_argument("--id", type=int, help="Lead ID")
    hist_p.add_argument("--email", help="Find lead by email")
    hist_p.add_argument("--name", help="Find lead by name (partial match)")
    hist_p.add_argument("--limit", type=int, default=50, help="Max events to show")
    hist_p.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print(f"Database initialized: {DB_PATH}")
        return

    if not db_exists():
        print("Database not initialized. Run: pipeline.py init")
        sys.exit(1)

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

        since = get_last_pull()
        result = pull_events(tok, since)

        if result.get("error"):
            if not args.cron:
                print(f"Pull failed: {result.get('message', 'unknown')}")
            sys.exit(0)

        count = result.get("count", 0)
        max_id = result.get("max_id", 0)

        if count == 0:
            if not args.cron:
                print("No new events.")
            sys.exit(0)

        imported = 0
        for event in result.get("events", []):
            try:
                ingest_relay_event(event)
                imported += 1
            except Exception as e:
                if not args.cron:
                    print(f"  Skipped event: {e}")

        if max_id:
            ack_events(tok, max_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        set_last_pull(now_iso)

        print(f"Pulled {imported} events from relay.")
        print(f"Run 'pipeline.py show' to see your updated pipeline.")
        return

    if args.command == "show":
        leads = get_pipeline(args.stage, args.limit)
        print(json.dumps(leads, indent=2) if getattr(args, "json", False) else format_pipeline_table(leads))
    elif args.command == "stats":
        stats = get_stats()
        print(json.dumps(stats, indent=2) if getattr(args, "json", False) else format_stats(stats))
    elif args.command == "add-lead":
        tags = json.loads(args.tags) if args.tags else None
        print(json.dumps(add_lead(name=args.name, company=args.company, title=args.title,
                                   industry=args.industry, headcount=args.headcount,
                                   email=args.email, linkedin_url=args.linkedin,
                                   channel=args.channel, stage=args.stage, notes=args.notes, tags=tags)))
    elif args.command == "update-stage":
        update_lead_stage(args.id, args.stage, args.next_action)
        print(json.dumps({"status": "updated", "id": args.id, "stage": args.stage}))
    elif args.command == "log-event":
        log_event(lead_id=args.lead_id, event_type=args.event_type, direction=args.direction,
                  channel=args.channel, subject=args.subject, body_preview=args.body)
        print(json.dumps({"status": "logged", "lead_id": args.lead_id}))
    elif args.command == "history":
        conn = get_conn()
        if args.id:
            lead = conn.execute("SELECT * FROM leads WHERE id = ?", (args.id,)).fetchone()
        elif args.email:
            lead = conn.execute("SELECT * FROM leads WHERE email = ?", (args.email,)).fetchone()
        elif args.name:
            lead = conn.execute(
                "SELECT * FROM leads WHERE name LIKE ? LIMIT 1", (f"%{args.name}%",)
            ).fetchone()
        else:
            conn.close()
            print(json.dumps({"error": "Provide --id, --email, or --name"}))
            sys.exit(1)
        conn.close()
        if not lead:
            print(json.dumps({"error": "Lead not found"}))
            sys.exit(1)
        events = get_lead_events(lead["id"], args.limit)
        print(json.dumps({"lead": dict(lead), "events": events}, indent=2)
              if args.json else format_event_timeline(dict(lead), events))
    else:
        if not db_exists():
            init_db()
        leads = get_pipeline()
        print(format_pipeline_table(leads))
        print()
        print(format_stats(get_stats()))


if __name__ == "__main__":
    main()