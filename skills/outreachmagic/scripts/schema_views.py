"""Read-only SQL views for agent analytics (applied in migrate_db).

These exist so an agent asking a reporting question never has to re-derive the
tricky expressions by hand. The reply condition in particular is an OR inside an
AND -- written out by hand it loses its parentheses, the date filter gets
swallowed by operator precedence, and every time window quietly returns all-time
totals. Selecting from `v_reply_events` makes that failure impossible: the
condition is already applied and parenthesized inside the view, so the caller
writes a flat AND chain.

Keep them PLAIN FILTER views -- no aggregation. SQLite flattens those into the
outer query, so they cost nothing. A view with a per-lead MAX() aggregate would
not flatten, and that is the shape that took one campaign query from 1.4s to
926s (see the temp-table note in reporting_queries._LeadScope).
"""

from campaign_stats import reply_kind_case_sql
from platform_registry import reply_event_sql_condition, reply_event_types_sql_list

# Legacy view: every inbound event, campaign-joined.
V_INBOUND_EVENTS_BY_CAMPAIGN = """
CREATE VIEW v_inbound_events_by_campaign AS
SELECT
    e.id AS event_id,
    e.lead_id,
    e.event_type,
    e.direction,
    e.channel,
    e.created_at,
    c.id AS campaign_id,
    c.name AS campaign_name
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE lower(coalesce(e.direction, '')) = 'inbound';
"""

# One row per REPLY event, workspace-scoped, already classified.
#
#   SELECT campaign_id, COUNT(DISTINCT lead_id)
#     FROM v_reply_events
#    WHERE workspace_id = ? AND reply_kind = 'human'
#      AND event_at >= ? AND event_at < ?
#    GROUP BY campaign_id;
#
# Only `human` and `auto_reply` appear here: `bounce` and `status_only` are
# lead-level verdicts with no reply event behind them, so they live on
# v_lead_sentiment instead. `event_at` is the workspace's own timestamp, which
# is what every dashboard range filters on -- not events.created_at.
V_REPLY_EVENTS = f"""
CREATE VIEW v_reply_events AS
SELECT
    wle.workspace_id,
    wle.lead_id,
    wle.event_at,
    e.id AS event_id,
    e.campaign_id,
    c.name AS campaign_name,
    e.event_type,
    e.direction,
    e.channel,
    e.subject,
    {reply_kind_case_sql("e")} AS reply_kind
FROM workspace_lead_events wle
JOIN events e ON e.id = wle.event_id
LEFT JOIN campaigns c ON c.id = e.campaign_id
WHERE wle.event_type IN ({reply_event_types_sql_list()})
  AND ({reply_event_sql_condition()
        .replace("event_type", "e.event_type").replace("direction", "e.direction")});
"""

# One row per lead carrying a sentiment, with both anchors named. Which column
# anchors a window is the other thing agents guess at: `current_sentiment_since`
# is when the lead entered its CURRENT sentiment (a flip away and back resets
# it), `first_positive_at` is the first time it ever turned positive and never
# moves. Performance questions want the second.
V_LEAD_SENTIMENT = """
CREATE VIEW v_lead_sentiment AS
SELECT
    wl.workspace_id,
    wl.lead_id,
    lower(wl.current_status_sentiment) AS sentiment,
    wl.current_status_label AS status_label,
    wl.current_sentiment_since,
    wl.first_positive_at,
    wl.last_activity_at
FROM workspace_leads wl
WHERE wl.current_status_sentiment IS NOT NULL;
"""

_VIEWS = {
    "v_inbound_events_by_campaign": V_INBOUND_EVENTS_BY_CAMPAIGN,
    "v_reply_events": V_REPLY_EVENTS,
    "v_lead_sentiment": V_LEAD_SENTIMENT,
}


def ensure_read_views(conn) -> None:
    """Idempotent analytics views.

    DROP then CREATE, deliberately: `CREATE VIEW IF NOT EXISTS` will not replace
    a changed definition, so an upgraded install would keep the old body of a
    view forever and quietly report against last release's logic.
    """
    for name, ddl in _VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.executescript(ddl)
