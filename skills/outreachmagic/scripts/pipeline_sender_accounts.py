#!/usr/bin/env python3
"""Sender account (PlusVibe deliverability) import, sync, and stats.

Sender accounts are the mailboxes we send *from* -- warmup/health/deliverability
metadata imported from PlusVibe's CSV export, plus reply/bounce rates computed
from our own local `events`/`bounce_events` data. Org-wide, mirroring the
leads/workspace_leads split (a mailbox can be shared across workspaces).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime
from typing import Optional

from campaign_stats import reply_event_sql_condition
from db_conn import get_conn
from pipeline_utils import normalize_email
from workspace_routing import DEFAULT_ORG_ID

# PlusVibe export columns we intentionally ignore -- SMTP/IMAP connection
# config, not deliverability data, and we don't use those.
_IGNORED_CSV_COLUMNS = frozenset({
    "username", "smtp_username", "smtp_host", "smtp_port",
    "imap_host", "imap_port", "warmup_custom_words",
})

_SENT_EVENT_TYPES = ("email_sent", "email_sent_auto")

# CSV column -> (sender_accounts column, coercion)
_INT_FIELDS = frozenset({
    "daily_limit", "warmup_max_daily_limit",
    "overall_hscore", "google_hscore", "microsoft_hscore", "other_hscore",
})
_REAL_FIELDS = frozenset({"ooo_rr", "ooo_rr_14", "ooo_rr_30", "ooo_rr_90", "bounce_r", "miss_warmup_r"})

_CSV_TO_COLUMN = {
    "_id": "external_id",
    "first_name": "first_name",
    "last_name": "last_name",
    "provider": "provider",
    "daily_limit": "daily_limit",
    "warmup_status": "warmup_status",
    "email": "email",
    "created_at": "source_created_at",
    "status": "status",
    "SPF": "spf_status",
    "DKIM": "dkim_status",
    "DMARC": "dmarc_status",
    "warmup_enabled_date": "warmup_enabled_date",
    "warmup_max_daily_limit": "warmup_max_daily_limit",
    "overall_hscore": "overall_health_score",
    "google_hscore": "google_health_score",
    "microsoft_hscore": "microsoft_health_score",
    "other_hscore": "other_health_score",
    "ooo_rr": "ooo_rr",
    "ooo_rr_14": "ooo_rr_14",
    "ooo_rr_30": "ooo_rr_30",
    "ooo_rr_90": "ooo_rr_90",
    "bounce_r": "bounce_rate",
    "miss_warmup_r": "miss_warmup_rate",
}

_JS_DATE_TRAILING_PAREN_RE = re.compile(r"\s*\(.*\)\s*$")


def _parse_plusvibe_date(raw: Optional[str]) -> Optional[str]:
    """PlusVibe stamps JS Date.toString() output, e.g.
    'Wed Mar 04 2026 03:17:51 GMT+0000 (Coordinated Universal Time)'."""
    if not raw or not str(raw).strip():
        return None
    cleaned = _JS_DATE_TRAILING_PAREN_RE.sub("", str(raw).strip())
    try:
        return datetime.strptime(cleaned, "%a %b %d %Y %H:%M:%S GMT%z").isoformat()
    except ValueError:
        return raw


def parse_sender_accounts_csv(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            email = normalize_email(raw_row.get("email"))
            if not email:
                continue
            row: dict = {"email": email}
            for csv_col, value in raw_row.items():
                if csv_col in _IGNORED_CSV_COLUMNS or csv_col in ("email",):
                    continue
                if csv_col == "tags":
                    tags = [t.strip() for t in (value or "").split(";") if t.strip()]
                    row["tags_json"] = json.dumps(tags)
                    continue
                column = _CSV_TO_COLUMN.get(csv_col)
                if not column:
                    continue
                if value is None or value == "":
                    row[column] = None
                    continue
                if csv_col in _INT_FIELDS:
                    try:
                        row[column] = int(float(value))
                    except ValueError:
                        row[column] = None
                elif csv_col in _REAL_FIELDS:
                    try:
                        row[column] = float(value)
                    except ValueError:
                        row[column] = None
                elif csv_col in ("created_at", "warmup_enabled_date"):
                    row[column] = _parse_plusvibe_date(value)
                else:
                    row[column] = value
            rows.append(row)
    return rows


def infer_workspace_slugs_from_tags(tags: list[str], known_slugs: Optional[set] = None) -> list[str]:
    """Match PlusVibe tags against known workspace slugs.

    Convention observed in exports: '{slug}_all' / '{slug}_segment_*' tags
    identify which workspace a mailbox belongs to. Unmatched tags are
    ignored, not an error.

    known_slugs lets callers holding an open write connection pass an
    already-fetched slug set instead of triggering a second get_conn() call
    per row (which deadlocks against the caller's open transaction).
    """
    if known_slugs is None:
        from pipeline_workspace import list_workspaces

        known_slugs = {w["slug"] for w in list_workspaces()}
    matched = set()
    for tag in tags:
        for slug in known_slugs:
            if tag == slug or tag.startswith(f"{slug}_"):
                matched.add(slug)
    return sorted(matched)


def upsert_sender_account(
    conn: sqlite3.Connection, row: dict, org_id: str = DEFAULT_ORG_ID, channel: str = "email",
) -> int:
    email = row["email"]
    columns = [c for c in _CSV_TO_COLUMN.values() if c in row] + (["tags_json"] if "tags_json" in row else [])
    columns = sorted(set(columns))
    existing = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(
            f"UPDATE sender_accounts SET {set_clause}, channel = ?, updated_at = datetime('now') WHERE id = ?",
            [row.get(c) for c in columns] + [channel, existing["id"]],
        )
        return int(existing["id"])
    insert_cols = ["org_id", "email", "channel"] + columns
    placeholders = ", ".join("?" for _ in insert_cols)
    cur = conn.execute(
        f"INSERT INTO sender_accounts ({', '.join(insert_cols)}) VALUES ({placeholders})",
        [org_id, email, channel] + [row.get(c) for c in columns],
    )
    return int(cur.lastrowid)


def ensure_sender_account(
    conn: sqlite3.Connection, identifier: str, channel: str = "email", org_id: str = DEFAULT_ORG_ID,
) -> Optional[int]:
    """Bootstrap a bare sender_accounts row from an event's sender, if it doesn't exist yet.

    Cheap INSERT OR IGNORE -- never overwrites richer data from a CSV import
    (warmup/health scores etc), only fills in accounts we've never seen
    before (this is the primary creation path for Prosp/LinkedIn accounts,
    which have no CSV export).
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO sender_accounts (org_id, email, channel) VALUES (?, ?, ?)",
        (org_id, identifier, channel),
    )
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, identifier)
    ).fetchone()
    return int(row["id"]) if row else None


def link_sender_account_to_workspace(conn: sqlite3.Connection, workspace_id: str, sender_account_id: int) -> None:
    _link_workspace(conn, workspace_id, sender_account_id)


def _link_workspace(conn: sqlite3.Connection, workspace_id: str, sender_account_id: int) -> None:
    link_id = f"wsa_{workspace_id}_{sender_account_id}"
    conn.execute(
        """INSERT OR IGNORE INTO workspace_sender_accounts (id, workspace_id, sender_account_id)
           VALUES (?, ?, ?)""",
        (link_id, workspace_id, sender_account_id),
    )


def import_sender_accounts(file_path: str, workspace: Optional[str] = None, org_id: str = DEFAULT_ORG_ID) -> dict:
    from pipeline_workspace import list_workspaces

    slug_to_id = {w["slug"]: w["id"] for w in list_workspaces()}
    explicit_ws_id = None
    if workspace:
        explicit_ws_id = slug_to_id.get(workspace)
        if not explicit_ws_id:
            return {"status": "error", "error": f"unknown workspace: {workspace}"}

    rows = parse_sender_accounts_csv(file_path)
    conn = get_conn()
    created = 0
    updated = 0
    workspace_links = 0
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?",
                (org_id, row["email"]),
            ).fetchone()
            sender_account_id = upsert_sender_account(conn, row, org_id=org_id, channel="email")
            if existing:
                updated += 1
            else:
                created += 1

            if explicit_ws_id:
                _link_workspace(conn, explicit_ws_id, sender_account_id)
                workspace_links += 1
            else:
                tags = json.loads(row.get("tags_json") or "[]")
                for slug in infer_workspace_slugs_from_tags(tags, known_slugs=set(slug_to_id)):
                    ws_id = slug_to_id.get(slug)
                    if ws_id:
                        _link_workspace(conn, ws_id, sender_account_id)
                        workspace_links += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "total": len(rows),
        "created": created,
        "updated": updated,
        "workspace_links": workspace_links,
    }


def sender_account_entity_key(conn: sqlite3.Connection, sender_account_id: int) -> Optional[str]:
    row = conn.execute("SELECT email FROM sender_accounts WHERE id = ?", (sender_account_id,)).fetchone()
    if not row or not row["email"]:
        return None
    return f"sender_account:{row['email'].strip().lower()}"


def resolve_sender_account_from_entity_key(
    conn: sqlite3.Connection, entity_key: str, org_id: str = DEFAULT_ORG_ID
) -> Optional[int]:
    if not entity_key.startswith("sender_account:"):
        return None
    email = entity_key.split(":", 1)[1]
    if not email:
        return None
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO sender_accounts (org_id, email) VALUES (?, ?)", (org_id, email)
    )
    return int(cur.lastrowid)


_SYNC_PAYLOAD_COLUMNS = sorted(set(_CSV_TO_COLUMN.values()) | {"tags_json"})


def build_sender_account_sync_payload(conn: sqlite3.Connection, sender_account_id: int) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(_SYNC_PAYLOAD_COLUMNS)} FROM sender_accounts WHERE id = ?",
        (sender_account_id,),
    ).fetchone()
    if not row:
        return {}
    payload: dict = {}
    for col in _SYNC_PAYLOAD_COLUMNS:
        val = row[col]
        if val is not None and val != "":
            payload[col] = val
    return payload


def apply_agent_sender_account_sync_payload(sender_account_id: int, payload: dict, *, conn=None) -> None:
    own_conn = conn is None
    conn = conn or get_conn()
    columns = [c for c in _SYNC_PAYLOAD_COLUMNS if c in payload]
    if columns:
        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(
            f"UPDATE sender_accounts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            [payload.get(c) for c in columns] + [sender_account_id],
        )
    if own_conn:
        conn.commit()
        conn.close()


def compute_sender_stats(
    conn: sqlite3.Connection, email: str, *, workspace: Optional[str] = None, since: Optional[str] = None
) -> dict:
    """Reply/bounce rates for a sender, computed from local events/bounce_events."""
    params: list = [email]
    since_clause = ""
    if since:
        since_clause = " AND e.created_at >= ?"
        params.append(since)

    sent_placeholders = ", ".join("?" for _ in _SENT_EVENT_TYPES)
    sent_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND e.event_type IN ({sent_placeholders}){since_clause}""",
        params + list(_SENT_EVENT_TYPES),
    ).fetchone()[0]

    reply_params = [email] + ([since] if since else [])
    reply_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND {reply_event_sql_condition()}{since_clause}""",
        reply_params,
    ).fetchone()[0]

    bounce_params: list = [email]
    bounce_since_clause = ""
    if since:
        bounce_since_clause = " AND first_seen_at >= ?"
        bounce_params.append(since)
    bounce_count = conn.execute(
        f"SELECT COUNT(*) FROM bounce_events WHERE sender_email = ?{bounce_since_clause}",
        bounce_params,
    ).fetchone()[0]

    return {
        "sent_count": sent_count,
        "reply_count": reply_count,
        "reply_rate": round(reply_count / sent_count, 4) if sent_count else None,
        "bounce_count": bounce_count,
        "bounce_rate": round(bounce_count / sent_count, 4) if sent_count else None,
    }


def sender_insights(conn: sqlite3.Connection, workspace: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
    if workspace:
        rows = conn.execute(
            """SELECT sa.* FROM sender_accounts sa
               INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
               INNER JOIN workspaces w ON w.id = wsa.workspace_id
               WHERE w.slug = ?
               ORDER BY sa.email""",
            (workspace,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sender_accounts ORDER BY email").fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json", None) or "[]")
        item.update(compute_sender_stats(conn, row["email"], since=since))
        results.append(item)
    return results
