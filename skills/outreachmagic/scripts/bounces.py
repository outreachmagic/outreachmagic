"""Platform bounce extraction, deduplicated bounce_events storage, and email verification."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from db_conn import get_conn
from relay_extractors import extract_bounce_fields
from workspace_routing import DEFAULT_ORG_ID

BOUNCE_EVENT_TYPES = frozenset({
    "email_bounce",
    "email_bounced",
    "email.bounced",
    "bounced_email",
})

_SOFT_BOUNCE_HINTS = (
    "out of storage",
    "mailbox full",
    "over quota",
    "temporarily",
    "try again",
    "rate limit",
    "452 ",
    "421 ",
    "4.2.2",
    "4.4.1",
    "message expired",
)

_SMTP_CODE_RE = re.compile(r"\b([245]\d{2})(?:[-.](\d\.\d\.\d))?\b")


def is_bounce_event_type(event_type: str) -> bool:
    et = (event_type or "").strip().lower()
    if et in BOUNCE_EVENT_TYPES:
        return True
    return "bounce" in et


def normalize_bounce_event_type(event_type: str) -> str:
    return "email_bounce" if is_bounce_event_type(event_type) else (event_type or "unknown")


def _normalize_bounce_sender(sender: Optional[str]) -> str:
    return (sender or "").strip().lower() or "unknown"


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def _extract_smtp_code(message: str) -> Optional[str]:
    if not message:
        return None
    match = _SMTP_CODE_RE.search(message)
    if not match:
        return None
    return match.group(2) or match.group(1)


def _classify_bounce_type(raw_type: str, message: str) -> str:
    bt = (raw_type or "").strip().lower()
    if "hard" in bt:
        return "hard"
    if "soft" in bt or "temporary" in bt:
        return "soft"
    msg = (message or "").lower()
    if any(hint in msg for hint in _SOFT_BOUNCE_HINTS):
        return "soft"
    code = _extract_smtp_code(message or "")
    if code:
        if code.startswith("5"):
            return "hard"
        if code.startswith("4"):
            return "soft"
    if msg:
        return "hard"
    return "unknown"


def extract_bounce_payload(raw: dict, platform: str) -> dict:
    """Extract normalized bounce diagnostics from a relay webhook payload."""
    fields = extract_bounce_fields(platform, raw or {})
    message = (fields.get("message") or "").strip()
    bounce_type = _classify_bounce_type(fields.get("bounce_type", ""), message)
    return {
        "bounce_type": bounce_type,
        "bounce_message": message,
        "smtp_code": _extract_smtp_code(message),
        "recipient_mx": (fields.get("recipient_mx") or "").strip() or None,
        "sender_mx": (fields.get("sender_mx") or "").strip() or None,
    }


def _bounce_event_id(lead_id: int, sender_email: str) -> str:
    key = f"{lead_id}:{_normalize_bounce_sender(sender_email)}"
    return "be_" + hashlib.sha256(key.encode()).hexdigest()[:24]


def build_bounce_event_metadata(payload: dict, envelope_event_type: str) -> dict:
    """Status metadata stored on bounce timeline events."""
    meta = {
        "lead_status_raw": "email_bounced",
        "lead_status_display": "email bounced",
        "lead_status_sentiment": "invalid",
        "bounce_type": payload.get("bounce_type") or "unknown",
    }
    if payload.get("bounce_message"):
        meta["bounce_message"] = payload["bounce_message"]
    if payload.get("smtp_code"):
        meta["smtp_code"] = payload["smtp_code"]
    if payload.get("recipient_mx"):
        meta["recipient_mx"] = payload["recipient_mx"]
    if payload.get("sender_mx"):
        meta["sender_mx"] = payload["sender_mx"]
    if envelope_event_type:
        meta["webhook_event"] = envelope_event_type
    return meta


def record_bounce_event(
    conn: sqlite3.Connection,
    *,
    lead_id: int,
    event_id: Optional[int],
    platform: str,
    sender_email: str,
    lead_email: str,
    payload: dict,
    campaign_id: Optional[int] = None,
    campaign_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
    event_at: Optional[str] = None,
    relay_id: Optional[str] = None,
) -> dict:
    """Persist deduplicated bounce analytics (one row per lead + sender)."""
    sender_norm = _normalize_bounce_sender(sender_email)
    now_ts = event_at or datetime.now(timezone.utc).isoformat()
    bounce_id = _bounce_event_id(lead_id, sender_norm)
    message = (payload.get("bounce_message") or "").strip()
    existing = conn.execute(
        """SELECT id, bounce_message, occurrence_count FROM bounce_events
           WHERE lead_id = ? AND sender_email = ?""",
        (lead_id, sender_norm),
    ).fetchone()
    if existing:
        keep_message = existing["bounce_message"] or ""
        if message and (not keep_message or len(message) > len(keep_message)):
            keep_message = message
        conn.execute(
            """UPDATE bounce_events SET
                   latest_event_id = COALESCE(?, latest_event_id),
                   bounce_message = ?,
                   bounce_type = ?,
                   smtp_code = COALESCE(?, smtp_code),
                   recipient_mx = COALESCE(?, recipient_mx),
                   sender_mx = COALESCE(?, sender_mx),
                   relay_id = COALESCE(?, relay_id),
                   occurrence_count = occurrence_count + 1,
                   last_seen_at = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (
                event_id,
                keep_message or None,
                payload.get("bounce_type") or "unknown",
                payload.get("smtp_code"),
                payload.get("recipient_mx"),
                payload.get("sender_mx"),
                relay_id,
                now_ts,
                existing["id"],
            ),
        )
        return {
            "status": "duplicate",
            "id": existing["id"],
            "occurrence_count": int(existing["occurrence_count"]) + 1,
        }

    conn.execute(
        """INSERT INTO bounce_events (
               id, org_id, lead_id, first_event_id, latest_event_id, platform,
               sender_email, lead_email, bounce_type, bounce_message, smtp_code,
               recipient_mx, sender_mx, campaign_id, campaign_name, workspace_id,
               relay_id, occurrence_count, first_seen_at, last_seen_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            bounce_id,
            DEFAULT_ORG_ID,
            lead_id,
            event_id,
            event_id,
            platform,
            sender_norm,
            (lead_email or "").strip().lower(),
            payload.get("bounce_type") or "unknown",
            message or None,
            payload.get("smtp_code"),
            payload.get("recipient_mx"),
            payload.get("sender_mx"),
            campaign_id,
            campaign_name,
            workspace_id,
            relay_id,
            now_ts,
            now_ts,
        ),
    )
    return {"status": "recorded", "id": bounce_id, "occurrence_count": 1}


def backfill_bounce_events_from_events(conn: sqlite3.Connection) -> int:
    """Backfill bounce_events from historical timeline rows (best-effort)."""
    rows = conn.execute(
        """SELECT e.id, e.lead_id, e.sender, e.body_preview, e.metadata_json, e.created_at,
                  e.campaign_id, l.email AS lead_email
           FROM events e
           INNER JOIN leads l ON l.id = e.lead_id
           WHERE e.event_type IN ('email_bounce', 'email_bounced', 'bounced_email', 'email.bounced')
              OR lower(json_extract(e.metadata_json, '$.webhook_event')) IN (
                  'bounced_email', 'email_bounced', 'email.bounced', 'email_bounce'
              )
           ORDER BY e.created_at ASC, e.id ASC"""
    ).fetchall()
    recorded = 0
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        platform = (meta.get("platform") or "unknown").lower()
        payload = {
            "bounce_type": meta.get("bounce_type") or "unknown",
            "bounce_message": meta.get("bounce_message") or row["body_preview"] or "",
            "smtp_code": meta.get("smtp_code"),
            "recipient_mx": meta.get("recipient_mx"),
            "sender_mx": meta.get("sender_mx"),
        }
        if not payload["bounce_message"]:
            continue
        sender = row["sender"] or meta.get("sender") or "unknown"
        result = record_bounce_event(
            conn,
            lead_id=row["lead_id"],
            event_id=row["id"],
            platform=platform,
            sender_email=sender,
            lead_email=row["lead_email"] or "",
            payload=payload,
            campaign_id=row["campaign_id"],
            campaign_name=meta.get("campaign"),
            workspace_id=None,
            event_at=row["created_at"],
            relay_id=meta.get("relay_id"),
        )
        if result.get("status") == "recorded":
            recorded += 1
    return recorded


def list_bounce_events(
    *,
    platform: Optional[str] = None,
    bounce_type: Optional[str] = None,
    sender: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    conn = get_conn()
    query = """
        SELECT b.*, l.name AS lead_name, l.company
        FROM bounce_events b
        INNER JOIN leads l ON l.id = b.lead_id
        WHERE 1=1
    """
    params: list = []
    if platform:
        query += " AND lower(b.platform) = lower(?)"
        params.append(platform)
    if bounce_type:
        query += " AND lower(b.bounce_type) = lower(?)"
        params.append(bounce_type)
    if sender:
        query += " AND lower(b.sender_email) = lower(?)"
        params.append(sender.strip())
    if since:
        since_date = since.strip()
        if since_date.lower() == "today":
            since_date = datetime.now().strftime("%Y-%m-%d")
        query += " AND b.last_seen_at >= ?"
        params.append(since_date)
    query += " ORDER BY b.last_seen_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bounce_stats(*, since: Optional[str] = None) -> dict:
    conn = get_conn()
    since_filter = ""
    params: list = []
    if since:
        since_date = since.strip()
        if since_date.lower() == "today":
            since_date = datetime.now().strftime("%Y-%m-%d")
        since_filter = " WHERE last_seen_at >= ?"
        params.append(since_date)

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM bounce_events{since_filter}", params
    ).fetchone()["c"]
    by_platform = conn.execute(
        f"""SELECT platform, COUNT(*) AS c FROM bounce_events{since_filter}
            GROUP BY platform ORDER BY c DESC""",
        params,
    ).fetchall()
    by_type = conn.execute(
        f"""SELECT bounce_type, COUNT(*) AS c FROM bounce_events{since_filter}
            GROUP BY bounce_type ORDER BY c DESC""",
        params,
    ).fetchall()
    by_mx = conn.execute(
        f"""SELECT COALESCE(recipient_mx, 'unknown') AS recipient_mx, COUNT(*) AS c
            FROM bounce_events{since_filter}
            GROUP BY COALESCE(recipient_mx, 'unknown') ORDER BY c DESC LIMIT 20""",
        params,
    ).fetchall()
    dupes = conn.execute(
        f"""SELECT COALESCE(SUM(occurrence_count - 1), 0) AS suppressed
            FROM bounce_events{since_filter}""",
        params,
    ).fetchone()["suppressed"]
    conn.close()
    return {
        "total_unique_bounces": total,
        "suppressed_duplicate_webhooks": int(dupes or 0),
        "by_platform": [dict(r) for r in by_platform],
        "by_bounce_type": [dict(r) for r in by_type],
        "by_recipient_mx": [dict(r) for r in by_mx],
    }


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


def record_platform_bounce(
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
        email = _normalize_email(email)
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
