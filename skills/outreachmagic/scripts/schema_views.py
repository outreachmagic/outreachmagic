"""Read-only SQL views for agent analytics (applied in migrate_db)."""

V_INBOUND_EVENTS_BY_CAMPAIGN = """
CREATE VIEW IF NOT EXISTS v_inbound_events_by_campaign AS
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


def ensure_read_views(conn) -> None:
    """Idempotent analytics views."""
    conn.executescript(V_INBOUND_EVENTS_BY_CAMPAIGN)
