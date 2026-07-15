"""
Tag CRUD, event logging, pipeline views, campaign stats, and lead export.
Extracted from pipeline.py's "Tag CRUD" section.
"""

import csv
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from activity_sync import refresh_lead_activity_for_lead
from constants import (
    ATTRIBUTE_INSIGHT_FIELDS,
    COMPANY_DOMAIN_SQL,
    MAX_EVENT_BODY_STORAGE_CHARS,
    PIPELINE_STAGES,
    require_professional_domain_clause,
)
from db_conn import get_conn
from event_classification import normalize_campaign_event_type
from lead_sync import build_lead_sync_payload, _load_lead_sync_prefetch
from om_paths import resolve_project_path
from pipeline_lead_review import _never_contacted_sql
from pipeline_update import load_config
from pipeline_utils import normalize_email, normalize_tag, parse_tags_value
from platform_registry import (
    looks_like_html,
    normalize_event_body_for_storage,
    reply_event_sql_condition,
    strip_html_reply,
)
from read_queries import LATEST_STATUS_CTE
from relay_ingest import normalize_lead_status_display
from workspace_routing import (
    DEFAULT_ORG_ID,
    normalize_linkedin,
    resolve_lead_ids_by_identity,
    resolve_workspace_identity,
)

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
        conn.execute(
            """UPDATE workspace_leads SET updated_at = datetime('now')
               WHERE workspace_id = ? AND lead_id = ?""",
            (workspace_id, lead_id),
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
    if cur.rowcount:
        conn.execute(
            """UPDATE workspace_leads SET updated_at = datetime('now')
               WHERE workspace_id = ? AND lead_id = ?""",
            (workspace_id, lead_id),
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
    if added:
        conn.execute(
            """UPDATE workspace_leads SET updated_at = datetime('now')
               WHERE workspace_id = ? AND lead_id = ?""",
            (workspace_id, lead_id),
        )
    conn.commit()
    conn.close()
    return {"status": "set", "tags": added, "lead_id": lead_id}


def tag_list(
    workspace_id: str,
    lead_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """List tags for a workspace, optionally filtered by lead_id."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
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
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


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


def linkedin_status_summary(
    workspace_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Aggregate LinkedIn connection state by sender for a workspace."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
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
    finally:
        if own_conn:
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


def get_workspace_summary(workspace: str, *, tags_only: bool = False) -> dict:
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
        tags = tag_list(ws_id, conn=conn)
        if tags_only:
            li_summary = {"linkedin_senders": [], "linkedin_connected_leads": 0}
        else:
            li_summary = linkedin_status_summary(ws_id, conn=conn)
    finally:
        conn.close()
    cfg = load_config()
    return {
        "workspace": ws_row["slug"],
        "workspace_name": ws_row["name"],
        "lead_count": int(lead_count or 0),
        "last_pull": cfg.get("last_pull"),
        "tags": tags,
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
    if changed:
        for lead_id in lead_ids:
            conn.execute(
                """UPDATE workspace_leads SET updated_at = datetime('now')
                   WHERE workspace_id = ? AND lead_id = ?""",
                (workspace_id, lead_id),
            )
    conn.commit()
    conn.close()
    action = "removed" if remove else "added"
    return {"status": action, "changed": changed, "leads": len(lead_ids), "tags": tags}


def tag_bulk_by_identity(
    workspace_id: str,
    identity_type: str,
    values: list[str],
    tags: list[str],
    *,
    remove: bool = False,
) -> dict:
    """tag_bulk(), but resolving lead ids from raw identity values (e.g. a
    batch of linkedin_sales_nav_id strings from a fresh import) instead of
    requiring the caller to already know them. A fresh Sales Nav import's
    only stable identity is linkedin_sales_nav_id -- there was previously no
    way to bulk-tag those leads without first resolving ids yourself, which
    for a few thousand values meant either 1 query per lead or a compound
    SELECT that hits SQLite's SQLITE_LIMIT_COMPOUND_SELECT (500 terms)."""
    conn = get_conn()
    lead_ids, unresolved = resolve_lead_ids_by_identity(conn, DEFAULT_ORG_ID, identity_type, values)
    conn.close()
    result = tag_bulk(workspace_id, lead_ids, tags, remove=remove)
    result["identity_type"] = identity_type
    result["resolved"] = len(lead_ids)
    result["unresolved"] = unresolved
    return result



def load_json_array_from_cli(*, json_input: Optional[str] = None, file_path: Optional[str] = None) -> list:
    """Load a JSON array for companion subprocesses (--json or --file)."""
    if file_path and json_input:
        raise ValueError("Use --file or --json, not both")
    if file_path:
        path = resolve_project_path(file_path, kind="input")
        if not path.is_file():
            raise ValueError(f"File not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    elif json_input:
        data = json.loads(json_input)
    else:
        raise ValueError("Provide --file or --json")
    if not isinstance(data, list):
        raise ValueError("JSON must be an array")
    return data


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


def update_lead_stage(
    lead_id,
    stage,
    next_action=None,
    event_at=None,
    conn: Optional[sqlite3.Connection] = None,
    *,
    commit: bool = True,
):
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Valid: {PIPELINE_STAGES}")
    ts_expr = "?" if event_at else "datetime('now')"
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.execute(
        f"""UPDATE leads SET stage = ?, updated_at = {ts_expr},
           next_action = CASE WHEN ? IS NOT NULL THEN ? ELSE next_action END WHERE id = ?""",
        (stage, event_at, next_action, next_action, lead_id) if event_at
        else (stage, next_action, next_action, lead_id),
    )
    if commit and own_conn:
        conn.commit()
    if own_conn and conn is not None:
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
              event_at=None, sender=None, *,
              conn: Optional[sqlite3.Connection] = None,
              commit: bool = True,
              refresh_activity: bool = True):
    meta = dict(metadata or {})
    preview = _prepare_stored_event_body(meta, body_preview)
    campaign_name = campaign or meta.get("campaign")
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    campaign_id = None
    if campaign_name and str(campaign_name).strip():
        campaign_id = ensure_campaign(conn, str(campaign_name).strip(), lead_id)
    created = event_at or None
    relay_id = meta.get("relay_id")
    # ON CONFLICT against idx_events_relay_unique. relay_ingested is the primary
    # dedupe, but it's a *separate* ledger: if it is ever lost or reset while
    # events survives (a restore, a rebuild), the next pull silently re-ingests
    # everything and doubles the table -- which is exactly what happened, to the
    # tune of 17,363 duplicated relay events. The constraint makes that
    # impossible; landing on it just means we already have the row.
    if created:
        cur = conn.execute(
            """INSERT INTO events (
                   lead_id, event_type, direction, channel, subject, body_preview,
                   metadata_json, campaign_id, sender, relay_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(relay_id) WHERE relay_id IS NOT NULL DO NOTHING""",
            (lead_id, event_type, direction, channel, subject, preview,
             json.dumps(meta), campaign_id, sender, relay_id, created),
        )
        conn.execute(
            """UPDATE leads SET updated_at = ?, last_contact_at = ?
               WHERE id = ? AND (last_contact_at IS NULL OR last_contact_at < ?)""",
            (created, created, lead_id, created),
        )
    else:
        cur = conn.execute(
            """INSERT INTO events (
                   lead_id, event_type, direction, channel, subject, body_preview,
                   metadata_json, campaign_id, sender, relay_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(relay_id) WHERE relay_id IS NOT NULL DO NOTHING""",
            (lead_id, event_type, direction, channel, subject, preview,
             json.dumps(meta), campaign_id, sender, relay_id),
        )
        conn.execute(
            "UPDATE leads SET updated_at = datetime('now'), last_contact_at = datetime('now') WHERE id = ?",
            (lead_id,),
        )
    if cur.rowcount == 0 and relay_id is not None:
        # Already had this relay event; reuse its row rather than reporting a new one.
        row = conn.execute("SELECT id FROM events WHERE relay_id = ?", (relay_id,)).fetchone()
        event_id = row["id"] if row else None
    else:
        event_id = cur.lastrowid
    if sender:
        from pipeline_sender_accounts import (
            ensure_sender_account,
            link_sender_account_to_workspace,
            touch_sender_account_activity,
        )

        sa_channel = "linkedin" if channel == "linkedin" else "email"
        sender_account_id = ensure_sender_account(conn, sender, channel=sa_channel)
        if sender_account_id:
            touch_sender_account_activity(
                conn, sender_account_id, direction=direction, event_at=created,
            )
            for ws_row in conn.execute(
                "SELECT workspace_id FROM workspace_leads WHERE lead_id = ?", (lead_id,)
            ).fetchall():
                link_sender_account_to_workspace(conn, ws_row["workspace_id"], sender_account_id)
    if commit:
        conn.commit()
    if own_conn:
        conn.close()
    if refresh_activity:
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
        LATEST_STATUS_CTE
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
        query = LATEST_STATUS_CTE + f"""
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
        lead_ids = [int(lead["id"]) for lead in leads if lead.get("id") is not None]
        prefetch = _load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, lead_ids)
        company_ids = sorted({
            int(row["company_id"])
            for lid in lead_ids
            if (row := prefetch["leads"].get(lid)) and row["company_id"]
        })
        company_pers: dict[int, dict] = {}
        if company_ids:
            placeholders = ",".join("?" * len(company_ids))
            for row in conn.execute(
                f"""SELECT company_id, field_name, field_value, field_date, processed_at
                    FROM company_personalization
                    WHERE company_id IN ({placeholders})""",
                company_ids,
            ).fetchall():
                company_pers.setdefault(int(row["company_id"]), {})[row["field_name"]] = dict(row)

        enriched: list[dict] = []
        for lead in leads:
            row = dict(lead)
            lead_id = int(lead["id"])
            snap = build_lead_sync_payload(
                conn, DEFAULT_ORG_ID, lead_id, workspace_slug=ws_slug, prefetch=prefetch,
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
            merged: dict = {}
            lead_row = prefetch["leads"].get(lead_id)
            if lead_row and lead_row["company_id"]:
                cid = int(lead_row["company_id"])
                for fname, rec in (company_pers.get(cid) or {}).items():
                    merged[fname] = rec["field_value"]
                    if rec.get("field_date"):
                        merged[f"{fname}_date"] = rec["field_date"]
            for pers_row in prefetch["personalization"].get(lead_id) or []:
                fname = pers_row["field_name"]
                merged[fname] = pers_row["field_value"]
                field_date = pers_row["field_date"]
                if field_date:
                    merged[f"{fname}_date"] = field_date
                elif f"{fname}_date" in merged:
                    del merged[f"{fname}_date"]
            row["personalization"] = merged
            id_rows = prefetch["identities"].get(lead_id) or []
            # Only fall back to the identity table if the lead's own column is
            # empty. The identity value is case-folded (match key); the leads
            # column holds the canonical mixed case where we have it.
            if not (row.get("linkedin_sales_nav_id") or "").strip():
                for ident in id_rows:
                    if ident["identity_type"] == "linkedin_sales_nav_id":
                        row["linkedin_sales_nav_id"] = ident["identity_value_normalized"]
                        break
            row["linkedin_url"] = row.get("linkedin_url") or row.get("linkedin") or ""
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
    never_contacted: bool = False,
    no_email: bool = False,
    require_domain: bool = False,
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
               {COMPANY_DOMAIN_SQL},
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
    if never_contacted:
        query += f" AND {_never_contacted_sql('wl')}"
    if no_email:
        query += " AND (l.email IS NULL OR TRIM(l.email) = '')"
    if require_domain:
        domain_clause, domain_params = require_professional_domain_clause()
        query += f" {domain_clause}"
        params.extend(domain_params)
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
    never_contacted: bool = False,
    no_email: bool = False,
    require_domain: bool = False,
) -> dict:
    rows, truncated = query_leads_for_export(
        workspace=workspace,
        tag=tag,
        stage=stage,
        since=since,
        limit=limit,
        never_contacted=never_contacted,
        no_email=no_email,
        require_domain=require_domain,
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
    active = sum(v for k, v in stage_counts.items() if k not in ("won", "not_interested"))
    recent = conn.execute("SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')").fetchone()[0]
    conn.close()
    stats = {"total_leads": total, "total_events": events, "active_pipeline": active,
             "won": stage_counts.get("won", 0), "not_interested": stage_counts.get("not_interested", 0),
             "events_7d": recent, "stages": stage_counts,
             "reply_events": reply_events, "replied_leads": leads_with_replies}
    stats.update(get_campaign_stats())
    return stats

def get_lead_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None
