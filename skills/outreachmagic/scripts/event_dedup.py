"""Local-only cleanup for duplicate email_reply events sharing a message_id.

PlusVibe fires all_email_replies + all_positive_replies for one reply, both
carrying the same message_id. Before relay_dedupe_key() started keying
PlusVibe events by message_id (ahead of relay_id), both webhooks created
separate `events` rows, showing the same reply twice in the CRM summary note
timeline. This module finds and merges those historical duplicates; new
ingests are already deduped at the source.
"""

from __future__ import annotations

import json
import sqlite3


def find_duplicate_reply_events(conn: sqlite3.Connection) -> list[dict]:
    """Group email_reply events by (lead_id, message_id), returning only
    groups with more than one row. Each group picks a `keep_id` (the row
    with richer metadata -- carrying a sentiment/label signal -- tie-broken
    by earliest created_at/id) and lists the rest as `duplicate_ids`.
    """
    rows = conn.execute(
        """SELECT id, lead_id, subject, body_preview, metadata_json, created_at
           FROM events
           WHERE event_type = 'email_reply'
             AND json_extract(metadata_json, '$.message_id') IS NOT NULL
           ORDER BY lead_id, created_at ASC, id ASC"""
    ).fetchall()

    groups: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        d = dict(r)
        try:
            meta = json.loads(d.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        msg_id = meta.get("message_id")
        if not msg_id:
            continue
        d["_meta"] = meta
        groups.setdefault((d["lead_id"], msg_id), []).append(d)

    results = []
    for (lead_id, msg_id), group_rows in groups.items():
        if len(group_rows) < 2:
            continue

        def _has_signal(row: dict) -> bool:
            meta = row["_meta"]
            return bool(meta.get("lead_status_sentiment") or meta.get("lead_status_raw"))

        ranked = sorted(
            group_rows,
            key=lambda r: (0 if _has_signal(r) else 1, r["created_at"] or "", r["id"]),
        )
        keep = ranked[0]
        results.append({
            "lead_id": lead_id,
            "message_id": msg_id,
            "keep_id": keep["id"],
            "duplicate_ids": [r["id"] for r in ranked[1:]],
        })
    return results


def dedupe_reply_events(conn: sqlite3.Connection, *, commit: bool = False) -> dict:
    """Merge duplicate email_reply events found by find_duplicate_reply_events.

    For each group, any subject/body_preview/body present on a discarded row
    but missing from the kept row is copied over before the discarded row is
    deleted. Dry-run (commit=False, the default) only counts what would
    happen -- no writes.
    """
    groups = find_duplicate_reply_events(conn)
    events_merged = 0
    events_deleted = 0

    for group in groups:
        keep_id = group["keep_id"]
        duplicate_ids = group["duplicate_ids"]
        events_deleted += len(duplicate_ids)
        if not commit:
            events_merged += 1
            continue

        keep_row = conn.execute(
            "SELECT subject, body_preview, metadata_json FROM events WHERE id = ?",
            (keep_id,),
        ).fetchone()
        keep_meta = json.loads(keep_row["metadata_json"] or "{}")
        keep_subject = keep_row["subject"]
        keep_body_preview = keep_row["body_preview"]
        changed = False

        for loser_id in duplicate_ids:
            loser_row = conn.execute(
                "SELECT subject, body_preview, metadata_json FROM events WHERE id = ?",
                (loser_id,),
            ).fetchone()
            loser_meta = json.loads(loser_row["metadata_json"] or "{}")
            if not keep_subject and loser_row["subject"]:
                keep_subject = loser_row["subject"]
                changed = True
            if not keep_body_preview and loser_row["body_preview"]:
                keep_body_preview = loser_row["body_preview"]
                changed = True
            if not keep_meta.get("body") and loser_meta.get("body"):
                keep_meta["body"] = loser_meta["body"]
                changed = True
            conn.execute("DELETE FROM events WHERE id = ?", (loser_id,))

        if changed:
            conn.execute(
                "UPDATE events SET subject = ?, body_preview = ?, metadata_json = ? WHERE id = ?",
                (keep_subject, keep_body_preview, json.dumps(keep_meta), keep_id),
            )
        events_merged += 1

    if commit:
        conn.commit()

    return {
        "groups": len(groups),
        "events_merged": events_merged,
        "events_deleted": events_deleted,
        "committed": commit,
    }
