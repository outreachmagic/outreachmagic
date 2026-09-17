"""repair-lead-status: rebuild workspace_leads.current_status_* from the event log.

The events are the source of truth; the current_status_* columns are a
projection of them. Before set_lead_status guarded writes by event time, a
stale workspace snapshot (including an echo of our own push) could overwrite a
newer status. This replays each workspace lead's status events in event-time
order and rewrites the projection wherever it disagrees.

Per workspace lead, over its whole status history:
  sentiment       latest sentiment-bearing event
  since           start of the current contiguous sentiment run
  label           latest event that actually carries a label
  status_at       latest status event
  first_positive  first positive event -- only filled when NULL (set-once)
Fields no event speaks to are left as they are.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from workspace_routing import NOT_A_STATUS, normalize_status_label

_STATUS_EVENTS = """
    json_extract(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
    OR json_extract(e.metadata_json, '$.lead_status_raw') IS NOT NULL
    OR json_extract(e.metadata_json, '$.status_label') IS NOT NULL
"""


def parse_since(value: Optional[str]) -> Optional[str]:
    """'90d' -> SQLite modifier '-90 days'; 'all'/None -> no window."""
    if not value or value == "all":
        return None
    m = re.fullmatch(r"(\d+)d", value.strip())
    if not m:
        raise ValueError(f"--since must look like 90d or 'all', got {value!r}")
    return f"-{int(m.group(1))} days"


def event_label(meta: dict, event_type: str) -> Optional[str]:
    """The label an event set, or None when it only set a stage/sentiment."""
    if meta.get("status_label"):
        return normalize_status_label(meta["status_label"])
    if "stage" in meta:
        return None  # manual change without a label
    raw = normalize_status_label(meta.get("lead_status_raw"))
    if not raw:
        return None
    if (event_type == "lead_status_updated"
            and not meta.get("platform") and not meta.get("webhook_event")):
        # Legacy manual change: raw was the stage, and a label given with it
        # only reached lead_status_display.
        display = normalize_status_label(meta.get("lead_status_display"))
        return display if display and display != raw else None
    return raw


def project(events: list[sqlite3.Row]) -> dict[str, Any]:
    """Fold one workspace lead's status events (oldest first) into its status."""
    out: dict[str, Any] = {"label": None, "sentiment": None, "since": None,
                           "status_at": None, "first_positive": None}
    for ev in events:
        meta = json.loads(ev["metadata_json"] or "{}")
        if normalize_status_label(meta.get("lead_status_raw")) in NOT_A_STATUS:
            continue
        at = ev["event_at"]
        label = event_label(meta, ev["event_type"])
        sentiment = (meta.get("lead_status_sentiment") or "").strip().lower() or None
        if not label and not sentiment:
            continue
        out["status_at"] = at
        if label:
            out["label"] = label
        if sentiment:
            if sentiment != out["sentiment"]:
                out["sentiment"], out["since"] = sentiment, at
            if sentiment == "positive" and not out["first_positive"]:
                out["first_positive"] = at
    return out


def repair_lead_status(
    conn: sqlite3.Connection,
    *,
    workspace_id: Optional[str] = None,
    since: Optional[str] = "90d",
    dry_run: bool = True,
) -> dict[str, Any]:
    window = parse_since(since)
    where, params = [], []
    if window:
        where.append("w.event_at >= datetime('now', ?)")
        params.append(window)
    if workspace_id:
        where.append("w.workspace_id = ?")
        params.append(workspace_id)
    scope = f"""
        SELECT DISTINCT w.workspace_id, w.lead_id
        FROM workspace_lead_events w JOIN events e ON e.id = w.event_id
        WHERE ({_STATUS_EVENTS}){''.join(' AND ' + c for c in where)}
    """
    rows = conn.execute(
        f"""WITH scope AS ({scope})
            SELECT wle.workspace_id, wle.lead_id, wle.event_at, e.event_type, e.metadata_json
            FROM scope s
            JOIN workspace_lead_events wle
              ON wle.workspace_id = s.workspace_id AND wle.lead_id = s.lead_id
            JOIN events e ON e.id = wle.event_id
            WHERE {_STATUS_EVENTS}
            ORDER BY wle.workspace_id, wle.lead_id, wle.event_at, e.id""",
        params,
    ).fetchall()
    by_lead: dict[tuple[str, int], list] = {}
    for r in rows:
        by_lead.setdefault((r["workspace_id"], r["lead_id"]), []).append(r)

    changed: list[dict[str, Any]] = []
    for (ws_id, lead_id), events in by_lead.items():
        want = project(events)
        wl = conn.execute(
            """SELECT wl.id, wl.current_status_label, wl.current_status_sentiment,
                      wl.current_sentiment_since, wl.current_status_at,
                      wl.first_positive_at, l.email, w.slug
               FROM workspace_leads wl
               JOIN leads l ON l.id = wl.lead_id
               JOIN workspaces w ON w.id = wl.workspace_id
               WHERE wl.workspace_id = ? AND wl.lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
        if wl is None:
            continue
        sets: dict[str, Any] = {}
        for col, key in (("current_status_label", "label"),
                         ("current_status_sentiment", "sentiment"),
                         ("current_sentiment_since", "since"),
                         ("current_status_at", "status_at")):
            if want[key] and want[key] != wl[col]:
                sets[col] = want[key]
        if want["first_positive"] and not wl["first_positive_at"]:
            sets["first_positive_at"] = want["first_positive"]
        if not sets:
            continue
        changed.append({
            "lead_id": lead_id, "email": wl["email"], "workspace": wl["slug"],
            "from": {k: wl[k] for k in sets}, "to": sets,
        })
        if not dry_run:
            conn.execute(
                f"UPDATE workspace_leads SET {', '.join(f'{k} = ?' for k in sets)}, "
                "updated_at = datetime('now') WHERE id = ?",
                (*sets.values(), wl["id"]),
            )
    if not dry_run:
        conn.commit()

    counts: dict[str, int] = {}
    for c in changed:
        for col in c["to"]:
            counts[col] = counts.get(col, 0) + 1
    sentiment_flips = sum(
        1 for c in changed
        if "current_status_sentiment" in c["to"]
    )
    return {
        "status": "dry_run" if dry_run else "applied",
        "since": since or "all",
        "leads_checked": len(by_lead),
        "leads_changed": len(changed),
        "sentiment_changed": sentiment_flips,
        "columns_changed": counts,
        "changed": changed,
    }
