#!/usr/bin/env python3
"""
Campaign Stats — aggregated campaign performance data for Google Sheets export.

Builds a 4-sheet workbook:
  Sheet 1: Campaign Overview — master table (one row per campaign)
  Sheet 2: Campaign Funnels — per-campaign conversion funnels
  Sheet 3: Lead Sentiment — sentiment distribution pivot across campaigns
  Sheet 4: Daily Breakdown — one row per (campaign, day) for time-series
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

# Timezone offset for daily breakdown date splitting (e.g. -4 for EDT / UTC-4)
DAY_SPLIT_OFFSET_HOURS = -4


def strip_workspace_prefix(campaign_name: str, workspace: str) -> str:
    """Strip '{workspace} |' prefix from campaign names for display."""
    prefix = f"{workspace} |"
    if campaign_name.lower().startswith(prefix.lower()):
        return campaign_name[len(prefix):].lstrip()
    return campaign_name


def pct(num: int, denom: int) -> str:
    """Safe percentage string (e.g. '43.9%' or '\u2014')."""
    if denom and denom > 0:
        return f"{round(num / denom * 100, 1)}%"
    return "\u2014"


def format_date(dt_str: Optional[str]) -> str:
    """Format ISO timestamp to short date like 'Jun 13'."""
    if not dt_str:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d")
    except (ValueError, TypeError):
        return dt_str[:10] if dt_str else "\u2014"


def campaign_status(sends_in_window: int, sends_outside_window: int) -> str:
    """Detect campaign status based on send counts."""
    if sends_in_window > 0:
        return "active"
    if sends_outside_window > 0:
        return "paused"
    return "exhausted"


def reply_event_sql_condition() -> str:
    """Canonical reply event filter from platform_registry."""
    return """(
      LOWER(e.event_type) IN ('email_reply', 'linkedin_reply')
      OR (LOWER(e.direction) = 'inbound' AND LOWER(e.event_type) = 'email')
    )"""


def build_since_expr(since: Optional[str]) -> tuple[str, str]:
    """Convert since parameter to (bare_expr, qualified_expr) for SQL.

    bare_expr — no table alias (use in CTEs)
    qualified_expr — with e. prefix (use in top-level queries)
    """
    if not since or since.lower() == "all":
        return "1=1", "1=1"
    if since.endswith("d"):
        days = int(since.replace("d", ""))
        return f"created_at >= datetime('now', '-{days} days')", \
               f"e.created_at >= datetime('now', '-{days} days')"
    if "-" in since:
        return f"date(created_at) >= '{since}'", f"date(e.created_at) >= '{since}'"
    return "created_at >= datetime('now', '-14 days')", \
           "e.created_at >= datetime('now', '-14 days')"


def build_campaign_stats_payload(
    conn: sqlite3.Connection,
    workspace: str,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """Build a 4-sheet campaign stats payload for review_cloud.export_review.

    Args:
        conn: Open SQLite connection to OutreachMagic DB
        workspace: Workspace slug (e.g. 'popcam')
        since: Time window \u2014 '14d', '30d', '7d', 'all', or 'YYYY-MM-DD'

    Returns:
        dict with template, title, and sheets list
    """
    bare_expr, qualified_expr = build_since_expr(since)
    reply_cond = reply_event_sql_condition()
    ws_like = f"{workspace} |%"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cell_note = (
        f"Settings: workspace={workspace}, "
        f"since={since or 'all'}, "
        f"generated={generated_at}, "
        f"tz_offset={DAY_SPLIT_OFFSET_HOURS}"
    )

    # \u2500\u2500 Sheet 1: Campaign Overview \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    overview_query = f"""
    WITH campaign_ids AS (
      SELECT id, name FROM campaigns WHERE name LIKE ?
    ),
    window_sends AS (
      SELECT campaign_id, COUNT(*) AS sent_count
      FROM events WHERE event_type IN ('email_sent', 'email_sent_auto')
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    ),
    all_sends AS (
      SELECT campaign_id, COUNT(*) AS total_sent
      FROM events WHERE event_type IN ('email_sent', 'email_sent_auto')
        AND campaign_id IN (SELECT id FROM campaign_ids)
      GROUP BY campaign_id
    ),
    bounces AS (
      SELECT campaign_id, COUNT(*) AS bounce_count
      FROM events WHERE event_type IN ('email_bounce', 'bounced_email', 'email_bounced')
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    ),
    replies AS (
      SELECT campaign_id, COUNT(*) AS reply_count
      FROM events e WHERE {reply_cond}
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id
    ),
    ooo AS (
      SELECT campaign_id, COUNT(*) AS ooo_count
      FROM events e WHERE {reply_cond}
        AND CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id
    ),
    li_connects AS (
      SELECT campaign_id, COUNT(*) AS connect_count
      FROM events WHERE event_type = 'linkedin_connect'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    ),
    li_accepts AS (
      SELECT campaign_id, COUNT(*) AS accept_count
      FROM events WHERE event_type = 'linkedin_connection_accepted'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    ),
    li_messages AS (
      SELECT campaign_id, COUNT(*) AS message_count
      FROM events WHERE event_type = 'linkedin_message' AND lower(coalesce(direction,'')) = 'outbound'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    ),
    li_replies AS (
      SELECT campaign_id, COUNT(*) AS li_reply_count
      FROM events e WHERE event_type = 'linkedin_reply'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id
    ),
    last_activity AS (
      SELECT campaign_id, MAX(created_at) AS last_at
      FROM events WHERE campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id
    )
    SELECT
      c.name,
      COALESCE(s.sent_count, 0) AS sent,
      COALESCE(b.bounce_count, 0) AS bounced,
      COALESCE(r.reply_count, 0) AS total_replies,
      COALESCE(o.ooo_count, 0) AS ooo,
      COALESCE(lc.connect_count, 0) AS li_connects,
      COALESCE(la.accept_count, 0) AS li_accepts,
      COALESCE(lm.message_count, 0) AS li_messages,
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

    overview_cursor = conn.execute(overview_query, (ws_like,))
    overview_cols = [desc[0] for desc in overview_cursor.description]
    overview_rows_raw = [dict(zip(overview_cols, r)) for r in overview_cursor.fetchall()]

    # \u2500\u2500 Sentiment data (shared across sheets) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    sentiment_query = f"""
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
        AND {qualified_expr}
    )
    SELECT
      c.name AS campaign,
      rs.sentiment,
      COUNT(DISTINCT rs.lead_id) AS lead_count
    FROM ranked_status rs
    JOIN campaigns c ON rs.campaign_id = c.id
    WHERE rs.rn = 1
      AND rs.sentiment IN ('positive', 'interested', 'neutral', 'negative', 'not_interested', 'invalid')
      AND c.name LIKE ?
    GROUP BY c.name, rs.sentiment
    ORDER BY campaign, lead_count DESC
    """
    sent_cursor = conn.execute(sentiment_query, (ws_like,))
    sent_cols = [desc[0] for desc in sent_cursor.description]
    sentiment_rows = [dict(zip(sent_cols, r)) for r in sent_cursor.fetchall()]

    # Build sentiment pivot: campaign -> sentiment -> count
    sentiment_pivot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sr in sentiment_rows:
        sentiment_pivot[sr["campaign"]][sr["sentiment"]] += sr["lead_count"]

    # Gather interested/not_interested counts per campaign
    interested_sentiments = {"positive", "interested"}
    not_interested_sentiments = {"negative", "not_interested"}
    sentiment_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"interested": 0, "not_interested": 0})
    for sr in sentiment_rows:
        camp = sr["campaign"]
        if sr["sentiment"] in interested_sentiments:
            sentiment_totals[camp]["interested"] += sr["lead_count"]
        elif sr["sentiment"] in not_interested_sentiments:
            sentiment_totals[camp]["not_interested"] += sr["lead_count"]

    # \u2500\u2500 Build Sheet 1: Campaign Overview \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    overview_headers = [
        "Campaign", "Status",
        "Interested", "Not Interested",
        "Sent", "Delivered", "Bounced", "Bounce %",
        "Total Replies", "OOO", "Human", "Reply %",
        "LI Connects", "LI Accepts", "Accept %", "LI Messages", "LI Replies",
        "Last Activity"
    ]
    overview_rows = []
    for r in overview_rows_raw:
        name = strip_workspace_prefix(r["name"], workspace)
        sent = r["sent"]
        bounced = r["bounced"]
        total_replies = r["total_replies"]
        ooo = r["ooo"]
        human = total_replies - ooo
        delivered = sent - bounced
        li_connects = r["li_connects"]
        li_accepts = r["li_accepts"]
        li_messages = r["li_messages"]
        li_replies = r["li_replies"]
        status = campaign_status(sent, r["all_time_sent"] - sent)

        st = sentiment_totals.get(r["name"], {"interested": 0, "not_interested": 0})
        interested = st["interested"] or "\u2014"
        not_interested = st["not_interested"] or "\u2014"

        overview_rows.append([
            name, status,
            interested, not_interested,
            sent if sent > 0 else "\u2014",
            delivered if delivered > 0 else "\u2014",
            bounced if bounced > 0 else "\u2014",
            pct(bounced, sent),
            total_replies if total_replies > 0 else "\u2014",
            ooo if ooo > 0 else "\u2014",
            human if human > 0 else "\u2014",
            pct(total_replies, delivered),
            li_connects if li_connects > 0 else "\u2014",
            li_accepts if li_accepts > 0 else "\u2014",
            pct(li_accepts, li_connects),
            li_messages if li_messages > 0 else "\u2014",
            li_replies if li_replies > 0 else "\u2014",
            format_date(r["last_activity"]),
        ])

    # \u2500\u2500 Build Sheet 2: Campaign Funnels \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    funnel_headers = ["Stage", "Volume", "%"]
    funnel_rows = []
    active_campaigns = [r for r in overview_rows_raw if r["sent"] > 0]

    for camp in active_campaigns:
        name = strip_workspace_prefix(camp["name"], workspace)
        sent = camp["sent"]
        bounced = camp["bounced"]
        replies = camp["total_replies"]
        ooo = camp["ooo"]
        human = replies - ooo

        st = sentiment_totals.get(camp["name"], {"interested": 0, "not_interested": 0})
        li_connects = camp["li_connects"]
        li_accepts = camp["li_accepts"]
        li_messages = camp["li_messages"]
        li_replies = camp["li_replies"]
        delivered = sent - bounced

        funnel_rows.append([f"{name} \u2014 Funnel", "", ""])
        funnel_rows.append(funnel_headers)
        funnel_rows.append(["Emails Sent (total)", sent, "100%"]) if sent > 0 else None
        if delivered >= 0 and sent > 0:
            funnel_rows.append(["Emails Delivered", delivered, pct(delivered, sent)])
        funnel_rows.append(["Bounced", bounced, pct(bounced, sent)])
        funnel_rows.append(["Total Replies", replies, pct(replies, sent)])
        funnel_rows.append(["OOO Auto-Replies", ooo, pct(ooo, sent)])
        funnel_rows.append(["Human Replies", human, pct(human, sent)])

        if li_connects > 0:
            funnel_rows.append(["LinkedIn Connects", li_connects, ""])
        if li_accepts > 0:
            funnel_rows.append(["LinkedIn Accepts", li_accepts, pct(li_accepts, li_connects) if li_connects > 0 else "\u2014"])
        if li_messages > 0:
            funnel_rows.append(["LinkedIn Messages", li_messages, ""])
        if li_replies > 0:
            funnel_rows.append(["LinkedIn Replies", li_replies, pct(li_replies, li_messages) if li_messages > 0 else "\u2014"])

        total_int = st["interested"] + st["not_interested"]
        if total_int > 0:
            funnel_rows.append(["Interested Leads", st["interested"], pct(st["interested"], sent)])
            funnel_rows.append(["Not Interested", st["not_interested"], pct(st["not_interested"], sent)])

        funnel_rows.append([])  # spacer

    # \u2500\u2500 Build Sheet 3: Lead Sentiment \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    sentiment_order = ["positive", "interested", "neutral", "negative", "not_interested", "invalid"]
    sentiment_headers = ["Campaign"] + [s.capitalize() for s in sentiment_order] + ["Total Tagged", "Positivity Rate"]
    sentiment_body = []

    all_campaigns_with_sentiment = sorted(sentiment_pivot.keys())
    for camp in all_campaigns_with_sentiment:
        display_name = strip_workspace_prefix(camp, workspace)
        row_data = [display_name]
        total_tagged = 0
        for s in sentiment_order:
            count = sentiment_pivot[camp].get(s, 0)
            row_data.append(count)
            total_tagged += count

        pos = sentiment_pivot[camp].get("positive", 0) + sentiment_pivot[camp].get("interested", 0)
        neg = sentiment_pivot[camp].get("negative", 0) + sentiment_pivot[camp].get("not_interested", 0)
        positivity = pct(pos, pos + neg) if (pos + neg) > 0 else "\u2014"

        row_data.append(total_tagged)
        row_data.append(positivity)
        sentiment_body.append(row_data)

    if not sentiment_body:
        sentiment_body.append(["No sentiment data in this period", "", "", "", "", "", "", "", ""])

    # \u2500\u2500 Build Sheet 4: Daily Breakdown \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    offset = f"{DAY_SPLIT_OFFSET_HOURS:+d}"
    daily_query = f"""
    WITH campaign_ids AS (
      SELECT id, name FROM campaigns WHERE name LIKE ?
    ),
    daily_sends AS (
      SELECT campaign_id,
             date(datetime(created_at, '{offset} hours')) AS day,
             COUNT(*) AS sent_count
      FROM events WHERE event_type IN ('email_sent', 'email_sent_auto')
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id, day
    ),
    daily_bounces AS (
      SELECT campaign_id,
             date(datetime(created_at, '{offset} hours')) AS day,
             COUNT(*) AS bounce_count
      FROM events WHERE event_type IN ('email_bounce', 'bounced_email', 'email_bounced')
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id, day
    ),
    daily_replies AS (
      SELECT e.campaign_id,
             date(datetime(e.created_at, '{offset} hours')) AS day,
             COUNT(*) AS reply_count
      FROM events e WHERE {reply_cond}
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id, day
    ),
    daily_ooo AS (
      SELECT e.campaign_id,
             date(datetime(e.created_at, '{offset} hours')) AS day,
             COUNT(*) AS ooo_count
      FROM events e WHERE {reply_cond}
        AND CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id, day
    ),
    daily_li_connects AS (
      SELECT campaign_id,
             date(datetime(created_at, '{offset} hours')) AS day,
             COUNT(*) AS connect_count
      FROM events WHERE event_type = 'linkedin_connect'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id, day
    ),
    daily_li_accepts AS (
      SELECT campaign_id,
             date(datetime(created_at, '{offset} hours')) AS day,
             COUNT(*) AS accept_count
      FROM events WHERE event_type = 'linkedin_connection_accepted'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id, day
    ),
    daily_li_messages AS (
      SELECT campaign_id,
             date(datetime(created_at, '{offset} hours')) AS day,
             COUNT(*) AS message_count
      FROM events WHERE event_type = 'linkedin_message' AND lower(coalesce(direction,'')) = 'outbound'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {bare_expr}
      GROUP BY campaign_id, day
    ),
    daily_li_replies AS (
      SELECT e.campaign_id,
             date(datetime(e.created_at, '{offset} hours')) AS day,
             COUNT(*) AS li_reply_count
      FROM events e WHERE event_type = 'linkedin_reply'
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
      GROUP BY campaign_id, day
    ),
    ranked_daily_sentiment AS (
      SELECT e.campaign_id,
             e.lead_id,
             date(datetime(e.created_at, '{offset} hours')) AS day,
             LOWER(JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment')) AS sentiment,
             ROW_NUMBER() OVER (
               PARTITION BY e.lead_id, date(datetime(e.created_at, '{offset} hours'))
               ORDER BY e.created_at DESC, e.id DESC
             ) AS rn
      FROM events e
      WHERE JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
        AND campaign_id IN (SELECT id FROM campaign_ids)
        AND {qualified_expr}
    ),
    daily_sentiment AS (
      SELECT campaign_id, day,
             SUM(CASE WHEN sentiment IN ('positive', 'interested') THEN 1 ELSE 0 END) AS interested_count,
             SUM(CASE WHEN sentiment IN ('negative', 'not_interested') THEN 1 ELSE 0 END) AS not_interested_count
      FROM ranked_daily_sentiment
      WHERE rn = 1
      GROUP BY campaign_id, day
    ),
    daily_days AS (
      SELECT campaign_id, day FROM daily_sends
      UNION SELECT campaign_id, day FROM daily_bounces
      UNION SELECT campaign_id, day FROM daily_replies
      UNION SELECT campaign_id, day FROM daily_ooo
      UNION SELECT campaign_id, day FROM daily_li_connects
      UNION SELECT campaign_id, day FROM daily_li_accepts
      UNION SELECT campaign_id, day FROM daily_li_messages
      UNION SELECT campaign_id, day FROM daily_li_replies
      UNION SELECT campaign_id, day FROM daily_sentiment
    )
    SELECT
      c.name,
      dd.day,
      COALESCE(ds.sent_count, 0) AS sent,
      COALESCE(db.bounce_count, 0) AS bounced,
      COALESCE(dr.reply_count, 0) AS total_replies,
      COALESCE(do.ooo_count, 0) AS ooo,
      COALESCE(dlc.connect_count, 0) AS li_connects,
      COALESCE(dla.accept_count, 0) AS li_accepts,
      COALESCE(dlm.message_count, 0) AS li_messages,
      COALESCE(dlr.li_reply_count, 0) AS li_replies,
      COALESCE(dsi.interested_count, 0) AS interested,
      COALESCE(dsi.not_interested_count, 0) AS not_interested
    FROM daily_days dd
    JOIN campaign_ids c ON dd.campaign_id = c.id
    LEFT JOIN daily_sends ds ON dd.campaign_id = ds.campaign_id AND dd.day = ds.day
    LEFT JOIN daily_bounces db ON dd.campaign_id = db.campaign_id AND dd.day = db.day
    LEFT JOIN daily_replies dr ON dd.campaign_id = dr.campaign_id AND dd.day = dr.day
    LEFT JOIN daily_ooo do ON dd.campaign_id = do.campaign_id AND dd.day = do.day
    LEFT JOIN daily_li_connects dlc ON dd.campaign_id = dlc.campaign_id AND dd.day = dlc.day
    LEFT JOIN daily_li_accepts dla ON dd.campaign_id = dla.campaign_id AND dd.day = dla.day
    LEFT JOIN daily_li_messages dlm ON dd.campaign_id = dlm.campaign_id AND dd.day = dlm.day
    LEFT JOIN daily_li_replies dlr ON dd.campaign_id = dlr.campaign_id AND dd.day = dlr.day
    LEFT JOIN daily_sentiment dsi ON dd.campaign_id = dsi.campaign_id AND dd.day = dsi.day
    ORDER BY c.name, dd.day DESC
    """

    daily_cursor = conn.execute(daily_query, (ws_like,))
    daily_cols = [desc[0] for desc in daily_cursor.description]
    daily_rows_raw = [dict(zip(daily_cols, r)) for r in daily_cursor.fetchall()]

    daily_headers = [
        "Campaign", "Day",
        "Interested", "Not Interested",
        "Sent", "Delivered", "Bounced", "Bounce %",
        "Total Replies", "OOO", "Human", "Reply %",
        "LI Connects", "LI Accepts", "Accept %", "LI Messages", "LI Replies",
    ]
    daily_rows = []
    for r in daily_rows_raw:
        name = strip_workspace_prefix(r["name"], workspace)
        sent = r["sent"]
        bounced = r["bounced"]
        total_replies = r["total_replies"]
        ooo = r["ooo"]
        human = total_replies - ooo
        delivered = sent - bounced
        interested = r["interested"]
        not_interested = r["not_interested"]

        daily_rows.append([
            name, r["day"],
            interested if interested > 0 else "\u2014",
            not_interested if not_interested > 0 else "\u2014",
            sent if sent > 0 else "\u2014",
            delivered if delivered > 0 else "\u2014",
            bounced if bounced > 0 else "\u2014",
            pct(bounced, sent),
            total_replies if total_replies > 0 else "\u2014",
            ooo if ooo > 0 else "\u2014",
            human if human > 0 else "\u2014",
            pct(total_replies, delivered),
            r["li_connects"] if r["li_connects"] > 0 else "\u2014",
            r["li_accepts"] if r["li_accepts"] > 0 else "\u2014",
            pct(r["li_accepts"], r["li_connects"]),
            r["li_messages"] if r["li_messages"] > 0 else "\u2014",
            r["li_replies"] if r["li_replies"] > 0 else "\u2014",
        ])

    notes = {"A1": cell_note}
    sheets = [
        {
            "title": "Campaign Overview",
            "notes": notes,
            "headers": overview_headers,
            "rows": overview_rows,
        },
        {
            "title": "Campaign Funnels",
            "notes": notes,
            "headers": funnel_headers,
            "rows": funnel_rows,
        },
        {
            "title": "Lead Sentiment",
            "notes": notes,
            "headers": sentiment_headers,
            "rows": sentiment_body,
        },
        {
            "title": "Daily Breakdown",
            "notes": notes,
            "filter": True,
            "headers": daily_headers,
            "rows": daily_rows,
        },
    ]

    return {
        "template": "campaign-stats",
        "title": f"{workspace.title()} Campaign Stats - {since or '14d'}",
        "sheets": sheets,
    }
