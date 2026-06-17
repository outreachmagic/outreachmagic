"""Campaign analytics — read-only queries and multi-sheet payload builders.

Consumed by both the `sheets campaign-stats` export (→ Google Sheets via backend)
and the `query campaign-stats` command (→ JSON for the AI agent).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from platform_registry import reply_event_sql_condition
from read_queries import normalize_since


# ── Helpers ──────────────────────────────────────────────────────────


def strip_workspace_prefix(campaign_name: str, workspace: str) -> str:
    """Strip '{workspace} | ' prefix from campaign names for display."""
    prefix = f"{workspace} | "
    if campaign_name.lower().startswith(prefix.lower()):
        return campaign_name[len(prefix):]
    return campaign_name


def detect_status(sends_in_window: int, sends_outside_window: int) -> str:
    """Classify campaign activity status."""
    if sends_in_window > 0:
        return "active"
    if sends_outside_window > 0:
        return "paused"
    return "exhausted"


def pct(part: float | int, total: float | int) -> str:
    """Format a percentage string, or '—' when total is 0."""
    if not total:
        return "—"
    return f"{round(part / total * 100, 1)}%"


def _fmt_date(iso_str: Optional[str]) -> str:
    """Format ISO datetime to short display like 'Jun 13'."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d")
    except (ValueError, TypeError):
        return iso_str or "—"


def _since_expr(since: Optional[str]) -> tuple[str, list]:
    """Build SQL expression and params for time-window filtering.

    Returns (where_clause_fragment, params_list).
    The fragment starts with ' AND ' or is empty.
    """
    expr = normalize_since(since)
    if not expr:
        return "", []
    if expr.startswith("datetime("):
        return f" AND e.created_at >= {expr}", []
    return " AND e.created_at >= ?", [expr]


def _since_expr_t(since: Optional[str], alias: str = "e") -> tuple[str, list]:
    """Like _since_expr but with a configurable table alias."""
    raw, params = _since_expr(since)
    if raw and alias != "e":
        raw = raw.replace("e.created_at", f"{alias}.created_at")
    return raw, params


# ── Campaign name stream collecting (leads → campaign join) ──────────


def _load_campaigns(conn: sqlite3.Connection, workspace: str) -> list[dict[str, Any]]:
    """All campaigns for a workspace, sorted by name."""
    rows = conn.execute(
        """SELECT id, name FROM campaigns WHERE name LIKE ? ORDER BY name""",
        [f"{workspace} |%"],
    ).fetchall()
    return [dict(r) for r in rows]


# ── Sheet 1: Campaign Overview ──────────────────────────────────────


def _query_overview(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str],
) -> list[dict[str, Any]]:
    """Wide query: one row per campaign with all event-type counts."""
    since_sql, since_params = _since_expr(since)
    reply_where = reply_event_sql_condition()

    sql = f"""
    WITH campaign_ids AS (
        SELECT id, name FROM campaigns WHERE name LIKE ?
    ),
    window_sends AS (
        SELECT campaign_id, COUNT(*) AS sent_count
        FROM events WHERE LOWER(event_type) IN ('email_sent', 'email_sent_auto')
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    all_sends AS (
        SELECT campaign_id, COUNT(*) AS total_sent
        FROM events WHERE LOWER(event_type) IN ('email_sent', 'email_sent_auto')
          AND campaign_id IN (SELECT id FROM campaign_ids)
        GROUP BY campaign_id
    ),
    bounces AS (
        SELECT campaign_id, COUNT(*) AS bounce_count
        FROM events WHERE LOWER(event_type) IN ('email_bounce', 'bounced_email', 'email_bounced')
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    replies AS (
        SELECT campaign_id, COUNT(*) AS reply_count
        FROM events e WHERE ({reply_where})
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql}
        GROUP BY campaign_id
    ),
    ooo AS (
        SELECT campaign_id, COUNT(*) AS ooo_count
        FROM events e WHERE ({reply_where})
          AND CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql}
        GROUP BY campaign_id
    ),
    li_connects AS (
        SELECT campaign_id, COUNT(*) AS li_connect_count
        FROM events WHERE event_type = 'linkedin_connect'
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    li_accepts AS (
        SELECT campaign_id, COUNT(*) AS li_accept_count
        FROM events WHERE event_type = 'linkedin_accept'
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    li_messages AS (
        SELECT campaign_id, COUNT(*) AS li_message_count
        FROM events WHERE event_type = 'linkedin_message'
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    li_replies AS (
        SELECT campaign_id, COUNT(*) AS li_reply_count
        FROM events WHERE event_type = 'linkedin_reply'
          AND campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    ),
    last_activity AS (
        SELECT campaign_id, MAX(created_at) AS last_at
        FROM events
        WHERE campaign_id IN (SELECT id FROM campaign_ids)
          {since_sql.replace('e.created_at', 'created_at')}
        GROUP BY campaign_id
    )
    SELECT
      c.id AS campaign_id,
      c.name,
      COALESCE(s.sent_count, 0) AS sent,
      COALESCE(b.bounce_count, 0) AS bounced,
      COALESCE(r.reply_count, 0) AS total_replies,
      COALESCE(o.ooo_count, 0) AS ooo,
      COALESCE(lc.li_connect_count, 0) AS li_connects,
      COALESCE(la.li_accept_count, 0) AS li_accepts,
      COALESCE(lm.li_message_count, 0) AS li_messages,
      COALESCE(lr.li_reply_count, 0) AS li_replies,
      laa.last_at AS last_activity,
      COALESCE(ast.total_sent, 0) AS all_time_sent
    FROM campaign_ids c
    LEFT JOIN window_sends s ON c.id = s.campaign_id
    LEFT JOIN all_sends ast ON c.id = ast.campaign_id
    LEFT JOIN bounces b ON c.id = b.campaign_id
    LEFT JOIN replies r ON c.id = r.campaign_id
    LEFT JOIN ooo o ON c.id = o.campaign_id
    LEFT JOIN li_connects lc ON c.id = lc.campaign_id
    LEFT JOIN li_accepts la ON c.id = la.campaign_id
    LEFT JOIN li_messages lm ON c.id = lm.campaign_id
    LEFT JOIN li_replies lr ON c.id = lr.campaign_id
    LEFT JOIN last_activity laa ON c.id = laa.campaign_id
    ORDER BY
      CASE WHEN s.sent_count > 0 THEN 0 ELSE 1 END,
      COALESCE(r.reply_count, 0) DESC
    """
    params: list[Any] = [f"{workspace} |%"] + since_params
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _query_sentiment_per_campaign(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str],
) -> list[dict[str, Any]]:
    """Latest sentiment per lead grouped by campaign and sentiment value."""
    since_sql, since_params = _since_expr_t(since)

    sql = f"""
    WITH ranked_status AS (
      SELECT
        e.lead_id,
        LOWER(JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment')) AS sentiment,
        e.campaign_id,
        ROW_NUMBER() OVER (
          PARTITION BY e.lead_id
          ORDER BY e.created_at DESC, e.id DESC
        ) AS rn
      FROM events e
      WHERE JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
        {since_sql}
    )
    SELECT
      c.name AS campaign,
      rs.sentiment,
      COUNT(DISTINCT rs.lead_id) AS lead_count
    FROM ranked_status rs
    JOIN campaigns c ON rs.campaign_id = c.id
    WHERE rs.rn = 1
      AND c.name LIKE ?
    GROUP BY c.name, rs.sentiment
    ORDER BY campaign, lead_count DESC
    """
    params: list[Any] = [*since_params, f"{workspace} |%"]
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_overview_sheet(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str],
) -> dict[str, Any]:
    """Build the Campaign Overview sheet data."""
    overview_rows = _query_overview(conn, workspace, since)
    sentiment_rows = _query_sentiment_per_campaign(conn, workspace, since)

    # Aggregate sentiment by campaign
    interested_sentiments = {"positive", "interested"}
    not_interested_sentiments = {"negative", "not_interested"}
    sentiment_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in sentiment_rows:
        cam = strip_workspace_prefix(row["campaign"], workspace)
        s = (row["sentiment"] or "").lower()
        if s in interested_sentiments:
            sentiment_counts[cam]["interested"] += row["lead_count"]
        elif s in not_interested_sentiments:
            sentiment_counts[cam]["not_interested"] += row["lead_count"]

    headers = [
        "Campaign", "Status", "Sent", "Delivered", "Bounced", "Bounce %",
        "Total Replies", "OOO", "Manual", "Reply %",
        "LI Connects", "LI Accepts", "Accept %",
        "LI Messages", "LI Replies",
        "Interested", "Not Interested", "Sentiment Rate", "Last Activity",
    ]

    rows = []
    for c in overview_rows:
        camp_name = strip_workspace_prefix(c["name"], workspace)
        sent = c["sent"]
        bounced = c["bounced"]
        total_replies = c["total_replies"]
        ooo = c["ooo"]
        manual = total_replies - ooo
        delivered = sent - bounced
        li_connects = c["li_connects"]
        li_accepts = c["li_accepts"]
        li_messages = c["li_messages"]
        li_replies = c["li_replies"]
        interested = sentiment_counts[camp_name]["interested"]
        not_interested = sentiment_counts[camp_name]["not_interested"]
        sentiment_denom = interested + not_interested

        rows.append([
            camp_name,
            detect_status(
                c["sent"],
                c["all_time_sent"] - c["sent"],
            ),
            sent or "—",
            delivered,
            bounced or "—",
            pct(bounced, sent),
            total_replies,
            ooo,
            manual,
            pct(total_replies, delivered),
            li_connects or "—",
            li_accepts or "—",
            pct(li_accepts, li_connects),
            li_messages or "—",
            li_replies or "—",
            interested or "—",
            not_interested or "—",
            pct(interested, sentiment_denom) if sentiment_denom else "—",
            _fmt_date(c["last_activity"]),
        ])

    return {
        "title": "Campaign Overview",
        "headers": headers,
        "rows": rows,
    }


# ── Sheet 2: Campaign Funnels ──────────────────────────────────────


def _query_funnel_data(
    conn: sqlite3.Connection,
    workspace: str,
    campaign_name: str,
    since: Optional[str],
) -> dict[str, Any]:
    """Per-campaign funnel counts."""
    since_sql, since_params = _since_expr(since)
    reply_where = reply_event_sql_condition()
    full_name = f"{workspace} | {campaign_name}"

    params: list[Any] = [full_name, *since_params]

    sql = f"""
    WITH campaign AS (
        SELECT id FROM campaigns WHERE name = ? LIMIT 1
    ),
    unique_sent AS (
        SELECT COUNT(DISTINCT lead_id) AS val FROM events
        WHERE campaign_id = (SELECT id FROM campaign)
          AND LOWER(event_type) IN ('email_sent', 'email_sent_auto')
          {since_sql.replace('e.created_at', 'created_at')}
    ),
    delivered AS (
        SELECT COUNT(*) AS val FROM events
        WHERE campaign_id = (SELECT id FROM campaign)
          AND LOWER(event_type) IN ('email_sent', 'email_sent_auto')
          {since_sql.replace('e.created_at', 'created_at')}
    )
    SELECT
        (SELECT val FROM unique_sent) AS unique_sent,
        (SELECT val FROM delivered) AS delivered,
        (SELECT COALESCE(COUNT(*), 0) FROM events
         WHERE campaign_id = (SELECT id FROM campaign)
           AND LOWER(event_type) IN ('email_bounce', 'bounced_email', 'email_bounced')
           {since_sql.replace('e.created_at', 'created_at')}) AS bounced,
        (SELECT COALESCE(COUNT(*), 0) FROM events e
         WHERE ({reply_where})
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql}) AS total_replies,
        (SELECT COALESCE(COUNT(*), 0) FROM events e
         WHERE ({reply_where})
           AND CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql}) AS ooo,
        (SELECT COALESCE(COUNT(*), 0) FROM events
         WHERE event_type = 'linkedin_connect'
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql.replace('e.created_at', 'created_at')}) AS li_connects,
        (SELECT COALESCE(COUNT(*), 0) FROM events
         WHERE event_type = 'linkedin_accept'
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql.replace('e.created_at', 'created_at')}) AS li_accepts,
        (SELECT COALESCE(COUNT(*), 0) FROM events
         WHERE event_type = 'linkedin_message'
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql.replace('e.created_at', 'created_at')}) AS li_messages,
        (SELECT COALESCE(COUNT(*), 0) FROM events
         WHERE event_type = 'linkedin_reply'
           AND campaign_id = (SELECT id FROM campaign)
           {since_sql.replace('e.created_at', 'created_at')}) AS li_replies
    """
    row = conn.execute(sql, params).fetchone()
    if not row:
        return {"unique_sent": 0, "delivered": 0, "bounced": 0,
                "total_replies": 0, "ooo": 0, "li_connects": 0,
                "li_accepts": 0, "li_messages": 0, "li_replies": 0}
    return dict(row)


def build_funnels_sheet(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str],
) -> dict[str, Any]:
    """Build the Campaign Funnels sheet — one section per active campaign."""
    overview_rows = _query_overview(conn, workspace, since)
    active_campaigns = [r for r in overview_rows if r["sent"] > 0]

    # Also get sentiment for interested/not interested per campaign
    sentiment_rows = _query_sentiment_per_campaign(conn, workspace, since)
    interested_sentiments = {"positive", "interested"}
    not_interested_sentiments = {"negative", "not_interested"}
    sentiment_by_campaign: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in sentiment_rows:
        cam = strip_workspace_prefix(row["campaign"], workspace)
        s = (row["sentiment"] or "").lower()
        if s in interested_sentiments:
            sentiment_by_campaign[cam]["interested"] += row["lead_count"]
        elif s in not_interested_sentiments:
            sentiment_by_campaign[cam]["not_interested"] += row["lead_count"]

    rows: list[list[Any]] = []
    for c in active_campaigns:
        camp_name = strip_workspace_prefix(c["name"], workspace)
        funnel = _query_funnel_data(conn, workspace, camp_name, since)

        us = funnel["unique_sent"]
        delivered = funnel["delivered"]
        bounced = funnel["bounced"]
        total_replies = funnel["total_replies"]
        ooo = funnel["ooo"]
        manual = total_replies - ooo
        li_connects = funnel["li_connects"]
        li_accepts = funnel["li_accepts"]
        li_messages = funnel["li_messages"]
        li_replies = funnel["li_replies"]
        interested = sentiment_by_campaign[camp_name]["interested"]
        not_interested = sentiment_by_campaign[camp_name]["not_interested"]

        rows.append([f"{camp_name} — Funnel", "", ""])
        rows.append(["Stage", "Volume", "% of Sent"])
        rows.append(["Unique Leads Contacted", us, "100%"])
        rows.append(["Emails Sent", delivered, pct(delivered, us)])
        rows.append(["Delivered", delivered, pct(delivered, us)])
        rows.append(["Bounced", bounced, pct(bounced, us)])
        rows.append(["Total Replies", total_replies, pct(total_replies, us)])
        rows.append(["OOO Auto-Replies", ooo, pct(ooo, us)])
        rows.append(["Manual Replies", manual, pct(manual, us)])
        if li_connects:
            rows.append(["LinkedIn Connects", li_connects, "—"])
            rows.append(["LinkedIn Accepts", li_accepts, pct(li_accepts, li_connects)])
            rows.append(["LinkedIn Messages", li_messages, "—"])
            rows.append(["LinkedIn Replies", li_replies, pct(li_replies, li_messages) if li_messages else "—"])
        rows.append(["Interested Leads", interested, pct(interested, us)])
        rows.append(["Not Interested", not_interested, pct(not_interested, us)])
        rows.append([])  # spacer

    return {
        "title": "Campaign Funnels",
        "headers": ["Stage", "Volume", "% of Sent"],
        "rows": rows,
    }


# ── Sheet 3: Lead Sentiment Summary ─────────────────────────────────


def build_sentiment_sheet(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str],
) -> dict[str, Any]:
    """Build the Lead Sentiment pivot sheet."""
    sentiment_rows = _query_sentiment_per_campaign(conn, workspace, since)
    sentiment_order = ["positive", "interested", "neutral",
                        "negative", "not_interested", "invalid"]

    pivot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in sentiment_rows:
        cam = strip_workspace_prefix(row["campaign"], workspace)
        s = (row["sentiment"] or "").lower()
        if s in sentiment_order:
            pivot[cam][s] += row["lead_count"]

    headers = (
        ["Campaign"]
        + [s.capitalize() for s in sentiment_order]
        + ["Total Tagged", "Positivity Rate"]
    )

    if not pivot:
        return {
            "title": "Lead Sentiment",
            "headers": headers,
            "rows": [["No sentiment data in this period", "", "", "", "", "", "", "", ""]],
        }

    rows = []
    for campaign_name in sorted(pivot.keys()):
        counts = pivot[campaign_name]
        total_tagged = sum(counts.values())
        positive = counts.get("positive", 0) + counts.get("interested", 0)
        negative = counts.get("negative", 0) + counts.get("not_interested", 0)
        positivity_denom = positive + negative
        positivity = pct(positive, positivity_denom) if positivity_denom else "—"

        row = [campaign_name]
        for s in sentiment_order:
            row.append(counts.get(s, 0))
        row.append(total_tagged)
        row.append(positivity)
        rows.append(row)

    return {
        "title": "Lead Sentiment",
        "headers": headers,
        "rows": rows,
    }


# ── Top-level payload builder ──────────────────────────────────────


def build_campaign_stats_payload(
    conn: sqlite3.Connection,
    *,
    workspace: str,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """Query SQLite and build the full multi-sheet payload.

    Returns a dict suitable for JSON serialization:
        {template, title, workspace, since, sheets: [{title, headers, rows}, ...]}
    """
    sheet1 = build_overview_sheet(conn, workspace, since)
    sheet2 = build_funnels_sheet(conn, workspace, since)
    sheet3 = build_sentiment_sheet(conn, workspace, since)

    since_label = normalize_since(since) or since or "all"
    if isinstance(since_label, str) and since_label.startswith("datetime("):
        since_label = since or "all"
    title = f"{workspace.capitalize()} Campaign Stats — {since_label}"

    return {
        "template": "campaign-stats",
        "title": title,
        "workspace": workspace,
        "since": since,
        "sheets": [sheet1, sheet2, sheet3],
    }
