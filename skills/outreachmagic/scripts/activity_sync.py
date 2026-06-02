"""Materialized lead activity summaries for cross-platform sync."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Callable, Optional

from db_conn import get_conn
from event_classification import classify_event_for_activity

WORKSPACE_LAST_CONTACTED_EXPR = "COALESCE(last_contacted_at, last_activity_at)"


@dataclass
class ActivitySummary:
    last_contacted_at: Optional[str] = None
    email_sent_count: int = 0
    linkedin_sent_count: int = 0
    total_replies_count: int = 0

    @property
    def total_contacted_count(self) -> int:
        return self.email_sent_count + self.linkedin_sent_count

    def to_sync_dict(self) -> dict:
        out = {
            "email_sent_count": self.email_sent_count,
            "linkedin_sent_count": self.linkedin_sent_count,
            "total_replies_count": self.total_replies_count,
            "total_contacted_count": self.total_contacted_count,
        }
        if self.last_contacted_at:
            out["last_contacted_at"] = self.last_contacted_at
        return out

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> ActivitySummary:
        if not raw:
            return cls()
        kwargs = {}
        for field in fields(cls):
            if field.name in raw and raw[field.name] is not None:
                if field.name == "last_contacted_at":
                    kwargs[field.name] = str(raw[field.name]).strip() or None
                else:
                    kwargs[field.name] = _activity_int(raw[field.name])
        return cls(**kwargs)

    @classmethod
    def merge(cls, existing: Optional[ActivitySummary], incoming: Optional[ActivitySummary]) -> ActivitySummary:
        base = existing or cls()
        inc = incoming or cls()
        email = max(base.email_sent_count, inc.email_sent_count)
        linkedin = max(base.linkedin_sent_count, inc.linkedin_sent_count)
        replies = max(base.total_replies_count, inc.total_replies_count)
        last = _activity_max_timestamp(base.last_contacted_at, inc.last_contacted_at)
        return cls(
            last_contacted_at=last,
            email_sent_count=email,
            linkedin_sent_count=linkedin,
            total_replies_count=replies,
        )

    def is_empty(self) -> bool:
        return (
            not self.last_contacted_at
            and self.email_sent_count == 0
            and self.linkedin_sent_count == 0
            and self.total_replies_count == 0
        )


def activity_debug_enabled() -> bool:
    return os.environ.get("OUTREACHMAGIC_DEBUG", "").strip().lower() in ("1", "true", "yes", "activity")


def activity_debug(message: str) -> None:
    if activity_debug_enabled():
        print(f"[activity] {message}", flush=True)


def _activity_int(val) -> int:
    try:
        return max(0, int(val or 0))
    except (TypeError, ValueError):
        return 0


def _parse_activity_ts(raw: Optional[str]) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _activity_max_timestamp(*values: Optional[str]) -> Optional[str]:
    best_raw: Optional[str] = None
    best_dt: Optional[datetime] = None
    for raw in values:
        text = (raw or "").strip()
        if not text:
            continue
        parsed = _parse_activity_ts(text)
        if parsed is None:
            if best_raw is None or text > best_raw:
                best_raw = text
            continue
        if best_dt is None or parsed > best_dt:
            best_dt = parsed
            best_raw = text
    return best_raw


def merge_activity_summary(existing: Optional[dict], incoming: Optional[dict]) -> dict:
    """Merge activity snapshots: max counts, latest last_contacted_at."""
    merged = ActivitySummary.merge(
        ActivitySummary.from_dict(existing),
        ActivitySummary.from_dict(incoming),
    )
    if merged.is_empty():
        return {}
    return merged.to_sync_dict()


def _decode_event_metadata(raw_meta) -> dict:
    if not raw_meta:
        return {}
    try:
        return json.loads(raw_meta)
    except (json.JSONDecodeError, TypeError):
        return {}


def _activity_from_event_metadata(metadata_json) -> ActivitySummary:
    meta = _decode_event_metadata(metadata_json)
    return ActivitySummary(
        email_sent_count=_activity_int(meta.get("emails_sent_count") or meta.get("email_sent_count")),
        linkedin_sent_count=_activity_int(meta.get("linkedin_sent_count")),
        total_replies_count=_activity_int(meta.get("total_replies_count")),
    )


def compute_lead_activity_from_events(conn: sqlite3.Connection, lead_id: int) -> dict:
    """Derive activity summary from local events (including historical metadata)."""
    rows = conn.execute(
        """SELECT event_type, direction, channel, metadata_json, created_at
           FROM events WHERE lead_id = ? ORDER BY created_at ASC, id ASC""",
        (lead_id,),
    ).fetchall()
    summary = ActivitySummary()
    last_outbound: Optional[str] = None
    for row in rows:
        meta_summary = _activity_from_event_metadata(row["metadata_json"])
        summary = ActivitySummary.merge(summary, meta_summary)
        flags = classify_event_for_activity(row["event_type"], row["direction"], row["channel"])
        if flags.email_sent:
            summary.email_sent_count += 1
        if flags.linkedin_sent:
            summary.linkedin_sent_count += 1
        if flags.reply:
            summary.total_replies_count += 1
        if (flags.email_sent or flags.linkedin_sent) and row["created_at"]:
            last_outbound = _activity_max_timestamp(last_outbound, row["created_at"])
    if last_outbound:
        summary.last_contacted_at = last_outbound
    if summary.is_empty():
        return {}
    return summary.to_sync_dict()


def _read_workspace_activity_row(conn: sqlite3.Connection, workspace_id: str, lead_id: int) -> dict:
    row = conn.execute(
        f"""SELECT {WORKSPACE_LAST_CONTACTED_EXPR} AS last_contacted_at,
                   email_sent_count, linkedin_sent_count, total_replies_count
            FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
        (workspace_id, lead_id),
    ).fetchone()
    if not row:
        return {}
    summary = ActivitySummary(
        last_contacted_at=row["last_contacted_at"],
        email_sent_count=_activity_int(row["email_sent_count"]),
        linkedin_sent_count=_activity_int(row["linkedin_sent_count"]),
        total_replies_count=_activity_int(row["total_replies_count"]),
    )
    if summary.is_empty():
        return {}
    return summary.to_sync_dict()


def _summary_from_workspace_row(wl_row) -> dict:
    if wl_row is None:
        return {}
    last = wl_row["last_contacted_at"] if "last_contacted_at" in wl_row.keys() else None
    if not last and "last_activity_at" in wl_row.keys():
        last = wl_row["last_activity_at"]
    summary = ActivitySummary(
        last_contacted_at=last,
        email_sent_count=_activity_int(wl_row["email_sent_count"]),
        linkedin_sent_count=_activity_int(wl_row["linkedin_sent_count"]),
        total_replies_count=_activity_int(wl_row["total_replies_count"]),
    )
    if summary.is_empty():
        return {}
    return summary.to_sync_dict()


def build_activity_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    workspace_id: Optional[str] = None,
    wl_row=None,
) -> dict:
    """Build cross-platform activity block for lead sync."""
    stored: dict = {}
    if wl_row is not None:
        stored = _summary_from_workspace_row(wl_row)
    elif workspace_id:
        stored = _read_workspace_activity_row(conn, workspace_id, lead_id)
    activity = merge_activity_summary(stored, {})
    return activity


def _write_workspace_activity(
    conn: sqlite3.Connection,
    workspace_id: str,
    lead_id: int,
    activity: dict,
) -> None:
    summary = ActivitySummary.from_dict(activity)
    last = summary.last_contacted_at
    if last:
        conn.execute(
            """UPDATE workspace_leads SET
                   email_sent_count = ?,
                   linkedin_sent_count = ?,
                   total_replies_count = ?,
                   last_contacted_at = ?,
                   last_activity_at = ?,
                   updated_at = datetime('now')
               WHERE workspace_id = ? AND lead_id = ?""",
            (
                summary.email_sent_count,
                summary.linkedin_sent_count,
                summary.total_replies_count,
                last,
                last,
                workspace_id,
                lead_id,
            ),
        )
        conn.execute(
            """UPDATE leads SET last_contact_at = ?, updated_at = datetime('now')
               WHERE id = ? AND (last_contact_at IS NULL OR last_contact_at < ?)""",
            (last, lead_id, last),
        )
    else:
        conn.execute(
            """UPDATE workspace_leads SET
                   email_sent_count = ?,
                   linkedin_sent_count = ?,
                   total_replies_count = ?,
                   updated_at = datetime('now')
               WHERE workspace_id = ? AND lead_id = ?""",
            (
                summary.email_sent_count,
                summary.linkedin_sent_count,
                summary.total_replies_count,
                workspace_id,
                lead_id,
            ),
        )


def apply_activity_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_id: str,
    activity: dict,
    *,
    merge: bool = True,
) -> dict:
    """Persist activity summary on workspace_leads (+ leads.last_contact_at)."""
    if not activity:
        return {}
    incoming = ActivitySummary.from_dict(activity)
    if merge:
        existing = ActivitySummary.from_dict(_read_workspace_activity_row(conn, workspace_id, lead_id))
        incoming = ActivitySummary.merge(existing, incoming)
        activity_debug(
            f"lead={lead_id} workspace={workspace_id} merged={incoming.to_sync_dict()}"
        )
    else:
        activity_debug(
            f"lead={lead_id} workspace={workspace_id} set={incoming.to_sync_dict()}"
        )
    merged = incoming.to_sync_dict()
    _write_workspace_activity(conn, workspace_id, lead_id, merged)
    return merged


def refresh_lead_activity_from_events(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_id: str,
) -> dict:
    """Recompute activity from events and merge with stored workspace counts."""
    computed = compute_lead_activity_from_events(conn, lead_id)
    stored = _read_workspace_activity_row(conn, workspace_id, lead_id)
    merged = merge_activity_summary(stored, computed)
    if merged:
        _write_workspace_activity(conn, workspace_id, lead_id, merged)
    return merged


def refresh_lead_activity_for_lead(
    lead_id: int,
    *,
    mark_pending_fn: Optional[Callable[[int], None]] = None,
) -> None:
    conn = get_conn()
    rows = conn.execute(
        "SELECT workspace_id FROM workspace_leads WHERE lead_id = ?", (lead_id,),
    ).fetchall()
    for row in rows:
        refresh_lead_activity_from_events(conn, lead_id, row["workspace_id"])
    conn.commit()
    conn.close()
    if mark_pending_fn is None:
        from pipeline import _mark_workspace_lead_cloud_pending

        def _default_pending(lid: int, wid: str) -> None:
            _mark_workspace_lead_cloud_pending(lid, wid)

        mark_pending_fn = _default_pending
    if mark_pending_fn:
        for row in rows:
            mark_pending_fn(lead_id, row["workspace_id"])


def set_lead_activity_summary(
    lead_id: int,
    workspace_id: str,
    *,
    last_contacted_at: Optional[str] = None,
    email_sent_count: Optional[int] = None,
    linkedin_sent_count: Optional[int] = None,
    total_replies_count: Optional[int] = None,
    merge: bool = True,
    mark_cloud_pending: bool = True,
    mark_pending_fn: Optional[Callable[[int], None]] = None,
) -> dict:
    """Set or merge materialized activity summary (legacy import, manual backfill)."""
    incoming: dict = {}
    if last_contacted_at:
        incoming["last_contacted_at"] = last_contacted_at.strip()
    if email_sent_count is not None:
        incoming["email_sent_count"] = _activity_int(email_sent_count)
    if linkedin_sent_count is not None:
        incoming["linkedin_sent_count"] = _activity_int(linkedin_sent_count)
    if total_replies_count is not None:
        incoming["total_replies_count"] = _activity_int(total_replies_count)
    if not incoming:
        return {}
    conn = get_conn()
    result = apply_activity_sync_payload(
        conn, lead_id, workspace_id, incoming, merge=merge,
    )
    conn.commit()
    conn.close()
    if mark_cloud_pending:
        if mark_pending_fn is None:
            from pipeline import _mark_workspace_lead_cloud_pending

            mark_pending_fn = lambda lid, wid=workspace_id: _mark_workspace_lead_cloud_pending(lid, wid)
        mark_pending_fn(lead_id, workspace_id)
    return result


def attach_activity_to_sync_payload(
    payload: dict,
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    workspace_id: Optional[str] = None,
    wl_row=None,
) -> None:
    activity = build_activity_sync_payload(
        conn, lead_id, workspace_id=workspace_id, wl_row=wl_row,
    )
    if activity:
        payload["activity"] = activity
