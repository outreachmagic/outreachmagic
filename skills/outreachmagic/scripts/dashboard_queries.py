"""Read-only query layer for the local dashboard.

Every function takes an open connection plus a workspace_id and returns a
JSON-shaped dict. No HTTP concerns here — dashboard_server routes call these,
and tests hit them directly. Workspace scoping always goes through
workspace_leads / workspace_lead_events / workspace_sender_accounts /
bounce_events; events has no workspace_id of its own.

Deliverability note: opens, clicks, and spam complaints are not delivered to
the local DB by any platform, so deliverability here is bounce trend +
per-mailbox health (sender_accounts) + domain blacklist (sender_domains).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from typing import Optional

from constants import PIPELINE_STAGES
from platform_registry import reply_event_sql_condition
from read_queries import _since_clause
from workspace_routing import DEFAULT_ORG_ID, get_org_routing_config

BOUNCE_EVENT_TYPES_SQL = "('email_bounce', 'email_bounced')"

# workspace-scoped events with content columns; bare event_type/direction in
# outer WHERE fragments (e.g. reply_event_sql_condition) resolve unambiguously.
_WS_EVENTS_CTE = """
WITH ws_events AS (
    SELECT e.id, e.lead_id, e.event_type, e.direction, e.channel,
           e.subject, e.campaign_id, wle.event_at
    FROM workspace_lead_events wle
    JOIN events e ON e.id = wle.event_id
    WHERE wle.workspace_id = ?{range_clause}
)
"""

ATTRIBUTE_FIELDS = {
    "industry": "l.industry",
    "title": "l.title",
    "headcount": "l.headcount",
    "location_country": "l.location_country",
    "source": "l.original_source_platform",
}

# sender_accounts.bounce_rate at or above this flags the mailbox.
MAILBOX_BOUNCE_RATE_THRESHOLD = 0.05


def _range_clause(
    since: Optional[str], until: Optional[str], column: str,
) -> tuple[str, list]:
    """Parameterized date-range filter. `until` is inclusive of its whole day."""
    clause, params = _since_clause(since, column=column)
    if until and until.strip():
        clause += f" AND {column} < datetime(?, '+1 day')"
        params.append(until.strip())
    return clause, params


def _ws_events(since: Optional[str], until: Optional[str] = None) -> tuple[str, list]:
    clause, params = _range_clause(since, until, "wle.event_at")
    return _WS_EVENTS_CTE.format(range_clause=clause), params


def _active_in_range(
    since: Optional[str], until: Optional[str], wl_alias: str = "wl",
) -> tuple[str, list]:
    """EXISTS fragment: the workspace_leads row `wl_alias` had an event in range.

    Turns a current-state query into a "leads active in this window" query — the
    agreed semantic for every current-state tab when a date range is set. Returns
    ("", []) when no range is active, so callers pass it through unconditionally.
    """
    clause, params = _range_clause(since, until, "wle_r.event_at")
    if not clause:
        return "", []
    return (
        f" AND EXISTS (SELECT 1 FROM workspace_lead_events wle_r"
        f" WHERE wle_r.workspace_id = {wl_alias}.workspace_id"
        f" AND wle_r.lead_id = {wl_alias}.lead_id{clause})",
        params,
    )


def list_workspaces(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id, name, slug FROM workspaces WHERE org_id = ? ORDER BY name",
        (DEFAULT_ORG_ID,),
    ).fetchall()
    routing = get_org_routing_config(conn, DEFAULT_ORG_ID)
    return {
        "workspaces": [dict(r) for r in rows],
        "routing_mode": routing.mode,
    }


def summary(
    conn: sqlite3.Connection, workspace_id: str,
    since: str = "7d", until: Optional[str] = None,
) -> dict:
    # Stage tiles reflect leads *active in the selected range* (so "Interested"
    # matches what the campaigns/replies views show for that window). With the
    # "all available" preset (since falsy/all), _active_in_range is a no-op and
    # every lead counts.
    active_sql, active_params = _active_in_range(since, until, "workspace_leads")
    stage_rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM workspace_leads"
        f" WHERE workspace_id = ?{active_sql} GROUP BY status",
        (workspace_id, *active_params),
    ).fetchall()
    stages = {stage: 0 for stage in PIPELINE_STAGES}
    for r in stage_rows:
        stages[r["status"]] = stages.get(r["status"], 0) + r["n"]

    cte, range_params = _ws_events(since, until)
    counts = conn.execute(
        cte + f"""
        SELECT
            SUM(event_type = 'email_sent') AS sent,
            SUM({reply_event_sql_condition()}) AS replied,
            SUM(event_type IN {BOUNCE_EVENT_TYPES_SQL}) AS bounced
        FROM ws_events""",
        (workspace_id, *range_params),
    ).fetchone()
    latest = conn.execute(
        "SELECT MAX(event_at) AS latest FROM workspace_lead_events WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    # Positive = leads whose first positive-sentiment event falls in the range,
    # counted once each — the same definition the daily chart and campaign table
    # use, so the tile can't disagree with them. Cached briefly (polled tile).
    positive = _positive_count_in_range(conn, workspace_id, since, until)
    return {
        "stages": stages,
        "total_leads": sum(stages.values()),
        "since": since,
        "until": until,
        "sent": counts["sent"] or 0,
        "replied": counts["replied"] or 0,
        "bounced": counts["bounced"] or 0,
        "positive": positive,
        "latest_event_at": latest["latest"],
    }


def bounce_series(
    conn: sqlite3.Connection, workspace_id: str,
    since: str = "30d", until: Optional[str] = None,
) -> dict:
    clause, params = _range_clause(since, until, "event_at")
    rows = conn.execute(
        f"""SELECT date(event_at) AS day,
               SUM(event_type = 'email_sent') AS sent,
               SUM(event_type IN {BOUNCE_EVENT_TYPES_SQL}) AS bounced
            FROM workspace_lead_events
            WHERE workspace_id = ?{clause}
              AND event_type IN ('email_sent', 'email_bounce', 'email_bounced')
            GROUP BY day ORDER BY day""",
        (workspace_id, *params),
    ).fetchall()
    series = []
    for r in rows:
        sent, bounced = r["sent"] or 0, r["bounced"] or 0
        series.append({
            "date": r["day"],
            "sent": sent,
            "bounced": bounced,
            "bounce_rate": round(bounced / sent, 4) if sent else None,
        })
    return {"since": since, "series": series}


def _mailbox_event_rates(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict[str, dict]:
    """Per-mailbox sends / replies / bounces from the real event stream, scoped
    to a date range — the honest basis for reply% and bounce% (the provider-
    exported sender_accounts.bounce_rate is stale/CSV-imported and often wrong).

    A reply is attributed to every mailbox that sent to the replying lead within
    the range; in cold outreach that's almost always exactly one mailbox, so the
    rate is accurate without a per-thread mailbox link the events don't carry."""
    reply_cond = reply_event_sql_condition()
    reply_cond = reply_cond.replace("event_type", "e.event_type").replace("direction", "e.direction")

    send_range, sp = _range_clause(since, until, "wle.event_at")
    sends = conn.execute(
        f"""SELECT e.sender AS mailbox, e.lead_id AS lead_id, COUNT(*) AS n
            FROM workspace_lead_events wle JOIN events e ON e.id = wle.event_id
            WHERE wle.workspace_id = ? AND e.event_type = 'email_sent'
              AND e.sender IS NOT NULL AND e.sender != ''{send_range}
            GROUP BY e.sender, e.lead_id""",
        (workspace_id, *sp),
    ).fetchall()
    rates: dict[str, dict] = {}
    lead_to_mailboxes: dict = defaultdict(set)
    for r in sends:
        m = rates.setdefault(r["mailbox"], {"sends": 0, "replies": 0, "bounces": 0})
        m["sends"] += r["n"]
        lead_to_mailboxes[r["lead_id"]].add(r["mailbox"])

    reply_range, rp = _range_clause(since, until, "wle.event_at")
    reply_rows = conn.execute(
        f"""SELECT DISTINCT wle.lead_id AS lead_id
            FROM workspace_lead_events wle JOIN events e ON e.id = wle.event_id
            WHERE wle.workspace_id = ? AND lower(e.direction) = 'inbound'
              AND lower(e.channel) = 'email' AND {reply_cond}{reply_range}""",
        (workspace_id, *rp),
    ).fetchall()
    for r in reply_rows:
        for m in lead_to_mailboxes.get(r["lead_id"], ()):
            rates[m]["replies"] += 1

    # Bounces from bounce_events (authoritative sender_email = the mailbox).
    # No workspace filter (older rows predate the column); consumers only read
    # their own mailboxes out of this map.
    b_range, bp = _range_clause(since, until, "last_seen_at")
    for r in conn.execute(
        f"""SELECT sender_email AS mailbox, COUNT(*) AS n
            FROM bounce_events
            WHERE sender_email IS NOT NULL AND sender_email != ''{b_range}
            GROUP BY sender_email""",
        bp,
    ).fetchall():
        m = rates.setdefault(r["mailbox"], {"sends": 0, "replies": 0, "bounces": 0})
        m["bounces"] += r["n"]
    return rates


def mailbox_health(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    rows = conn.execute(
        """SELECT sa.id, sa.email, sa.email_domain, sa.channel, sa.status,
                  sa.first_name, sa.last_name, sa.provider, sa.is_active,
                  sa.warmup_status, sa.spf_status, sa.dkim_status, sa.dmarc_status,
                  sa.overall_health_score, sa.bounce_rate, sa.daily_limit,
                  sa.last_outbound_at, sa.last_inbound_at
           FROM sender_accounts sa
           JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
           WHERE wsa.workspace_id = ?
           ORDER BY sa.email""",
        (workspace_id,),
    ).fetchall()
    event_rates = _mailbox_event_rates(conn, workspace_id, since, until)

    # Bounce trend per sender: last 7 days vs the 7 days before that. Not
    # workspace-filtered — a mailbox's health is a property of the mailbox
    # (and older bounce_events rows predate the workspace_id column anyway);
    # only the workspace's own mailboxes are looked up from this map.
    trend_rows = conn.execute(
        """SELECT sender_email,
                  SUM(CASE WHEN last_seen_at >= datetime('now', '-7 days')
                           THEN occurrence_count ELSE 0 END) AS recent,
                  SUM(CASE WHEN last_seen_at < datetime('now', '-7 days')
                            AND last_seen_at >= datetime('now', '-14 days')
                           THEN occurrence_count ELSE 0 END) AS prior
           FROM bounce_events
           GROUP BY sender_email""",
    ).fetchall()
    trend = {r["sender_email"]: (r["recent"] or 0, r["prior"] or 0) for r in trend_rows}

    mailboxes = []
    for r in rows:
        recent, prior = trend.get(r["email"], (0, 0))
        # Live rates from the event stream, scoped to the selected range —
        # these replace the provider-exported sender_accounts.bounce_rate.
        er = event_rates.get(r["email"], {"sends": 0, "replies": 0, "bounces": 0})
        sends = er["sends"]
        reply_rate = (er["replies"] / sends) if sends else None
        bounce_rate = (er["bounces"] / sends) if sends else None
        trending_down = bool(
            (recent > prior and recent > 0)
            or (bounce_rate is not None and bounce_rate >= MAILBOX_BOUNCE_RATE_THRESHOLD)
        )
        mb = dict(r)
        mb.update({
            "sends": sends,
            "replies": er["replies"],
            "reply_rate": reply_rate,
            "bounces": er["bounces"],
            "bounce_rate": bounce_rate,
            "bounces_last_7d": recent,
            "bounces_prior_7d": prior,
            "trending_down": trending_down,
        })
        mailboxes.append(mb)
    # Signal first: flagged, then measured bounce rate desc, then recently active.
    mailboxes.sort(key=lambda m: (
        not m["trending_down"],
        -(m["bounce_rate"] or 0),
        m["last_outbound_at"] is None,
        m["email"],
    ))
    return {
        "mailboxes": mailboxes,
        "flagged": [m["email"] for m in mailboxes if m["trending_down"]],
    }


def _parse_dnsbl_status(raw: Optional[str]) -> tuple[bool, str, Optional[str]]:
    """(listed, summary, checked_at) from sender_domains.dnsbl_status.

    blacklist_monitor stores a JSON report ({all_clean, results: [...]});
    tolerate plain-text values from older or hand-maintained rows.
    """
    text = (raw or "").strip()
    if not text:
        return False, "not checked", None
    if text.startswith("{"):
        try:
            report = json.loads(text)
        except json.JSONDecodeError:
            return True, text[:80], None
        listed_on = [r.get("name") or r.get("host") or "?"
                     for r in report.get("results", []) if r.get("status") == "listed"]
        listed = bool(listed_on) or report.get("all_clean") is False
        summary = "listed: " + ", ".join(listed_on) if listed_on else (
            "clean" if report.get("all_clean") else "checked")
        return listed, summary, report.get("checked_at")
    return text.lower() not in ("clean", "ok", "not_listed", "unlisted"), text, None


def domain_health(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    # Domains come from the workspace's own mailboxes; sender_domains is a
    # LEFT JOIN because it only holds domains someone registered there
    # (reseller/pricing/DNSBL) — a workspace's sending domains must show up
    # even when nobody has registered them yet.
    # mailboxes counts live accounts only (matches the cost/count semantics on
    # the CLI); an all-decommissioned domain still surfaces via its is_active=0
    # sender_domains row so it can be seen and reactivated.
    rows = conn.execute(
        """SELECT sa.email_domain AS domain,
                  SUM(sa.is_active = 1) AS mailboxes,
                  COUNT(sa.id) AS mailboxes_total,
                  MAX(sa.last_outbound_at) AS last_outbound_at,
                  sd.domain IS NOT NULL AS registered,
                  COALESCE(sd.is_active, 1) AS domain_active,
                  sd.dnsbl_status, sd.sending_ip, sd.reseller,
                  sd.domain_cost, sd.currency
           FROM sender_accounts sa
           JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
           LEFT JOIN sender_domains sd ON sd.domain = sa.email_domain
           WHERE wsa.workspace_id = ?
             AND sa.email_domain IS NOT NULL AND sa.email_domain != ''
           GROUP BY sa.email_domain
           ORDER BY sa.email_domain""",
        (workspace_id,),
    ).fetchall()

    # Roll the per-mailbox live event rates up to the domain, replacing the
    # provider-exported avg_health with avg reply% / bounce% from real events.
    mailbox_map = conn.execute(
        """SELECT sa.email AS email, sa.email_domain AS domain
           FROM sender_accounts sa
           JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
           WHERE wsa.workspace_id = ? AND sa.email_domain IS NOT NULL AND sa.email_domain != ''""",
        (workspace_id,),
    ).fetchall()
    event_rates = _mailbox_event_rates(conn, workspace_id, since, until)
    per_domain: dict[str, dict] = defaultdict(lambda: {"sends": 0, "replies": 0, "bounces": 0})
    for m in mailbox_map:
        er = event_rates.get(m["email"])
        if er:
            d = per_domain[m["domain"]]
            d["sends"] += er["sends"]
            d["replies"] += er["replies"]
            d["bounces"] += er["bounces"]

    domains = []
    for r in rows:
        listed, summary, checked_at = _parse_dnsbl_status(r["dnsbl_status"])
        if not r["registered"]:
            summary = "not monitored"
        agg = per_domain.get(r["domain"], {"sends": 0, "replies": 0, "bounces": 0})
        sends = agg["sends"]
        domains.append({
            "domain": r["domain"],
            "mailboxes": r["mailboxes"] or 0,
            "mailboxes_total": r["mailboxes_total"],
            "sends": sends,
            "reply_rate": (agg["replies"] / sends) if sends else None,
            "bounce_rate": (agg["bounces"] / sends) if sends else None,
            "last_outbound_at": r["last_outbound_at"],
            "registered": bool(r["registered"]),
            "is_active": bool(r["domain_active"]),
            "sending_ip": r["sending_ip"],
            "reseller": r["reseller"],
            "provider": r["reseller"],
            "domain_cost": r["domain_cost"],
            "currency": r["currency"],
            "listed": listed,
            "dnsbl_summary": summary,
            "dnsbl_checked_at": checked_at,
        })
    return {
        "domains": domains,
        "flagged": [d["domain"] for d in domains if d["listed"]],
    }


def domain_detail(conn: sqlite3.Connection, workspace_id: str, domain: str) -> dict:
    """Everything known about one sending domain, for the click-through view."""
    reg = conn.execute(
        "SELECT * FROM sender_domains WHERE domain = ?", (domain,)
    ).fetchone()
    registration = dict(reg) if reg else None
    if registration:
        listed, summary, checked_at = _parse_dnsbl_status(registration.pop("dnsbl_status", None))
        registration.update(
            {"listed": listed, "dnsbl_summary": summary, "dnsbl_checked_at": checked_at})
    mailboxes = conn.execute(
        """SELECT sa.email, sa.status, sa.warmup_status, sa.daily_limit,
                  sa.spf_status, sa.dkim_status, sa.dmarc_status,
                  sa.overall_health_score, sa.bounce_rate,
                  sa.last_outbound_at, sa.last_inbound_at
           FROM sender_accounts sa
           JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
           WHERE wsa.workspace_id = ? AND sa.email_domain = ?
           ORDER BY sa.email""",
        (workspace_id, domain),
    ).fetchall()
    bounces = conn.execute(
        """SELECT COUNT(*) AS leads_bounced, SUM(occurrence_count) AS total_bounces,
                  MAX(last_seen_at) AS last_bounce_at
           FROM bounce_events
           WHERE sender_email LIKE '%@' || ?""",
        (domain,),
    ).fetchone()
    return {
        "domain": domain,
        "registration": registration,
        "mailboxes": [
            {**dict(m),
             "bounce_rate": None if (m["bounce_rate"] is not None and m["bounce_rate"] < 0)
             else m["bounce_rate"]}
            for m in mailboxes
        ],
        "bounce_summary": dict(bounces) if bounces else {},
    }


def pipeline_counts(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    # "Leads active in range" semantic: with a range set, count only leads that
    # had a workspace event in the window, by their current stage.
    active_sql, active_params = _active_in_range(since, until)
    rows = conn.execute(
        f"SELECT wl.status, COUNT(*) AS n FROM workspace_leads wl"
        f" WHERE wl.workspace_id = ?{active_sql} GROUP BY wl.status",
        (workspace_id, *active_params),
    ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    known = [{"stage": s, "count": counts.pop(s, 0)} for s in PIPELINE_STAGES]
    extra = [{"stage": s, "count": n} for s, n in sorted(counts.items())]
    return {"stages": known + extra, "since": since, "until": until,
            "range_active": bool(active_sql)}


def pipeline_leads(
    conn: sqlite3.Connection,
    workspace_id: str,
    status: str,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    active_sql, active_params = _active_in_range(since, until)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM workspace_leads wl"
        f" WHERE wl.workspace_id = ? AND wl.status = ?{active_sql}",
        (workspace_id, status, *active_params),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT l.id AS lead_id, l.name, l.company, l.title, l.email,
                  l.industry, l.headcount,
                  wl.status, wl.current_status_label, wl.current_status_sentiment,
                  wl.last_activity_at, wl.stage_entered_at,
                  wl.email_sent_count, wl.total_replies_count
           FROM workspace_leads wl
           JOIN leads l ON l.id = wl.lead_id
           WHERE wl.workspace_id = ? AND wl.status = ?{active_sql}
           ORDER BY wl.last_activity_at IS NULL, wl.last_activity_at DESC, l.id DESC
           LIMIT ? OFFSET ?""",
        (workspace_id, status, *active_params, limit, offset),
    ).fetchall()
    return {
        "status": status,
        "total": total,
        "limit": limit,
        "offset": offset,
        "leads": [dict(r) for r in rows],
    }


def attribute_performance(
    conn: sqlite3.Connection,
    workspace_id: str,
    field: str = "industry",
    min_sample: int = 20,
    campaign_id: Optional[int] = None,
) -> dict:
    expr = ATTRIBUTE_FIELDS.get(field)
    if expr is None:
        raise ValueError(
            f"Unknown attribute field: {field}. Valid: {', '.join(sorted(ATTRIBUTE_FIELDS))}"
        )
    campaign_filter = ""
    params: list = [workspace_id]
    if campaign_id is not None:
        campaign_filter = (
            " AND wl.lead_id IN (SELECT lead_id FROM campaign_leads WHERE campaign_id = ?)"
        )
        params.append(int(campaign_id))
    params.append(max(min_sample, 1))
    # Group case-insensitively ("Career Services Coordinator" == "career
    # services coordinator"); display the first variant in case-sensitive
    # order so the shown label is deterministic.
    rows = conn.execute(
        f"""SELECT MIN(TRIM({expr})) AS value,
               COUNT(*) AS leads,
               SUM(wl.email_sent_count > 0 OR wl.linkedin_sent_count > 0) AS contacted,
               SUM(wl.total_replies_count > 0) AS replied,
               SUM(lower(COALESCE(wl.current_status_sentiment, '')) = 'positive') AS positive
            FROM workspace_leads wl
            JOIN leads l ON l.id = wl.lead_id
            WHERE wl.workspace_id = ?
              AND {expr} IS NOT NULL AND TRIM({expr}) != ''{campaign_filter}
            GROUP BY lower(TRIM({expr}))
            HAVING contacted >= ?
            ORDER BY CAST(replied AS REAL) / contacted DESC, contacted DESC""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        contacted = r["contacted"] or 0
        replied = r["replied"] or 0
        positive = r["positive"] or 0
        out.append({
            "value": r["value"],
            "leads": r["leads"],
            "contacted": contacted,
            "replied": replied,
            "positive": positive,
            "reply_rate": round(replied / contacted, 4) if contacted else None,
            "positive_rate": round(positive / contacted, 4) if contacted else None,
        })
    return {"field": field, "min_sample": min_sample, "campaign_id": campaign_id, "rows": out}


def campaign_audit(
    conn: sqlite3.Connection,
    workspace_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    cte, since_params = _ws_events(since, until)
    metric_rows = conn.execute(
        cte + f"""
        SELECT campaign_id,
               SUM(event_type = 'email_sent') AS sent,
               SUM({reply_event_sql_condition()}) AS replies,
               SUM(event_type = 'meeting_booked') AS meetings,
               SUM(event_type IN {BOUNCE_EVENT_TYPES_SQL}) AS bounces
        FROM ws_events
        WHERE campaign_id IS NOT NULL
        GROUP BY campaign_id""",
        (workspace_id, *since_params),
    ).fetchall()
    metrics = {r["campaign_id"]: r for r in metric_rows}

    # Campaigns assigned to the workspace, plus any that actually produced
    # events here (assignment is nullable and routing rules are name-based).
    campaigns = conn.execute(
        """SELECT id, name, status FROM campaigns
           WHERE workspace_id = ? OR id IN ({ids})
           ORDER BY name""".format(
            ids=",".join(str(int(cid)) for cid in metrics) or "-1"
        ),
        (workspace_id,),
    ).fetchall()

    # Positives are counted once per lead on current_sentiment_since (current
    # state), scoped to the same date range as sent/replies/bounces -- the same
    # definition the daily matrix and Replies list use, so the three agree.
    pos_range, pos_params = _range_clause(since, until, "wl.current_sentiment_since")
    positive_rows = conn.execute(
        f"""SELECT cl.campaign_id, COUNT(DISTINCT cl.lead_id) AS positive
            FROM workspace_leads wl
            JOIN campaign_leads cl ON cl.lead_id = wl.lead_id
            WHERE wl.workspace_id = ?
              AND lower(wl.current_status_sentiment) = 'positive'
              AND wl.current_sentiment_since IS NOT NULL{pos_range}
            GROUP BY cl.campaign_id""",
        (workspace_id, *pos_params),
    ).fetchall()
    positives = {r["campaign_id"]: r["positive"] for r in positive_rows}

    out = []
    for c in campaigns:
        m = metrics.get(c["id"])
        sent = (m["sent"] if m else 0) or 0
        replies = (m["replies"] if m else 0) or 0
        bounces = (m["bounces"] if m else 0) or 0
        meetings = (m["meetings"] if m else 0) or 0
        positive = positives.get(c["id"], 0)
        out.append({
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "sent": sent,
            "replies": replies,
            "reply_rate": round(replies / sent, 4) if sent else None,
            "positive": positive,
            "positive_rate": round(positive / sent, 4) if sent else None,
            "meetings": meetings,
            "bounces": bounces,
            "bounce_rate": round(bounces / sent, 4) if sent else None,
        })
    # Hide campaigns with zero activity in the selected range — an all-zero row
    # is just noise (the campaign exists but did nothing in this window).
    out = [c for c in out
           if c["sent"] or c["replies"] or c["bounces"] or c["meetings"] or c["positive"]]
    out.sort(key=lambda c: c["sent"], reverse=True)
    return {"since": since, "campaigns": out}


_SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|aw)\s*:\s*)+", re.IGNORECASE)


def normalize_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", subject or "").strip()


def campaign_subjects(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: int,
    limit: int = 10,
) -> dict:
    cte, _ = _ws_events(None)
    rows = conn.execute(
        cte + f"""
        SELECT subject,
               SUM(event_type = 'email_sent') AS sends,
               SUM({reply_event_sql_condition()}) AS replies,
               MAX(CASE WHEN event_type = 'email_sent' THEN id END) AS sample_event_id
        FROM ws_events
        WHERE campaign_id = ? AND subject IS NOT NULL AND TRIM(subject) != ''
        GROUP BY subject""",
        (workspace_id, campaign_id),
    ).fetchall()
    merged: dict[str, dict] = {}
    for r in rows:
        key = normalize_subject(r["subject"])
        if not key:
            continue
        agg = merged.setdefault(
            key, {"subject": key, "sends": 0, "replies": 0, "sample_event_id": None})
        agg["sends"] += r["sends"] or 0
        agg["replies"] += r["replies"] or 0
        if r["sample_event_id"] and not agg["sample_event_id"]:
            agg["sample_event_id"] = r["sample_event_id"]
    subjects = sorted(merged.values(), key=lambda s: (s["sends"], s["replies"]), reverse=True)
    for s in subjects:
        s["reply_rate"] = round(s["replies"] / s["sends"], 4) if s["sends"] else None
    return {"campaign_id": campaign_id, "subjects": subjects[:limit]}


def campaign_replies(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """One row per lead that currently carries a sentiment, anchored on the day
    it entered that sentiment (current_sentiment_since) — NOT one row per reply
    event. A lead who replied five times appears once; filtering this list to a
    day and sentiment reproduces that day's sentiment column exactly, because
    both read the same materialized anchor. The lead's most recent reply (if
    any) supplies the subject/copy for context.
    """
    from workspace_routing import linkedin_display_url

    reply_cond = reply_event_sql_condition()
    # Range applies to the sentiment anchor so this list reconciles with the
    # daily sentiment columns rather than the raw reply timestamps.
    range_sql, range_params = _range_clause(since, until, "wl.current_sentiment_since")
    params: list = [workspace_id, *range_params]
    camp = ""
    if campaign_id is not None:
        camp = (" AND wl.lead_id IN (SELECT lead_id FROM campaign_leads"
                " WHERE campaign_id = ?)")
        params.append(int(campaign_id))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT wl.lead_id AS lead_id,
               wl.current_status_sentiment AS sentiment,
               wl.current_status_label AS status_label,
               wl.current_sentiment_since AS event_at,
               l.name AS lead_name, l.linkedin_url, l.linkedin_sales_nav_id,
               lr.id AS event_id, lr.subject AS subject, lr.campaign_id AS campaign_id,
               c.name AS campaign_name,
               (lr.id IS NOT NULL AND json_extract(lr.metadata_json, '$.body') IS NOT NULL)
                   AS has_body
        FROM workspace_leads wl
        JOIN leads l ON l.id = wl.lead_id
        LEFT JOIN events lr ON lr.id = (
            SELECT wle2.event_id
            FROM workspace_lead_events wle2 JOIN events e2 ON e2.id = wle2.event_id
            WHERE wle2.workspace_id = wl.workspace_id
              AND wle2.lead_id = wl.lead_id
              AND {reply_cond.replace('event_type', 'e2.event_type').replace('direction', 'e2.direction')}
            ORDER BY wle2.event_at DESC, wle2.id DESC LIMIT 1)
        LEFT JOIN campaigns c ON c.id = lr.campaign_id
        WHERE wl.workspace_id = ?
          AND wl.current_status_sentiment IS NOT NULL
          AND wl.current_sentiment_since IS NOT NULL{range_sql}{camp}
        ORDER BY wl.current_sentiment_since DESC
        LIMIT ?""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["linkedin_display_url"] = linkedin_display_url(
            d.pop("linkedin_url"), d.pop("linkedin_sales_nav_id"))
        out.append(d)
    return {"campaign_id": campaign_id, "replies": out}


# Columns the activity search matches against (Section G: search the whole
# range, not just the loaded page). company_domain is the linked company's
# authoritative domain, falling back to the lead's own email domain.
ACTIVITY_SEARCH_COLUMNS = (
    "l.name", "l.email", "l.email_domain", "l.linkedin_url", "l.company", "co.domain",
)


def activity_event_types(
    conn: sqlite3.Connection,
    workspace_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Distinct event types (with counts) in the workspace within range, so the
    Activity filter dropdown lists exactly what's there — not a guessed set."""
    range_sql, range_params = _range_clause(since, until, "wle.event_at")
    rows = conn.execute(
        f"""SELECT wle.event_type, COUNT(*) AS n
            FROM workspace_lead_events wle
            WHERE wle.workspace_id = ?{range_sql}
              AND wle.event_type IS NOT NULL AND wle.event_type != ''
            GROUP BY wle.event_type ORDER BY n DESC, wle.event_type""",
        (workspace_id, *range_params),
    ).fetchall()
    return {"event_types": [dict(r) for r in rows]}


def activity_feed(
    conn: sqlite3.Connection,
    workspace_id: str,
    limit: int = 50,
    before: Optional[str] = None,
    q: Optional[str] = None,
    event_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Workspace activity, searchable across the whole range (Section G). Text
    search hits lead name / email / email-domain / LinkedIn / company /
    company-domain; event_type filters exactly; since/until bound the window.
    Keyset-paginated on (event_at, id)."""
    where = ["wle.workspace_id = ?"]
    params: list = [workspace_id]
    range_sql, range_params = _range_clause(since, until, "wle.event_at")
    if range_sql:
        # _range_clause yields a leading " AND …"; strip it since we re-join.
        where.append(range_sql.strip().removeprefix("AND ").strip())
        params += range_params
    if event_type and event_type.strip():
        where.append("wle.event_type = ?")
        params.append(event_type.strip())
    if q and q.strip():
        term = f"%{q.strip()}%"
        where.append("(" + " OR ".join(f"{c} LIKE ?" for c in ACTIVITY_SEARCH_COLUMNS) + ")")
        params += [term] * len(ACTIVITY_SEARCH_COLUMNS)
    if before:
        # keyset cursor "event_at|wle.id" from a previous page's next_before
        event_at, _, row_id = before.partition("|")
        where.append("(wle.event_at < ? OR (wle.event_at = ? AND wle.id < ?))")
        params += [event_at, event_at, int(row_id or 0)]
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT wle.id AS cursor_id, wle.event_at, wle.event_type,
               e.direction, e.channel, e.subject, e.sender,
               l.id AS lead_id, l.name AS lead_name, l.company,
               COALESCE(co.domain, l.email_domain) AS company_domain,
               c.name AS campaign_name
            FROM workspace_lead_events wle
            LEFT JOIN events e ON e.id = wle.event_id
            LEFT JOIN leads l ON l.id = wle.lead_id
            LEFT JOIN companies co ON co.id = l.company_id
            LEFT JOIN campaigns c ON c.id = e.campaign_id
            WHERE {where_sql}
            ORDER BY wle.event_at DESC, wle.id DESC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    events = [dict(r) for r in rows]
    next_before = None
    if len(events) == limit and events:
        last = events[-1]
        next_before = f"{last['event_at']}|{last['cursor_id']}"
    for ev in events:
        ev.pop("cursor_id", None)
    return {"events": events, "next_before": next_before,
            "q": q, "event_type": event_type}


# Daily activity matrix: one column per event kind an AM cares about.
# Sentiment (normalized by the platform registry) stands in for the messy
# free-text status labels: positive ~= interested, negative ~= not interested.
# Event-derived daily columns only. The reply-sentiment columns are NOT summed
# from the event stream here -- they're materialized from
# workspace_leads.current_sentiment_since so each lead counts exactly once, on
# the day it entered its *current* sentiment run (see _sentiment_by_day).
_DAILY_COLUMNS_SQL = f"""
    SUM(event_type = 'email_sent') AS email_sent,
    SUM(direction = 'inbound' AND channel = 'email'
        AND {reply_event_sql_condition()}) AS email_received,
    SUM(event_type = 'linkedin_message' AND direction = 'outbound') AS dm_sent,
    SUM((event_type = 'linkedin_message' AND direction = 'inbound')
        OR event_type = 'linkedin_dm_reply') AS dm_received,
    SUM(event_type IN ('linkedin_connect_sent', 'linkedin_connect')) AS connects_sent,
    SUM(event_type = 'linkedin_connection_accepted') AS connects_accepted,
    SUM(event_type = 'meeting_booked') AS meetings
"""

# Sentiment is the single reply-classification axis (stage is a manual
# sales-outcome overlay now, not a reporting axis). These four are what the
# campaigns matrix, the Replies list, and the summary tile all count, each
# one-row-per-lead on date(current_sentiment_since).
SENTIMENT_KEYS = ("positive", "negative", "invalid", "autoreply")
DAILY_EVENT_KEYS = (
    "email_sent", "email_received", "dm_sent", "dm_received",
    "connects_sent", "connects_accepted", "meetings",
)
DAILY_COLUMN_KEYS = (
    "email_sent", "email_received", "dm_sent", "dm_received",
    "connects_sent", "connects_accepted",
    *SENTIMENT_KEYS, "meetings",
)


# A lead sits in exactly one sentiment at a time: its current_status_sentiment,
# entered at current_sentiment_since (materialized at ingest; a flip away and
# back resets the anchor to the latest entry). Every sentiment count on the
# dashboard -- the campaigns matrix, the Replies list, the summary tile -- reads
# these two columns, so they can't disagree: each is one row per lead, grouped
# on date(current_sentiment_since). No event-stream rescans, no double-counting
# a lead across days or campaigns.
_POSITIVE_COUNT_CACHE: dict = {}
_POSITIVE_COUNT_TTL = 15.0


def _sentiment_count_in_range(
    conn: sqlite3.Connection, workspace_id: str, sentiment: str,
    since: Optional[str], until: Optional[str],
) -> int:
    """Leads whose CURRENT sentiment is `sentiment` and whose run started in the
    range -- counted once each."""
    range_sql, params = _range_clause(since, until, "current_sentiment_since")
    return conn.execute(
        f"""SELECT COUNT(*) AS n FROM workspace_leads
            WHERE workspace_id = ?
              AND lower(current_status_sentiment) = lower(?)
              AND current_sentiment_since IS NOT NULL{range_sql}""",
        (workspace_id, sentiment, *params),
    ).fetchone()["n"]


def _positive_count_in_range(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str], until: Optional[str],
) -> int:
    # summary() is polled every few seconds; memoize this one count briefly.
    key = (workspace_id, since, until)
    now = time.time()
    hit = _POSITIVE_COUNT_CACHE.get(key)
    if hit and now - hit[0] < _POSITIVE_COUNT_TTL:
        return hit[1]
    n = _sentiment_count_in_range(conn, workspace_id, "positive", since, until)
    _POSITIVE_COUNT_CACHE[key] = (now, n)
    return n


def _sentiment_by_day(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict[str, dict[str, int]]:
    """{sentiment: {date: count}} of leads counted once on the day they entered
    their current sentiment run (current_sentiment_since). Optionally scoped to
    one campaign's leads and/or a date range."""
    range_sql, range_params = _range_clause(since, until, "wl.current_sentiment_since")
    camp = ""
    params: list = [workspace_id, *range_params]
    if campaign_id is not None:
        camp = (" AND wl.lead_id IN (SELECT lead_id FROM campaign_leads"
                " WHERE campaign_id = ?)")
        params.append(int(campaign_id))
    rows = conn.execute(
        f"""SELECT lower(wl.current_status_sentiment) AS sentiment,
                   date(wl.current_sentiment_since) AS day, COUNT(*) AS n
            FROM workspace_leads wl
            WHERE wl.workspace_id = ?
              AND wl.current_status_sentiment IS NOT NULL
              AND wl.current_sentiment_since IS NOT NULL{range_sql}{camp}
            GROUP BY sentiment, day""",
        params,
    ).fetchall()
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        if r["day"] and r["sentiment"]:
            out[r["sentiment"]][r["day"]] = r["n"]
    return out


def campaign_daily(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: Optional[int] = None,
    since: str = "30d",
    until: Optional[str] = None,
) -> dict:
    """Per-day event-type matrix for the workspace, optionally one campaign."""
    range_sql, range_params = _range_clause(since, until, "wle.event_at")
    campaign_sql = ""
    params: list = [workspace_id, *range_params]
    if campaign_id is not None:
        campaign_sql = " AND e.campaign_id = ?"
        params.append(int(campaign_id))
    # Own CTE (not ws_events) because the matrix also needs metadata_json,
    # and bare event_type/direction must be unambiguous for the SUM exprs.
    rows = conn.execute(
        f"""WITH day_events AS (
                SELECT e.event_type, e.direction, e.channel, e.metadata_json,
                       wle.event_at
                FROM workspace_lead_events wle
                JOIN events e ON e.id = wle.event_id
                WHERE wle.workspace_id = ?{range_sql}{campaign_sql}
            )
            SELECT date(event_at) AS day, {_DAILY_COLUMNS_SQL}
            FROM day_events
            GROUP BY day ORDER BY day""",
        params,
    ).fetchall()
    # Event columns come from the stream above; the four sentiment columns come
    # from the materialized current_sentiment_since (one row per lead, current
    # state), grouped on the same range. A lead can enter a sentiment on a day
    # with no campaign-attributed events, so union the day sets from both sides.
    event_by_day = {r["day"]: r for r in rows}
    sent_by_day = _sentiment_by_day(conn, workspace_id, campaign_id, since, until)
    all_days = sorted(
        set(event_by_day)
        | {d for per_day in sent_by_day.values() for d in per_day}
    )
    days = []
    totals = dict.fromkeys(DAILY_COLUMN_KEYS, 0)
    for day in all_days:
        er = event_by_day.get(day)
        row = {"date": day}
        for key in DAILY_EVENT_KEYS:
            row[key] = (er[key] or 0) if er else 0
        for s in SENTIMENT_KEYS:
            row[s] = sent_by_day.get(s, {}).get(day, 0)
        for key in DAILY_COLUMN_KEYS:
            totals[key] += row[key]
        days.append(row)
    return {
        "campaign_id": campaign_id, "since": since, "until": until,
        "columns": list(DAILY_COLUMN_KEYS), "days": days, "totals": totals,
    }


def campaign_detail(
    conn: sqlite3.Connection, workspace_id: str, campaign_id: int,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    """Click-through view for one campaign: senders, lead statuses, activity span."""
    campaign = conn.execute(
        "SELECT id, name, status, description, created_at FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    if campaign is None:
        raise ValueError(f"campaign not found: {campaign_id}")
    cte, range_params = _ws_events(since, until)
    senders = conn.execute(
        cte + """
        SELECT e2.sender, COUNT(*) AS events,
               SUM(ws_events.event_type = 'email_sent') AS sent,
               MAX(ws_events.event_at) AS last_activity_at
        FROM ws_events JOIN events e2 ON e2.id = ws_events.id
        WHERE ws_events.campaign_id = ? AND e2.sender IS NOT NULL AND e2.sender != ''
        GROUP BY e2.sender ORDER BY events DESC""",
        (workspace_id, *range_params, campaign_id),
    ).fetchall()
    span = conn.execute(
        cte + """
        SELECT MIN(event_at) AS first_activity_at, MAX(event_at) AS last_activity_at,
               COUNT(*) AS events, COUNT(DISTINCT lead_id) AS leads_touched
        FROM ws_events WHERE campaign_id = ?""",
        (workspace_id, *range_params, campaign_id),
    ).fetchone()
    statuses = conn.execute(
        """SELECT wl.status, COUNT(*) AS n
           FROM campaign_leads cl
           JOIN workspace_leads wl ON wl.lead_id = cl.lead_id AND wl.workspace_id = ?
           WHERE cl.campaign_id = ?
           GROUP BY wl.status""",
        (workspace_id, campaign_id),
    ).fetchall()
    status_counts = {r["status"]: r["n"] for r in statuses}
    return {
        "campaign": dict(campaign),
        "senders": [dict(r) for r in senders],
        "activity": dict(span) if span else {},
        "lead_statuses": [
            {"stage": s, "count": status_counts.pop(s, 0)} for s in PIPELINE_STAGES
        ] + [{"stage": s, "count": n} for s, n in sorted(status_counts.items())],
    }


def lead_history(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_id: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Full event timeline for one lead; bodies fetched separately via event_body."""
    lead = conn.execute(
        """SELECT l.id, l.name, l.company, l.company_id, l.title, l.email,
                  l.email_domain, l.last_contact_at,
                  l.linkedin_url, l.linkedin_sales_nav_id,
                  -- Per-workspace status is authoritative; fall back to the lead's
                  -- own stage when the panel is opened without a workspace.
                  COALESCE(wl.status, l.stage) AS stage,
                  wl.current_status_sentiment, wl.current_status_label,
                  -- companies is canonical for industry/headcount; the lead's own
                  -- columns are only a fallback for leads with no company link
                  -- (expand/contract: they'll be dropped once every reader is here).
                  COALESCE(NULLIF(TRIM(co.industry), ''), l.industry) AS industry,
                  COALESCE(NULLIF(TRIM(co.headcount), ''), l.headcount) AS headcount,
                  co.name AS linked_company_name, co.domain AS linked_company_domain,
                  co.industry AS company_industry, co.headcount AS company_headcount
           FROM leads l
           LEFT JOIN companies co ON co.id = l.company_id
           LEFT JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
           WHERE l.id = ?""",
        (workspace_id, lead_id),
    ).fetchone()
    if lead is None:
        raise ValueError(f"lead not found: {lead_id}")
    lead = dict(lead)
    # Best openable LinkedIn URL (public profile, else synthesized Sales Nav URL).
    from workspace_routing import linkedin_display_url
    lead["linkedin_display_url"] = linkedin_display_url(
        lead.get("linkedin_url"), lead.get("linkedin_sales_nav_id"))
    # Workspace tags for the lead panel's add/remove/filter chips.
    if workspace_id:
        lead["tags"] = [
            r["tag"] for r in conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? ORDER BY tag",
                (workspace_id, lead_id)).fetchall()
        ]
    else:
        lead["tags"] = []
    ws_sql, params = "", [lead_id]
    if workspace_id:
        ws_sql = (
            " AND e.id IN (SELECT event_id FROM workspace_lead_events"
            "              WHERE workspace_id = ? AND lead_id = ?)"
        )
        params += [workspace_id, lead_id]
    params.append(limit)
    events = conn.execute(
        f"""SELECT e.id, e.event_type, e.direction, e.channel, e.subject,
               e.body_preview, e.sender, e.created_at,
               c.name AS campaign_name,
               json_extract(e.metadata_json, '$.body') IS NOT NULL AS has_body,
               json_extract(e.metadata_json, '$.lead_status_display') AS status_display,
               json_extract(e.metadata_json, '$.lead_status_sentiment') AS status_sentiment
            FROM events e
            LEFT JOIN campaigns c ON c.id = e.campaign_id
            WHERE e.lead_id = ?{ws_sql}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?""",
        params,
    ).fetchall()
    return {
        "lead": lead,
        "events": [dict(e) for e in events],
    }


def lead_custom_fields(conn: sqlite3.Connection, lead_id: int) -> dict:
    """Lead + linked-company personalization (custom) fields for the lead panel."""
    import pipeline_personalize as pp

    row = conn.execute(
        "SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"lead not found: {lead_id}")
    lead_fields = pp._lead_personalization_dict(conn, lead_id)
    company_fields = {}
    company_id = row["company_id"]
    if company_id:
        company_fields = pp._company_personalization_dict(conn, company_id)
    return {
        "lead_id": lead_id,
        "company_id": company_id,
        "lead": [{"field": k, "value": v["field_value"]} for k, v in lead_fields.items()],
        "company": [{"field": k, "value": v["field_value"]} for k, v in company_fields.items()],
    }


def lead_provider_runs(conn: sqlite3.Connection, lead_id: int) -> dict:
    """Per-capability run status + the full provider-attempt log for a lead —
    powers the verification/run-log section and the re-run guards.

    Verification providers (millionverifier/scrubby) only write a submission
    *stamp* to lead_provider_attempts (status 'unknown') because MillionVerifier
    is async — the real valid/invalid result lands later in
    lead_email_verification. Reading only the stamp made the panel show every
    verification as 'unknown'. Overlay the real verification result here so the
    run log and the capability headline reflect what actually came back."""
    from pipeline_provider_attempts import (
        get_provider_attempts_for_lead, provider_run_summary,
    )

    summary = provider_run_summary(conn, lead_id)
    runs = get_provider_attempts_for_lead(conn, lead_id)

    # Latest real verification result per provider (source), newest first.
    verif_rows = conn.execute(
        """SELECT email, status, source, verified_at
           FROM lead_email_verification
           WHERE lead_id = ? AND status IS NOT NULL AND TRIM(status) != ''
           ORDER BY verified_at DESC""",
        (lead_id,),
    ).fetchall()
    latest_by_provider: dict[str, dict] = {}
    for r in verif_rows:
        p = (r["source"] or "").strip().lower()
        if p and p not in latest_by_provider:
            latest_by_provider[p] = dict(r)

    def _blank(v) -> bool:
        return not v or str(v).strip().lower() == "unknown"

    for run in runs:
        v = latest_by_provider.get((run.get("provider") or "").strip().lower())
        if v and _blank(run.get("status")):
            run["status"] = v["status"]
            run["result_email"] = run.get("result_email") or v["email"]
            run["result_validity"] = run.get("result_validity") or v["status"]
            run["completed_at"] = run.get("completed_at") or v["verified_at"]

    verif = summary.get("email_verification")
    if verif and verif.get("ran") and _blank(verif.get("last_status")):
        cands = [latest_by_provider[p] for p in verif.get("providers", [])
                 if p in latest_by_provider]
        if cands:
            best = max(cands, key=lambda x: x.get("verified_at") or "")
            verif["last_status"] = best["status"]
            verif["last_attempted_at"] = verif.get("last_attempted_at") or best.get("verified_at")

    return {"lead_id": lead_id, "summary": summary, "runs": runs}


def event_body(conn: sqlite3.Connection, event_id: int) -> dict:
    """Full stored copy of one event (body lives in metadata_json)."""
    row = conn.execute(
        """SELECT id, lead_id, event_type, direction, channel, subject,
                  body_preview, sender, created_at, metadata_json
           FROM events WHERE id = ?""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"event not found: {event_id}")
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    body = meta.pop("body", None)
    return {
        "id": row["id"],
        "lead_id": row["lead_id"],
        "event_type": row["event_type"],
        "direction": row["direction"],
        "channel": row["channel"],
        "subject": row["subject"],
        "sender": row["sender"],
        "created_at": row["created_at"],
        "body": body if body is not None else row["body_preview"],
        "body_is_full": body is not None,
        "body_was_html": bool(meta.get("body_was_html")),
        "body_truncated": bool(meta.get("body_truncated")),
        "metadata": meta,
    }


def crm_overview(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """CRM sync state: configs (sanitized), synced leads, eligible-unsynced, runs."""
    import crm_sync

    configs = [
        {
            "platform": cfg.get("platform"),
            "enabled": bool(cfg.get("enabled", 1)),
            "has_pipeline": bool(cfg.get("pipeline_id")),
            "overwrite_existing": bool(cfg.get("overwrite_existing")),
        }
        for cfg in crm_sync.read_crm_config(conn, workspace_id)
    ]

    synced = conn.execute(
        """SELECT m.lead_id, l.name, l.company, l.title, l.email,
                  wl.status, wl.current_status_sentiment, wl.last_activity_at,
                  m.platform, m.last_synced_at, m.last_sync_status, m.sync_error,
                  m.crm_contact_id IS NOT NULL AS has_contact,
                  m.crm_deal_id IS NOT NULL AS has_deal,
                  m.crm_note_id IS NOT NULL AS has_note,
                  wl.updated_at > m.last_synced_at AS stale
           FROM crm_entity_map m
           JOIN leads l ON l.id = m.lead_id
           LEFT JOIN workspace_leads wl
                  ON wl.lead_id = m.lead_id AND wl.workspace_id = m.workspace_id
           WHERE m.workspace_id = ?
           ORDER BY m.last_synced_at DESC""",
        (workspace_id,),
    ).fetchall()
    synced_ids = {r["lead_id"] for r in synced}

    eligible = crm_sync.select_leads(conn, workspace_id)
    pending = [
        {
            "lead_id": lead.get("lead_id"),
            "name": lead.get("name"),
            "company": lead.get("company"),
            "email": lead.get("email"),
            "status": lead.get("status"),
            "sentiment": lead.get("current_status_sentiment"),
            "last_activity_at": lead.get("last_activity_at"),
        }
        for lead in eligible if lead.get("lead_id") not in synced_ids
    ]

    runs = conn.execute(
        """SELECT platform, started_at, completed_at, status, leads_checked,
                  contacts_created, contacts_updated, events_pushed, skipped,
                  errors, error_details
           FROM crm_sync_log WHERE workspace_id = ?
           ORDER BY started_at DESC LIMIT 10""",
        (workspace_id,),
    ).fetchall()

    return {
        "configs": configs,
        "configured": bool(configs),
        "synced": [dict(r) for r in synced],
        "pending": pending,
        "counts": {
            "synced": len(synced),
            "stale": sum(1 for r in synced if r["stale"]),
            "pending": len(pending),
            "errors": sum(1 for r in synced if (r["last_sync_status"] or "") == "error"),
        },
        "recent_runs": [dict(r) for r in runs],
    }


def sync_outbox(
    conn: sqlite3.Connection,
    workspace_slug: Optional[str] = None,
    limit: int = 100,
    entity_type: Optional[str] = None,
    op: Optional[str] = None,
) -> dict:
    """What is queued to push back to the relay (the outbox), for auditing.

    `groups` is the complete overview (every entity_type/op, workspace-scoped).
    The row list is a LIMIT slice — optionally drilled into one entity_type/op —
    and reports both `matched` (rows the filter selects) and `showing` (rows
    returned) so the UI can flag truncation instead of implying it's the whole
    set. The rows list is ordered failures-first, then most-recently-dirty."""
    base_where, base_params = [], []
    if workspace_slug:
        base_where.append("workspace_slug = ?")
        base_params.append(workspace_slug)
    ws_clause = (" WHERE " + " AND ".join(base_where)) if base_where else ""
    groups = conn.execute(
        f"""SELECT entity_type, op, COUNT(*) AS n,
               MIN(dirty_at) AS oldest, MAX(dirty_at) AS newest,
               SUM(attempts > 0) AS with_attempts,
               SUM(last_error IS NOT NULL) AS with_errors
            FROM outbox{ws_clause}
            GROUP BY entity_type, op ORDER BY n DESC""",
        base_params,
    ).fetchall()

    row_where, row_params = list(base_where), list(base_params)
    if entity_type:
        row_where.append("entity_type = ?")
        row_params.append(entity_type)
    if op:
        row_where.append("op = ?")
        row_params.append(op)
    row_clause = (" WHERE " + " AND ".join(row_where)) if row_where else ""
    rows = conn.execute(
        f"""SELECT entity_type, entity_id, op, entity_key, workspace_slug,
               dirty_at, attempts, last_error
            FROM outbox{row_clause}
            ORDER BY last_error IS NULL, attempts DESC, dirty_at DESC
            LIMIT ?""",
        (*row_params, limit),
    ).fetchall()
    matched = conn.execute(
        f"SELECT COUNT(*) AS n FROM outbox{row_clause}", row_params
    ).fetchone()["n"]
    # Split by what Push actually drains. Every upsert pushes. Deletes only
    # push for entity types the relay has a delete action for (lead_core/
    # lead_workspace/company, via _push_outbox_delete -- merges are a
    # separate path onto the same relay actions, not the only one anymore).
    # sender_account/sender_domain deletes have no relay-side handler at all
    # and can never push by any mechanism -- those are the only ones still
    # truly local-only.
    from pipeline_workspace import _SNAPSHOT_DELETE_ACTIONS

    total = 0
    pushable_total = 0
    delete_total = 0
    for g in groups:
        total += g["n"]
        if g["op"] == "upsert" or g["entity_type"] in _SNAPSHOT_DELETE_ACTIONS:
            pushable_total += g["n"]
        else:
            delete_total += g["n"]
    return {
        "total": total,
        "pushable_total": pushable_total,
        "delete_total": delete_total,
        "matched": matched,
        "showing": len(rows),
        "limit": limit,
        "workspace_slug": workspace_slug,
        "entity_type": entity_type,
        "op": op,
        "groups": [dict(g) for g in groups],
        "rows": [dict(r) for r in rows],
    }


# Per-entity display record + payload builder. Each entry is
# (record_sql, id_kind, payload_fn(conn, ids, slug)). Record lookup and payload
# build run in separate try blocks so a display-column slip never suppresses the
# payload (the field that actually matters), and vice-versa.
_OUTBOX_RECORD_SQL = {
    "lead_core": ("""SELECT l.id, l.uid, l.name, l.company, l.title, l.email,
                            l.email_domain, l.linkedin_url, l.stage
                     FROM leads l WHERE l.id = ?""", "int"),
    "lead_workspace": ("""SELECT wl.lead_id, wl.workspace_id, wl.status,
                                 wl.current_status_label, wl.current_status_sentiment,
                                 wl.last_activity_at, l.name, l.company
                          FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
                          WHERE wl.lead_id = ? AND wl.workspace_id = ?""", "composite"),
    "company": ("""SELECT id, uid, name, domain, industry, headcount,
                          hq_city, hq_country FROM companies WHERE id = ?""", "int"),
    "sender_account": ("""SELECT id, email, email_domain, provider, status,
                                 first_name, last_name, warmup_status, daily_limit
                          FROM sender_accounts WHERE id = ?""", "int"),
    "sender_domain": ("""SELECT domain, reseller, domain_cost, currency,
                                sending_ip, notes FROM sender_domains WHERE domain = ?""", "str"),
}


def _resolve_outbox_entity(
    conn: sqlite3.Connection, entity_type: str, entity_id: str,
    workspace_slug: Optional[str],
) -> tuple[Optional[dict], dict, Optional[str]]:
    """(current_record, built_payload, payload_error) for one outbox entity.

    Rebuilds the exact snapshot the push loop would send, from the live record,
    using the same payload builders sync_all() uses — so the Sync detail panel
    shows precisely what will go out, not an approximation. Builder failures are
    captured (not raised) so a broken row still renders its metadata."""
    from workspace_routing import DEFAULT_ORG_ID as _ORG

    spec = _OUTBOX_RECORD_SQL.get(entity_type)
    if spec is None:
        return None, {}, f"unknown entity_type: {entity_type}"

    # Identifiers shared by both the record SELECT and the payload builder.
    lead_part, _, ws_part = str(entity_id).partition(":")
    slug = workspace_slug
    if entity_type == "lead_workspace" and not slug and ws_part:
        r = conn.execute("SELECT slug FROM workspaces WHERE id = ?", (ws_part,)).fetchone()
        slug = r["slug"] if r else None

    record: Optional[dict] = None
    sql, id_kind = spec
    try:
        if id_kind == "composite":
            record = conn.execute(sql, (int(lead_part), ws_part)).fetchone()
        elif id_kind == "int":
            record = conn.execute(sql, (int(entity_id),)).fetchone()
        else:
            record = conn.execute(sql, (entity_id,)).fetchone()
    except (sqlite3.Error, ValueError):
        record = None  # display-only; never blocks the payload below

    payload: dict = {}
    error: Optional[str] = None
    try:
        if entity_type == "lead_core":
            import lead_sync
            payload = lead_sync.build_lead_core_sync_payload(conn, _ORG, int(entity_id))
        elif entity_type == "lead_workspace":
            import lead_sync
            if not slug:
                raise ValueError("workspace not resolvable for this row")
            payload = lead_sync.build_lead_workspace_sync_payload(
                conn, _ORG, int(lead_part), workspace_slug=slug)
        elif entity_type == "company":
            import pipeline_personalize
            payload = pipeline_personalize.build_company_sync_payload(conn, int(entity_id))
        elif entity_type == "sender_account":
            import pipeline_sender_accounts
            payload = pipeline_sender_accounts.build_sender_account_sync_payload(
                conn, int(entity_id))
        elif entity_type == "sender_domain":
            import pipeline_sender_accounts
            payload = pipeline_sender_accounts.build_sender_domain_sync_payload(conn, entity_id)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return (dict(record) if record else None), (payload or {}), error


def outbox_item_detail(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    op: Optional[str] = None,
) -> dict:
    """Section H: one outbox row's full picture — its queue metadata (op, dirty
    time, attempts, last error), the resolved live record, and the payload that
    will push. `op` narrows to a specific queued op; without it, all ops for the
    entity are returned."""
    where = ["entity_type = ?", "entity_id = ?"]
    params: list = [entity_type, str(entity_id)]
    if op:
        where.append("op = ?")
        params.append(op)
    outbox_rows = conn.execute(
        f"""SELECT entity_type, entity_id, op, entity_key, workspace_slug,
                   dirty_at, attempts, last_error
            FROM outbox WHERE {' AND '.join(where)}
            ORDER BY dirty_at DESC""",
        params,
    ).fetchall()
    if not outbox_rows:
        raise ValueError(f"no outbox row for {entity_type} {entity_id}")
    ws_slug = next((r["workspace_slug"] for r in outbox_rows if r["workspace_slug"]), None)
    record, payload, error = _resolve_outbox_entity(
        conn, entity_type, str(entity_id), ws_slug)
    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "outbox": [dict(r) for r in outbox_rows],
        "record": record,
        "record_exists": record is not None,
        "payload": payload,
        "payload_error": error,
    }


# ---------------------------------------------------------------------------
# Server-side search (Contacts / campaign leads / Companies). Searches the whole
# workspace within the active range, not just a loaded page.
# ---------------------------------------------------------------------------

LEAD_SEARCH_COLUMNS = (
    "l.name", "l.email", "l.email_domain", "l.linkedin_url", "l.company", "l.title",
)
LEAD_SORTS = {
    "last_activity": "wl.last_activity_at IS NULL, wl.last_activity_at DESC, l.id DESC",
    "name": "l.name COLLATE NOCASE ASC",
    "company": "l.company COLLATE NOCASE ASC, l.name COLLATE NOCASE ASC",
    "recent": "l.id DESC",
}

# Column expressions the Contacts / Data-quality tables can sort on server-side
# (whole list, not just the loaded page). Direction comes from the header click;
# NULLs are forced last for both directions so empty cells never lead.
LEAD_SORT_COLUMNS = {
    "name": "l.name COLLATE NOCASE",
    "email": "l.email COLLATE NOCASE",
    "company": "l.company COLLATE NOCASE",
    "title": "l.title COLLATE NOCASE",
    "status": "wl.status COLLATE NOCASE",
    "current_status_sentiment": "wl.current_status_sentiment COLLATE NOCASE",
    "email_verification_status": "l.email_verification_status COLLATE NOCASE",
    "total_replies_count": "wl.total_replies_count",
    "email_sent_count": "wl.email_sent_count",
    "last_activity_at": "wl.last_activity_at",
}


def _lead_order_by(sort: Optional[str], direction: Optional[str]) -> str:
    """ORDER BY fragment for search_leads. A known column key uses the requested
    direction with NULLs last and a stable id tiebreaker; otherwise fall back to
    the legacy named sorts."""
    col = LEAD_SORT_COLUMNS.get(sort or "")
    if col:
        d = "DESC" if str(direction or "").lower() == "desc" else "ASC"
        return f"({col} IS NULL), {col} {d}, l.id DESC"
    return LEAD_SORTS.get(sort or "", LEAD_SORTS["last_activity"])


def _lead_columns() -> str:
    return """l.id AS lead_id, l.name, l.company, l.company_id, l.title, l.email,
             l.email_domain, l.linkedin_url, l.linkedin_sales_nav_id,
             l.industry, l.headcount,
             l.location_city, l.location_country, l.email_verification_status,
             wl.status, wl.current_status_label, wl.current_status_sentiment,
             wl.last_activity_at, wl.email_sent_count, wl.total_replies_count,
             (SELECT GROUP_CONCAT(t.tag, ',') FROM workspace_lead_tags t
              WHERE t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id) AS tags"""


# Provider "catch-all"/accept-all verification statuses, however the provider
# spelled it. Kept as one list so the stats counts and the filter agree.
_CATCH_ALL_STATUSES_SQL = "('catch_all', 'catchall', 'catch-all', 'accept_all', 'acceptall')"
# A lead worth spending finder credits on: no email, a real name, and no
# email-finding attempt already on record.
_QUALIFY_FINDING_SQL = (
    "(l.email IS NULL OR TRIM(l.email) = '')"
    " AND l.name IS NOT NULL AND TRIM(l.name) != '' AND LOWER(TRIM(l.name)) != 'unknown'"
    " AND NOT EXISTS (SELECT 1 FROM lead_provider_attempts a"
    "                 WHERE a.lead_id = l.id AND a.provider IN ('trykitt', 'icypeas'))"
)


def _contacts_stats_selects() -> str:
    """The count columns shared by the overall and per-tag stats rows.
    CASE-wrapped so a comparison against a NULL status column contributes 0, not
    NULL (which would make SUM return NULL when every row is unverified)."""
    return f"""
        COUNT(*) AS total,
        SUM(CASE WHEN LOWER(l.email_verification_status) = 'valid' THEN 1 ELSE 0 END) AS valid_email,
        SUM(CASE WHEN LOWER(l.email_verification_status) IN {_CATCH_ALL_STATUSES_SQL} THEN 1 ELSE 0 END) AS catch_all_email,
        SUM(CASE WHEN l.email IS NULL OR TRIM(l.email) = '' THEN 1 ELSE 0 END) AS no_email,
        SUM(CASE WHEN {_QUALIFY_FINDING_SQL} THEN 1 ELSE 0 END) AS qualify_finding,
        SUM(CASE WHEN (l.linkedin_url IS NOT NULL AND TRIM(l.linkedin_url) != '')
            OR (l.linkedin_sales_nav_id IS NOT NULL AND TRIM(l.linkedin_sales_nav_id) != '') THEN 1 ELSE 0 END) AS has_linkedin"""


def contacts_stats(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """Email/LinkedIn readiness breakdown for a workspace, overall and per tag.

    Each group here is click-to-filter in the UI (the filter keys line up with
    search_leads params: verify=valid|catch_all|none, qualify_finding, has_linkedin,
    tag)."""
    overall = conn.execute(
        f"""SELECT {_contacts_stats_selects()}
            FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
            WHERE wl.workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    by_tag = conn.execute(
        f"""SELECT t.tag AS tag, {_contacts_stats_selects()}
            FROM workspace_lead_tags t
            JOIN workspace_leads wl ON wl.workspace_id = t.workspace_id AND wl.lead_id = t.lead_id
            JOIN leads l ON l.id = wl.lead_id
            WHERE t.workspace_id = ?
            GROUP BY t.tag ORDER BY total DESC""",
        (workspace_id,),
    ).fetchall()
    return {"overall": dict(overall) if overall else {}, "by_tag": [dict(r) for r in by_tag]}


def search_leads(
    conn: sqlite3.Connection,
    workspace_id: str,
    q: Optional[str] = None,
    status: Optional[str] = None,
    campaign_id: Optional[int] = None,
    missing: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    sort: str = "last_activity",
    direction: Optional[str] = None,
    tag: Optional[str] = None,
    connected: Optional[bool] = None,
    sender: Optional[str] = None,
    has_linkedin: Optional[bool] = None,
    verify: Optional[str] = None,
    qualify_finding: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    ids_only: bool = False,
) -> dict:
    """Search workspace leads by any attribute. `missing` in
    {email,company,title,name,linkable} filters to under-enriched leads
    (Section D reuses this). `linkable` = has company text but no company_id.

    `tag` restricts to leads carrying that workspace tag; `connected` (with
    optional `sender`) restricts to 1st-degree LinkedIn connections; `has_linkedin`
    restricts to leads with a public URL or Sales Navigator id."""
    where = ["wl.workspace_id = ?"]
    params: list = [workspace_id]
    if tag:
        where.append(
            "wl.lead_id IN (SELECT lead_id FROM workspace_lead_tags"
            " WHERE workspace_id = ? AND tag = ?)")
        params += [workspace_id, tag]
    if connected:
        conn_sql = ("wl.lead_id IN (SELECT lead_id FROM workspace_lead_linkedin_status"
                    " WHERE workspace_id = ? AND is_connected = 1")
        params.append(workspace_id)
        if sender:
            conn_sql += " AND sender_profile = ?"
            params.append(sender)
        conn_sql += ")"
        where.append(conn_sql)
    if has_linkedin:
        where.append(
            "((l.linkedin_url IS NOT NULL AND TRIM(l.linkedin_url) != '')"
            " OR (l.linkedin_sales_nav_id IS NOT NULL AND TRIM(l.linkedin_sales_nav_id) != ''))")
    if verify == "valid":
        where.append("LOWER(l.email_verification_status) = 'valid'")
    elif verify == "catch_all":
        where.append(f"LOWER(l.email_verification_status) IN {_CATCH_ALL_STATUSES_SQL}")
    elif verify == "none":
        where.append("(l.email IS NULL OR TRIM(l.email) = '')")
    if qualify_finding:
        where.append(_QUALIFY_FINDING_SQL)
    if q and q.strip():
        term = f"%{q.strip()}%"
        where.append("(" + " OR ".join(f"{c} LIKE ?" for c in LEAD_SEARCH_COLUMNS) + ")")
        params += [term] * len(LEAD_SEARCH_COLUMNS)
    if status:
        where.append("wl.status = ?")
        params.append(status)
    if campaign_id is not None:
        where.append("wl.lead_id IN (SELECT lead_id FROM campaign_leads WHERE campaign_id = ?)")
        params.append(int(campaign_id))
    if missing == "email":
        where.append("(l.email IS NULL OR TRIM(l.email) = '')")
    elif missing == "company":
        where.append("l.company_id IS NULL")
    elif missing == "title":
        where.append("(l.title IS NULL OR TRIM(l.title) = '')")
    elif missing == "name":
        where.append("(l.name IS NULL OR TRIM(l.name) = '' OR lower(TRIM(l.name)) = 'unknown')")
    elif missing == "linkable":
        where.append("l.company_id IS NULL AND l.company IS NOT NULL AND TRIM(l.company) != ''")
    active_sql, active_params = _active_in_range(since, until)
    params += active_params
    where_sql = " AND ".join(where) + active_sql
    order = _lead_order_by(sort, direction)
    # ids_only powers the "select all N matching" bulk action: same WHERE, but
    # just the ids (capped by `limit`) so the client can select every match
    # across pages without paging through them. No column projection or sort.
    if ids_only:
        rows = conn.execute(
            f"""SELECT l.id FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
                WHERE {where_sql} LIMIT ?""",
            (*params, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        return {"lead_ids": ids, "count": len(ids), "capped": len(ids) >= limit}
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id"
        f" WHERE {where_sql}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT {_lead_columns()}
            FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
            WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return {
        "total": total, "limit": limit, "offset": offset,
        "q": q, "sort": sort, "leads": [dict(r) for r in rows],
    }


def campaign_leads(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: int,
    q: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_activity",
    direction: Optional[str] = None,
) -> dict:
    """Leads in a campaign (Section F: campaign detail is lead-centric)."""
    return search_leads(
        conn, workspace_id, q=q, campaign_id=campaign_id,
        since=since, until=until, sort=sort, direction=direction,
        limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Companies (Section C): search, detail with all domains/branches, merge review.
# ---------------------------------------------------------------------------

def search_companies(
    conn: sqlite3.Connection,
    workspace_id: str,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "leads",
) -> dict:
    """Companies that have at least one lead in the workspace."""
    where = ["wl.workspace_id = ?"]
    params: list = [workspace_id]
    if q and q.strip():
        term = f"%{q.strip()}%"
        where.append("(c.name LIKE ? OR c.domain LIKE ? OR c.industry LIKE ?)")
        params += [term, term, term]
    where_sql = " AND ".join(where)
    order = {
        "leads": "leads DESC, c.name COLLATE NOCASE",
        "name": "c.name COLLATE NOCASE ASC",
    }.get(sort, "leads DESC, c.name COLLATE NOCASE")
    rows = conn.execute(
        f"""SELECT c.id, c.name, c.domain, c.industry, c.headcount,
                   c.hq_city, c.hq_country,
                   COUNT(DISTINCT wl.lead_id) AS leads,
                   (SELECT COUNT(*) FROM company_identities ci
                    WHERE ci.company_id = c.id AND ci.identity_type = 'domain') AS domains
            FROM companies c
            JOIN leads l ON l.company_id = c.id
            JOIN workspace_leads wl ON wl.lead_id = l.id
            WHERE {where_sql}
            GROUP BY c.id
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        (*params, limit + 1, offset),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "companies": [dict(r) for r in rows],
        "limit": limit, "offset": offset, "has_more": has_more, "q": q,
    }


def company_detail(conn: sqlite3.Connection, workspace_id: str, company_id: int) -> dict:
    """One company: attributes, all domains/branches, public emails, and its
    leads in this workspace."""
    company = conn.execute(
        """SELECT id, name, domain, industry, headcount, headcount_numeric,
                  hq_city, hq_state, hq_country, uid, created_at, updated_at
           FROM companies WHERE id = ?""",
        (company_id,),
    ).fetchone()
    if company is None:
        raise ValueError(f"company not found: {company_id}")
    identities = conn.execute(
        """SELECT identity_type, identity_value_normalized AS value, role,
                  verified_mx, is_verified, source
           FROM company_identities WHERE company_id = ?
           ORDER BY identity_type, is_verified DESC, value""",
        (company_id,),
    ).fetchall()
    domains = [dict(r) for r in identities if r["identity_type"] == "domain"]
    public_emails = [dict(r) for r in identities if r["identity_type"] == "public_email"]
    leads = conn.execute(
        f"""SELECT {_lead_columns()}
            FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
            WHERE wl.workspace_id = ? AND l.company_id = ?
            ORDER BY wl.last_activity_at IS NULL, wl.last_activity_at DESC
            LIMIT 200""",
        (workspace_id, company_id),
    ).fetchall()
    # Any open merge candidate that names this company (either side).
    pending_merges = conn.execute(
        """SELECT COUNT(*) AS n FROM company_merge_candidates
           WHERE status = 'pending'
             AND (existing_company_id = ? OR candidate_company_id = ?)""",
        (company_id, company_id),
    ).fetchone()["n"]
    return {
        "company": dict(company),
        "domains": domains,
        "public_emails": public_emails,
        "leads": [dict(r) for r in leads],
        "pending_merges": pending_merges,
        "lead_count": len(leads),
    }


def company_contact_activity(
    conn: sqlite3.Connection, workspace_id: str, company_id: int, limit: int = 200,
) -> dict:
    """One row per contact at this company that has at least one event, with its
    event count, latest event, and current sentiment — the roll-up for the
    company pane's aggregated history. The full per-contact timeline is fetched
    lazily via lead_history when a row is expanded, so this stays cheap."""
    rows = conn.execute(
        """SELECT l.id AS lead_id, l.name, l.title, l.email,
                  wl.current_status_sentiment AS sentiment,
                  wl.current_status_label AS status_label,
                  COUNT(DISTINCT wle.event_id) AS event_count,
                  MAX(wle.event_at) AS last_event_at,
                  (SELECT e2.event_type
                     FROM workspace_lead_events w2 JOIN events e2 ON e2.id = w2.event_id
                    WHERE w2.workspace_id = wl.workspace_id AND w2.lead_id = l.id
                    ORDER BY w2.event_at DESC, w2.id DESC LIMIT 1) AS last_event_type,
                  (SELECT e2.direction
                     FROM workspace_lead_events w2 JOIN events e2 ON e2.id = w2.event_id
                    WHERE w2.workspace_id = wl.workspace_id AND w2.lead_id = l.id
                    ORDER BY w2.event_at DESC, w2.id DESC LIMIT 1) AS last_event_direction
           FROM leads l
           JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
           JOIN workspace_lead_events wle ON wle.lead_id = l.id AND wle.workspace_id = ?
           WHERE l.company_id = ?
           GROUP BY l.id
           ORDER BY last_event_at DESC
           LIMIT ?""",
        (workspace_id, workspace_id, company_id, limit),
    ).fetchall()
    return {
        "company_id": company_id,
        "contacts": [dict(r) for r in rows],
    }


def company_search_for_link(conn: sqlite3.Connection, q: str, limit: int = 10) -> dict:
    """Lightweight company autocomplete for the 'link lead → company' control."""
    term = f"%{(q or '').strip()}%"
    rows = conn.execute(
        """SELECT id, name, domain, industry,
                  (SELECT COUNT(*) FROM leads l WHERE l.company_id = c.id) AS leads
           FROM companies c
           WHERE c.name LIKE ? OR c.domain LIKE ?
           ORDER BY leads DESC, c.name COLLATE NOCASE LIMIT ?""",
        (term, term, limit),
    ).fetchall()
    return {"companies": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Data quality + enrichment (Section D). Buckets/counts for the under-enriched
# leads, plus domain resolution so the email-finder is company/multi-domain
# aware, plus the truly-empty junk count (org-wide, event-less).
# ---------------------------------------------------------------------------

# The buckets shown in the data-quality view. Each is a WHERE fragment over the
# leads row `l` (workspace-joined). Kept as one map so the summary counts and
# the drill-down (search_leads missing=…) use the same definitions.
DATA_QUALITY_BUCKETS = {
    "missing_email": "l.email IS NULL OR TRIM(l.email) = ''",
    "missing_company": "l.company_id IS NULL",
    "missing_title": "l.title IS NULL OR TRIM(l.title) = ''",
    "unknown_name": "l.name IS NULL OR TRIM(l.name) = '' OR lower(TRIM(l.name)) = 'unknown'",
    "linkable": "l.company_id IS NULL AND l.company IS NOT NULL AND TRIM(l.company) != ''",
}

# search_leads `missing` value that lists each bucket (linkable maps 1:1).
BUCKET_TO_MISSING = {
    "missing_email": "email",
    "missing_company": "company",
    "missing_title": "title",
    "unknown_name": "name",
    "linkable": "linkable",
}


def data_quality(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    """Under-enriched-lead buckets with counts. Honors the active-in-range
    semantic like every other current-state tab. The junk (truly-empty,
    event-less) count is org-wide and range-agnostic by nature."""
    active_sql, active_params = _active_in_range(since, until)
    selects = ",\n".join(
        f"SUM({expr}) AS {name}" for name, expr in DATA_QUALITY_BUCKETS.items()
    )
    row = conn.execute(
        f"""SELECT COUNT(*) AS total,
                   {selects}
            FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
            WHERE wl.workspace_id = ?{active_sql}""",
        (workspace_id, *active_params),
    ).fetchone()
    buckets = [
        {"key": name, "missing": BUCKET_TO_MISSING[name], "count": row[name] or 0}
        for name in DATA_QUALITY_BUCKETS
    ]

    # Org-wide truly-empty junk (name='unknown', no email/linkedin, no child
    # rows). cleanup_junk_leads owns the exact predicate; ask it for the count.
    import junk_cleanup

    junk = junk_cleanup.cleanup_junk_leads(conn, dry_run=True)
    return {
        "since": since, "until": until,
        "range_active": bool(active_sql),
        "total": row["total"] or 0,
        "buckets": buckets,
        "junk_deletable": junk["selected"],
    }


def _lead_domains(conn: sqlite3.Connection, lead_id: int) -> tuple[list[str], str]:
    """Candidate sending domains for a lead's email-finder run, most-trusted
    first, plus the lead's own email_domain. Company domains come from
    company_identities (verified first); a company can have several, so the
    caller decides whether to auto-pick the first or prompt."""
    lead = conn.execute(
        "SELECT company_id, email_domain, company FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    domains: list[str] = []
    company_text = ""
    if lead:
        company_text = (lead["company"] or "").strip()
        if lead["company_id"]:
            rows = conn.execute(
                """SELECT identity_value_normalized AS d
                   FROM company_identities
                   WHERE company_id = ? AND identity_type = 'domain'
                   ORDER BY (role = 'primary') DESC, is_verified DESC, verified_mx DESC,
                            identity_value_normalized""",
                (lead["company_id"],),
            ).fetchall()
            domains = [r["d"] for r in rows if r["d"]]
        own = (lead["email_domain"] or "").strip().lower()
        if own and own not in domains:
            domains.append(own)
    return domains, company_text


def enrichment_targets(
    conn: sqlite3.Connection, workspace_id: str, lead_ids: list[int],
) -> dict:
    """Resolve, per lead, the domain(s) the email-finder would use. Surfaces
    multi-domain (needs a choice) and no-domain (can't run) leads so the UI can
    warn before spending provider credits."""
    targets = []
    for lead_id in lead_ids:
        lead = conn.execute(
            """SELECT l.id, l.name, l.company, l.company_id, l.email
               FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
               WHERE wl.workspace_id = ? AND l.id = ?""",
            (workspace_id, int(lead_id)),
        ).fetchone()
        if lead is None:
            continue
        domains, _ = _lead_domains(conn, int(lead_id))
        targets.append({
            "lead_id": lead["id"],
            "name": lead["name"],
            "company": lead["company"],
            "has_email": bool((lead["email"] or "").strip()),
            "domains": domains,
            "chosen_domain": domains[0] if domains else None,
            "multi_domain": len(domains) > 1,
            "no_domain": not domains,
        })
    return {"targets": targets}
