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

from constants import COMPANY_DOMAIN_SQL, PIPELINE_STAGES, SCHEDULING_PLATFORMS_SQL_LIST
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
    since: str = "30d", until: Optional[str] = None,
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
            # The raw counts travel alongside the rates so a totals row can be
            # weighted (sum/sum). Averaging per-domain rates would let a domain
            # with three sends move the number as much as one with three thousand.
            "replies": agg["replies"],
            "bounces": agg["bounces"],
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


# Attribution priority for "which campaign is this lead from", lowest wins:
#   0  this lead replied in it        2  a colleague replied in it
#   1  this lead was sent it          3  a colleague was sent it
#
# The rule is a ranking, but it must NOT be written as one correlated subquery
# per column. That version read beautifully and took 9.5 minutes on the largest
# real workspace: the `lead_id = l.id OR colleague` disjunction defeats
# idx_ws_events_lead, so each of the five projected columns degenerated into a
# scan of every event in the workspace plus a temp-B-tree sort -- 5 x 200 rows
# of it. EXPLAIN QUERY PLAN showed five CORRELATED SCALAR SUBQUERYs, each
# "SEARCH wlc USING INDEX idx_ws_events_lead (workspace_id=?)" with no lead_id.
#
# Resolved in two indexed passes instead, batched across the whole page:
#   1. every candidate lead's own campaign-bearing events, keyed on
#      (workspace_id, lead_id) so the index is fully used;
#   2. for leads that came back empty AND have a company, the same query over
#      that company's other leads -- one query for all of them, not per row.
# Same answer, same ordering, ~4 orders of magnitude faster.
CAMPAIGN_FIELDS = (
    "campaign_id", "campaign_name", "campaign_source",
    "campaign_via_lead_id", "campaign_via_lead_name",
)

_EMPTY_CAMPAIGN = dict.fromkeys(CAMPAIGN_FIELDS)


def _campaign_events_sql() -> str:
    """Campaign-bearing events for a set of leads in one workspace.

    `is_reply` rides along so the ranking happens where the rows are compared,
    rather than as a second SQL condition that could drift from the first.

    Scheduling-platform events are excluded. Calendly and friends send the
    booked event *type* with the webhook, and ingest stores it as a campaign --
    which is how "30 Minute Meeting" ended up being reported as the campaign a
    lead came from. It is a calendar slot, not outbound. Dropping it here fixes
    the replies table, the company pane and lead_campaign() together, because
    all three read this one query.
    """
    reply = (reply_event_sql_condition()
             .replace("event_type", "ec.event_type")
             .replace("direction", "ec.direction"))
    return f"""
        SELECT wlc.lead_id AS lead_id, lc.name AS lead_name, lc.company_id AS company_id,
               cc.id AS campaign_id, cc.name AS campaign_name,
               CASE WHEN {reply} THEN 1 ELSE 0 END AS is_reply,
               wlc.event_at AS event_at, wlc.id AS row_id
          FROM workspace_lead_events wlc
          JOIN events ec ON ec.id = wlc.event_id
          JOIN campaigns cc ON cc.id = ec.campaign_id
          JOIN leads lc ON lc.id = wlc.lead_id
         WHERE wlc.workspace_id = ?
           AND ec.campaign_id IS NOT NULL
           AND LOWER(COALESCE(json_extract(ec.metadata_json, '$.platform'), ''))
               NOT IN ({SCHEDULING_PLATFORMS_SQL_LIST})
           AND wlc.lead_id IN ({{placeholders}})"""


def _best(rows: list) -> Optional[sqlite3.Row]:
    """Replies outrank sends; within a rank, the most recent wins.

    All three keys sort descending together (is_reply 1 before 0, later before
    earlier, higher row id before lower), which is the whole ranking -- the same
    ORDER BY the SQL version used, just applied where the rows already are.
    """
    if not rows:
        return None
    return max(rows, key=lambda r: (r["is_reply"], r["event_at"] or "", r["row_id"] or 0))


def resolve_last_known_campaigns(
    conn: sqlite3.Connection, workspace_id: str, leads: list,
) -> dict[int, dict]:
    """{lead_id: campaign fields} for a page of leads.

    `leads` is any sequence of rows/mappings carrying `lead_id` and `company_id`.

    Why a fallback chain at all: the lead's *latest* event frequently carries no
    campaign_id (a bounce, an unsubscribe, an auto-reply), so reading the
    campaign off it shows "—" for leads that plainly came from a campaign.
    Taking the most recent event that *does* carry one covers every such event
    type without naming any of them.

    Why the company level: a reply from someone we never emailed is still
    attributable -- a colleague's campaign is what put the company in play. That
    is a weaker claim, so it ranks below the lead's own, and the caller is told
    which it got (`campaign_source`) and through whom.
    """
    out: dict[int, dict] = {}
    wanted = [(int(r["lead_id"]), r["company_id"]) for r in leads]
    if not wanted:
        return out
    for lead_id, _ in wanted:
        out[lead_id] = dict(_EMPTY_CAMPAIGN)

    # -- pass 1: the lead's own campaigns (fully indexed) --------------------
    lead_ids = [lid for lid, _ in wanted]
    own: dict[int, list] = {}
    sql = _campaign_events_sql().format(placeholders=",".join("?" * len(lead_ids)))
    for row in conn.execute(sql, (workspace_id, *lead_ids)).fetchall():
        own.setdefault(row["lead_id"], []).append(row)

    unresolved: dict[int, list[int]] = {}
    for lead_id, company_id in wanted:
        best = _best(own.get(lead_id, []))
        if best is not None:
            out[lead_id] = {
                "campaign_id": best["campaign_id"],
                "campaign_name": best["campaign_name"],
                "campaign_source": "self_reply" if best["is_reply"] else "self_send",
                # Null for a lead's own campaign, so the UI only renders the
                # "via …" chip when there genuinely is a via.
                "campaign_via_lead_id": None,
                "campaign_via_lead_name": None,
            }
        elif company_id is not None:
            unresolved.setdefault(int(company_id), []).append(lead_id)

    if not unresolved:
        return out

    # -- pass 2: colleagues at the same company ------------------------------
    # One query for every company still unresolved, not one per lead.
    company_ids = list(unresolved)
    colleagues = conn.execute(
        f"""SELECT id, company_id FROM leads
             WHERE company_id IN ({','.join('?' * len(company_ids))})""",
        company_ids,
    ).fetchall()
    colleague_ids = [r["id"] for r in colleagues]
    if not colleague_ids:
        return out

    by_company: dict[int, list] = {}
    sql = _campaign_events_sql().format(placeholders=",".join("?" * len(colleague_ids)))
    for row in conn.execute(sql, (workspace_id, *colleague_ids)).fetchall():
        if row["company_id"] is not None:
            by_company.setdefault(int(row["company_id"]), []).append(row)

    for company_id, lead_ids_here in unresolved.items():
        for lead_id in lead_ids_here:
            # A lead's own events already lost in pass 1 (it had none), but
            # exclude it anyway so "via" can never name the lead itself.
            best = _best([r for r in by_company.get(company_id, [])
                          if r["lead_id"] != lead_id])
            if best is None:
                continue
            out[lead_id] = {
                "campaign_id": best["campaign_id"],
                "campaign_name": best["campaign_name"],
                "campaign_source": "company_reply" if best["is_reply"] else "company_send",
                "campaign_via_lead_id": best["lead_id"],
                "campaign_via_lead_name": best["lead_name"],
            }
    return out


def attach_last_known_campaigns(
    conn: sqlite3.Connection, workspace_id: str, rows: list[dict],
) -> list[dict]:
    """Merge the resolved campaign fields into a list of result dicts."""
    resolved = resolve_last_known_campaigns(conn, workspace_id, rows)
    for row in rows:
        row.update(resolved.get(int(row["lead_id"]), _EMPTY_CAMPAIGN))
    return rows


def lead_campaign(
    conn: sqlite3.Connection, lead_id: int, workspace_id: Optional[str] = None,
) -> dict:
    """The last known campaign for one lead — the CLI/agent view of the column
    the replies table and the company pane both show.

    Without a workspace this answers per workspace the lead belongs to, because
    "which campaign" has a different answer in each and picking one silently
    would be a guess.
    """
    ws_sql, params = "", [lead_id]
    if workspace_id:
        ws_sql = " AND wl.workspace_id = ?"
        params.append(workspace_id)
    rows = conn.execute(
        f"""SELECT wl.workspace_id, w.slug AS workspace,
                   wl.lead_id AS lead_id, l.company_id AS company_id
              FROM workspace_leads wl
              JOIN leads l ON l.id = wl.lead_id
              LEFT JOIN workspaces w ON w.id = wl.workspace_id
             WHERE wl.lead_id = ?{ws_sql}""",
        params,
    ).fetchall()
    # Resolved per workspace: the same lead can come from a different campaign
    # in each, and collapsing that to one answer would be a guess.
    out = []
    for row in rows:
        entry = dict(row)
        entry.update(
            resolve_last_known_campaigns(conn, row["workspace_id"], [row])
            .get(int(row["lead_id"]), _EMPTY_CAMPAIGN))
        out.append(entry)
    return {"lead_id": lead_id, "workspaces": out}


def campaign_replies(
    conn: sqlite3.Connection,
    workspace_id: str,
    campaign_id: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 200,
    sentiment: Optional[str] = None,
    status_label: Optional[str] = None,
) -> dict:
    """One row per lead that currently carries a sentiment, anchored on the day
    it entered that sentiment (current_sentiment_since) — NOT one row per reply
    event. A lead who replied five times appears once; filtering this list to a
    day and sentiment reproduces that day's sentiment column exactly, because
    both read the same materialized anchor. The lead's most recent reply (if
    any) supplies the subject/copy for context.

    `sentiment` / `status_label` narrow the list. The returned `facets` are
    computed BEFORE those two apply, so each dropdown always offers every value
    available for the current campaign and date selection — picking "positive"
    must not empty the status-label dropdown of everything except the labels
    that happen to be positive. Both come back in one round trip; a separate
    facets endpoint would race the list against its own filter options.

    The campaign column is `last_known_campaign_sql`, not the latest reply's
    own campaign_id: a lead whose sentiment came from a bounce has no campaign
    on that event, and showing "—" for it was wrong rather than unknown.
    """
    reply_cond = reply_event_sql_condition()
    # Range applies to the sentiment anchor so this list reconciles with the
    # daily sentiment columns rather than the raw reply timestamps.
    range_sql, range_params = _range_clause(since, until, "wl.current_sentiment_since")
    base_params: list = [workspace_id, *range_params]
    camp = ""
    if campaign_id is not None:
        camp = (" AND wl.lead_id IN (SELECT lead_id FROM campaign_leads"
                " WHERE campaign_id = ?)")
        base_params.append(int(campaign_id))

    base_where = f"""wl.workspace_id = ?
          AND wl.current_status_sentiment IS NOT NULL
          AND wl.current_sentiment_since IS NOT NULL{range_sql}{camp}"""

    facets = {
        key: [
            dict(r) for r in conn.execute(
                f"""SELECT {col} AS value, COUNT(*) AS n
                      FROM workspace_leads wl
                     WHERE {base_where} AND {col} IS NOT NULL AND TRIM({col}) != ''
                     GROUP BY {col} ORDER BY n DESC, {col}""",
                base_params,
            ).fetchall()
        ]
        for key, col in (
            ("sentiment", "wl.current_status_sentiment"),
            ("status_label", "wl.current_status_label"),
        )
    }

    params = list(base_params)
    narrow = ""
    if sentiment:
        narrow += " AND LOWER(wl.current_status_sentiment) = LOWER(?)"
        params.append(sentiment)
    if status_label:
        narrow += " AND LOWER(wl.current_status_label) = LOWER(?)"
        params.append(status_label)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT wl.lead_id AS lead_id,
               wl.current_status_sentiment AS sentiment,
               wl.current_status_label AS status_label,
               wl.current_sentiment_since AS event_at,
               wl.last_activity_at AS last_activity_at,
               l.name AS lead_name, l.company_id AS company_id,
               l.company AS company, l.title AS title, l.email AS email,
               l.linkedin_url, l.linkedin_sales_nav_id,
               (SELECT GROUP_CONCAT(t.tag, ',') FROM workspace_lead_tags t
                WHERE t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id) AS tags,
               lr.id AS event_id, lr.subject AS subject,
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
        WHERE {base_where}{narrow}
        ORDER BY wl.current_sentiment_since DESC
        LIMIT ?""",
        params,
    ).fetchall()
    # Attribution is resolved for the whole page at once, after the list is
    # known -- doing it inline per row is what made this query take minutes.
    out = attach_last_known_campaigns(conn, workspace_id, [dict(r) for r in rows])
    return {
        "campaign_id": campaign_id,
        "sentiment": sentiment,
        "status_label": status_label,
        "facets": facets,
        "replies": out,
    }


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
    # Matches what the LinkedIn column renders (public profile URL, else the
    # Sales Navigator token). Sorting the stored value rather than the
    # synthesized URL groups public profiles and Sales Nav tokens into two
    # blocks instead of interleaving them -- the more useful read of the two.
    "linkedin": ("COALESCE(NULLIF(TRIM(l.linkedin_url), ''), l.linkedin_sales_nav_id)"
                 " COLLATE NOCASE"),
    "company": "l.company COLLATE NOCASE",
    "title": "l.title COLLATE NOCASE",
    "status": "wl.status COLLATE NOCASE",
    "current_status_label": "wl.current_status_label COLLATE NOCASE",
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
             l.record_type, l.superseded_at,
             l.email_domain, l.linkedin_url, l.linkedin_sales_nav_id,
             l.industry, l.headcount,
             l.location_city, l.location_country, l.email_verification_status,
             wl.status, wl.current_status_label, wl.current_status_sentiment,
             wl.contact_priority,
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
    # A company placeholder's "name" is the company. Searching for an email for
    # it burns finder credits looking for a person who does not exist.
    " AND l.record_type = 'contact'"
    # Belt and braces: the same structural test detect_company_placeholder uses,
    # applied to rows that are still labelled `contact`. Classification is a
    # judgement made once at import and it will keep missing cases -- 30 MBUSA
    # dealership stubs slipped through as contacts and TryKitt was spent on
    # them. A row whose name IS its company, with no email, no title and no
    # LinkedIn, is not a person to look up whatever its record_type says.
    " AND NOT (LOWER(TRIM(COALESCE(l.name, ''))) = LOWER(TRIM(COALESCE(l.company, '')))"
    "          AND (l.title IS NULL OR TRIM(l.title) = '')"
    "          AND (l.linkedin_url IS NULL OR TRIM(l.linkedin_url) = '')"
    "          AND (l.linkedin_sales_nav_id IS NULL OR TRIM(l.linkedin_sales_nav_id) = ''))"
    " AND NOT EXISTS (SELECT 1 FROM lead_provider_attempts a"
    "                 WHERE a.lead_id = l.id AND a.provider IN ('trykitt', 'icypeas'))"
)


_HAS_LINKEDIN_SQL = (
    "((l.linkedin_url IS NOT NULL AND TRIM(l.linkedin_url) != '')"
    " OR (l.linkedin_sales_nav_id IS NOT NULL AND TRIM(l.linkedin_sales_nav_id) != ''))"
)
_HAS_DOMAIN_SQL = (
    f"(({COMPANY_DOMAIN_SQL.rsplit(' AS company_domain', 1)[0]}) IS NOT NULL"
    f" AND TRIM(({COMPANY_DOMAIN_SQL.rsplit(' AS company_domain', 1)[0]})) != '')"
)
# Verified but not valid and not catch-all: invalid, bounced, disposable,
# unknown. Grouped as one "risky" bucket rather than five tiles nobody reads.
_RISKY_SQL = (
    "(l.email IS NOT NULL AND TRIM(l.email) != ''"
    " AND l.email_verification_status IS NOT NULL"
    " AND LOWER(l.email_verification_status) != 'valid'"
    f" AND LOWER(l.email_verification_status) NOT IN {_CATCH_ALL_STATUSES_SQL})"
)


def _contacts_stats_selects() -> str:
    """The count columns shared by the overall and per-tag stats rows.
    CASE-wrapped so a comparison against a NULL status column contributes 0, not
    NULL (which would make SUM return NULL when every row is unverified)."""
    return f"""
        COUNT(*) AS total,
        SUM(CASE WHEN l.record_type = 'contact' THEN 1 ELSE 0 END) AS people,
        SUM(CASE WHEN l.record_type = 'company_placeholder' THEN 1 ELSE 0 END) AS companies,
        SUM(CASE WHEN l.record_type = 'public_email' THEN 1 ELSE 0 END) AS shared_mailboxes,
        SUM(CASE WHEN LOWER(l.email_verification_status) = 'valid' THEN 1 ELSE 0 END) AS valid_email,
        SUM(CASE WHEN LOWER(l.email_verification_status) IN {_CATCH_ALL_STATUSES_SQL} THEN 1 ELSE 0 END) AS catch_all_email,
        SUM(CASE WHEN {_RISKY_SQL} THEN 1 ELSE 0 END) AS risky_email,
        SUM(CASE WHEN l.email IS NULL OR TRIM(l.email) = '' THEN 1 ELSE 0 END) AS no_email,
        SUM(CASE WHEN {_QUALIFY_FINDING_SQL} THEN 1 ELSE 0 END) AS qualify_finding,
        SUM(CASE WHEN {_HAS_DOMAIN_SQL} THEN 1 ELSE 0 END) AS has_domain,
        SUM(CASE WHEN {_HAS_LINKEDIN_SQL} THEN 1 ELSE 0 END) AS has_linkedin"""


def contacts_stats(conn: sqlite3.Connection, workspace_id: str, **filters) -> dict:
    """Email/LinkedIn readiness breakdown for a workspace, overall and per tag.

    Built from `lead_filter_clause()` -- the same WHERE the contacts list and
    the export use. It previously ran its own `WHERE wl.workspace_id = ?` with
    no record_type predicate, while the list below it defaulted to
    record_type='contact'. So the tiles counted 3,730 company placeholders and
    30 shared mailboxes that the list did not, and clicking a tile returned a
    different number than the tile showed. There is no second WHERE here now.

    The record-type counts are the one exception: they are computed with
    record_type forced to "all", because a tile whose job is to say how many
    company placeholders exist cannot be filtered down to people first.

    Each group is click-to-filter in the UI, and the filter keys line up with
    search_leads params (verify, qualify_finding, has_linkedin, record_type,
    tag).
    """
    filters = {k: v for k, v in filters.items() if k in LEAD_FILTER_KEYS}
    where_sql, params = lead_filter_clause(workspace_id, **filters)
    base = """FROM workspace_leads wl
              JOIN leads l ON l.id = wl.lead_id
              LEFT JOIN companies co ON co.id = l.company_id"""
    overall = conn.execute(
        f"SELECT {_contacts_stats_selects()} {base} WHERE {where_sql}", params,
    ).fetchone()

    # Record-type inventory, deliberately outside the record_type filter.
    rt_filters = {**filters, "record_type": "all"}
    rt_where, rt_params = lead_filter_clause(workspace_id, **rt_filters)
    totals = conn.execute(
        f"""SELECT COUNT(*) AS all_records,
                   SUM(CASE WHEN l.record_type = 'contact' THEN 1 ELSE 0 END) AS people,
                   SUM(CASE WHEN l.record_type = 'company_placeholder' THEN 1 ELSE 0 END) AS companies,
                   SUM(CASE WHEN l.record_type = 'public_email' THEN 1 ELSE 0 END) AS shared_mailboxes
              {base} WHERE {rt_where}""",
        rt_params,
    ).fetchone()
    # Suppressed is counted with suppression itself switched off, or the tile
    # meant to say how many are hidden would always read 0.
    sup_where, sup_params = lead_filter_clause(
        workspace_id, **{**rt_filters, "suppressed": "only"})
    suppressed_total = conn.execute(
        f"SELECT COUNT(*) AS n {base} WHERE {sup_where}", sup_params,
    ).fetchone()["n"]

    tag_where, tag_params = lead_filter_clause(workspace_id, **filters)
    by_tag = conn.execute(
        f"""SELECT t.tag AS tag, {_contacts_stats_selects()}
            FROM workspace_lead_tags t
            JOIN workspace_leads wl ON wl.workspace_id = t.workspace_id AND wl.lead_id = t.lead_id
            JOIN leads l ON l.id = wl.lead_id
            LEFT JOIN companies co ON co.id = l.company_id
            WHERE t.workspace_id = ? AND {tag_where}
            GROUP BY t.tag ORDER BY total DESC""",
        [workspace_id, *tag_params],
    ).fetchall()
    return {
        "overall": dict(overall) if overall else {},
        "record_types": {**(dict(totals) if totals else {}),
                         "suppressed": suppressed_total},
        "filters": filters,
        "by_tag": [dict(r) for r in by_tag],
    }


# Every filter the contacts list understands, in one place. Named so the export
# can take exactly the same kwargs and mean exactly the same thing -- "export
# what's on screen right now" is only true if both sides build the same WHERE.
def lead_message_block_sql(direction: str) -> str:
    """Correlated subqueries for the last message a lead sent or received:
    (at, subject, body) as three SELECT expressions.

    Mirrors the pattern campaign_replies already uses for its latest-reply
    join. `outbound` matches on direction; `inbound` uses the canonical reply
    condition, so "received" here means the same thing it means on the Replies
    tab rather than a second definition. Bodies come out of
    json_extract(metadata_json,'$.body'), same as event_body().
    """
    if direction == "inbound":
        cond = (reply_event_sql_condition()
                .replace("event_type", "em.event_type")
                .replace("direction", "em.direction"))
        prefix = "last_message_received"
    else:
        cond = "LOWER(em.direction) = 'outbound'"
        prefix = "last_message_sent"
    pick = f"""(SELECT {{col}} FROM workspace_lead_events wlem
                  JOIN events em ON em.id = wlem.event_id
                 WHERE wlem.workspace_id = wl.workspace_id
                   AND wlem.lead_id = wl.lead_id AND {cond}
                 ORDER BY wlem.event_at DESC, wlem.id DESC LIMIT 1)"""
    body_expr = "json_extract(em.metadata_json, '$.body')"
    return ", ".join(
        f"{pick.format(col=col)} AS {prefix}_{suffix}"
        for col, suffix in (
            ("em.created_at", "at"),
            ("em.subject", "subject"),
            (body_expr, "body"),
        )
    )


def _parse_personalized_filters(raw) -> list[tuple[str, str]]:
    """`["icp_segment=mercedes franchise"]` or `[("icp_segment", "...")]` -> pairs.

    Values may contain "="; only the first one separates field from value.
    """
    out: list[tuple[str, str]] = []
    for item in (raw or []):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            field, value = item
        else:
            text = str(item or "")
            if "=" not in text:
                raise ValueError(
                    f"--personalized expects FIELD=VALUE, got {text!r}")
            field, value = text.split("=", 1)
        field = str(field).strip().lower()
        if not field:
            raise ValueError("--personalized needs a field name before '='")
        out.append((field, str(value).strip()))
    return out


LEAD_FILTER_KEYS = (
    "q", "status", "campaign_id", "missing", "since", "until", "tag",
    "tags_any", "tags_all", "tags_none",
    "connected", "sender", "has_linkedin", "verify", "qualify_finding",
    "record_type", "lead_ids", "suppressed", "has_domain", "test", "personalized",
)


def _tag_list(value) -> list[str]:
    """Normalize a tag filter argument to a list of canonical tags.

    Accepts a bare string (the legacy scalar `tag`), a list, or None. Tags are
    stored normalized, so the filter has to normalize too or "NACE" and " nace "
    silently match nothing.
    """
    if value is None:
        return []
    from pipeline_utils import normalize_tag

    raw = [value] if isinstance(value, str) else list(value)
    out, seen = [], set()
    for item in raw:
        tag = normalize_tag(str(item or ""))
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out

# An explicit id list is capped for the same reason the "select all matching"
# fetch is: a caller that hands over the world should get an error, not a query
# with 200k bind parameters.
MAX_EXPLICIT_LEAD_IDS = 50_000


def lead_filter_clause(
    workspace_id: str,
    q: Optional[str] = None,
    status: Optional[str] = None,
    campaign_id: Optional[int] = None,
    missing: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tag: Optional[str] = None,
    connected: Optional[bool] = None,
    sender: Optional[str] = None,
    has_linkedin: Optional[bool] = None,
    verify: Optional[str] = None,
    qualify_finding: Optional[bool] = None,
    record_type: Optional[str] = None,
    lead_ids: Optional[list] = None,
    tags_any: Optional[list] = None,
    tags_all: Optional[list] = None,
    tags_none: Optional[list] = None,
    suppressed: Optional[str] = None,
    has_domain: Optional[bool] = None,
    test: Optional[str] = None,
    personalized: Optional[list] = None,
) -> tuple[str, list]:
    """The shared WHERE for anything selecting workspace leads: (sql, params).

    Assumes the aliases `wl` (workspace_leads) and `l` (leads) are in scope.
    Extracted from search_leads so lead_export.py can reuse it verbatim rather
    than growing a second, quietly divergent filter set -- which is what
    pipeline_tags.export_leads had become.

    `lead_ids` narrows to an explicit set -- what the operator ticked, rather
    than what the filters happen to match. It composes with the other filters
    (and with the record_type default), so an export of a hand-picked selection
    goes through this one clause set like everything else.
    """
    where = ["wl.workspace_id = ?"]
    params: list = [workspace_id]
    if lead_ids is not None:
        ids = [int(x) for x in lead_ids]
        if not ids:
            # An empty explicit selection selects nothing. Falling through to
            # "no id filter" would silently export the whole filtered list,
            # which is the bug this parameter exists to fix.
            return "0", []
        if len(ids) > MAX_EXPLICIT_LEAD_IDS:
            raise ValueError(
                f"too many lead_ids ({len(ids)}); cap is {MAX_EXPLICIT_LEAD_IDS}")
        where.append(f"wl.lead_id IN ({','.join('?' * len(ids))})")
        params += ids
    # Company placeholders are accounts, not people. They are excluded unless
    # asked for by name, so the contacts list, its counts, and every bulk action
    # driven off this query keep meaning "people you can actually contact".
    if record_type == "all":
        pass
    elif record_type:
        where.append("l.record_type = ?")
        params.append(record_type)
    else:
        where.append("l.record_type = 'contact'")
    # Suppression, enforced here and nowhere else. Because the list, the stat
    # counts, the CSV export and every bulk action all build their WHERE from
    # this function, one clause covers all four -- and defaulting to "exclude"
    # is the whole point: an operator who forgets the filter must get the safe
    # answer. `suppressed="all"` is the deliberate opt-out, and the only caller
    # that legitimately needs it is CRM/relay reconciliation, which has to see
    # everything to detect drift.
    # Test rows, same discipline as suppression: absent means exclude, so an
    # operator who forgets the filter gets the safe answer. Synthetic leads
    # tagged like real ones inflate every count and can be emailed.
    # Personalization as a first-class filter, so "give me segment X as its own
    # file" is a WHERE rather than a post-export pass in Python. Lead and
    # company scope are both reachable; the registry says which table a field
    # lives in, so the caller names the field, not the table.
    for field_name, wanted in _parse_personalized_filters(personalized):
        from pipeline_personalize import resolve_scope

        scope, _decided = resolve_scope(field_name)
        if scope == "company":
            where.append(
                "EXISTS (SELECT 1 FROM company_personalization cp"
                "         WHERE cp.company_id = l.company_id"
                "           AND cp.field_name = ? AND cp.field_value = ?)")
        else:
            where.append(
                "EXISTS (SELECT 1 FROM lead_personalization lp"
                "         WHERE lp.lead_id = l.id"
                "           AND lp.field_name = ? AND lp.field_value = ?)")
        params += [field_name, wanted]
    if test == "only":
        where.append("l.is_test = 1")
    elif test != "all":
        where.append("COALESCE(l.is_test, 0) = 0")
    if suppressed == "only":
        where.append("EXISTS (SELECT 1 FROM workspace_lead_suppressions s"
                     " WHERE s.workspace_id = wl.workspace_id AND s.lead_id = wl.lead_id)")
    elif suppressed != "all":
        where.append("NOT EXISTS (SELECT 1 FROM workspace_lead_suppressions s"
                     " WHERE s.workspace_id = wl.workspace_id AND s.lead_id = wl.lead_id)")
    # Tags: any / all / none, composable. `tag` is the legacy scalar and folds
    # into tags_any -- it is still what the stats drill-through, the CLI and
    # saved agent invocations pass, so it keeps working rather than becoming a
    # breaking rename across the whole surface.
    any_tags = _tag_list(tags_any) + _tag_list(tag)
    all_tags = _tag_list(tags_all)
    none_tags = _tag_list(tags_none)
    if any_tags:
        where.append(
            "wl.lead_id IN (SELECT lead_id FROM workspace_lead_tags"
            f" WHERE workspace_id = ? AND tag IN ({','.join('?' * len(any_tags))}))")
        params += [workspace_id, *any_tags]
    if all_tags:
        # COUNT(DISTINCT) rather than one EXISTS per tag: a lead carrying the
        # same tag twice cannot fake its way past the count, and the query
        # stays one subquery wide however many tags are selected.
        where.append(
            "(SELECT COUNT(DISTINCT tag) FROM workspace_lead_tags"
            f" WHERE workspace_id = ? AND lead_id = wl.lead_id"
            f" AND tag IN ({','.join('?' * len(all_tags))})) = ?")
        params += [workspace_id, *all_tags, len(all_tags)]
    if none_tags:
        where.append(
            "wl.lead_id NOT IN (SELECT lead_id FROM workspace_lead_tags"
            f" WHERE workspace_id = ? AND tag IN ({','.join('?' * len(none_tags))}))")
        params += [workspace_id, *none_tags]
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
    elif verify == "risky":
        # Verified, and the verdict was neither valid nor catch-all: invalid,
        # bounced, disposable, unknown. One bucket, because five separate tiles
        # for the same decision ("don't send") is five tiles nobody reads.
        where.append(_RISKY_SQL)
    elif verify == "none":
        where.append("(l.email IS NULL OR TRIM(l.email) = '')")
    if has_domain:
        where.append(_HAS_DOMAIN_SQL)
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
    return " AND ".join(where) + active_sql, params


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
    record_type: Optional[str] = None,
    tags_any: Optional[list] = None,
    tags_all: Optional[list] = None,
    tags_none: Optional[list] = None,
    suppressed: Optional[str] = None,
    has_domain: Optional[bool] = None,
    test: Optional[str] = None,
    personalized: Optional[list] = None,
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
    where_sql, params = lead_filter_clause(
        workspace_id, q=q, status=status, campaign_id=campaign_id, missing=missing,
        since=since, until=until, tag=tag, connected=connected, sender=sender,
        has_linkedin=has_linkedin, verify=verify, qualify_finding=qualify_finding,
        record_type=record_type, suppressed=suppressed, has_domain=has_domain,
        test=test, personalized=personalized,
        tags_any=tags_any, tags_all=tags_all, tags_none=tags_none)
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

# A contact you can actually email. Shared by search_companies' filter and
# companies_stats' tiles so "companies with no reachable contact" means the same
# number in both places -- the click-through from a tile has to land on the rows
# the tile counted.
_REACHABLE_CONTACT_SQL = """(
    l2.record_type = 'contact'
    AND l2.email IS NOT NULL AND TRIM(l2.email) != ''
    AND (l2.email_verification_status IS NULL
         OR LOWER(l2.email_verification_status) NOT IN ('invalid', 'bounced', 'do_not_contact'))
)"""


# Text-search predicate over a company's NON-primary domains. Shared by
# search_companies and company_search_for_link so "search finds it" means the
# same thing in the Companies list and in the link-lead-to-company picker.
_COMPANY_IDENTITY_MATCH_SQL = """EXISTS (
    SELECT 1 FROM company_identities ci
    WHERE ci.company_id = c.id AND ci.identity_type = 'domain'
      AND ci.identity_value_normalized LIKE ?
)"""


def _company_tag_exists_sql() -> str:
    """A company carries tag T if ANY of its leads in the workspace carries T.

    Derived, not stored: a company_tags table would need a sync surface and would
    drift from the lead tags that are the actual source of truth. A
    company_placeholder lead is itself taggable, so company-level-only tags still
    work -- you tag the placeholder.

    EXISTS rather than a join: search_companies already joins leads +
    workspace_leads and runs a correlated identity count, and a fourth join
    multiplies rows before the GROUP BY.
    """
    return """EXISTS (
        SELECT 1 FROM workspace_lead_tags t
        JOIN leads lt ON lt.id = t.lead_id
        WHERE t.workspace_id = wl.workspace_id AND lt.company_id = c.id AND t.tag = ?
    )"""


# Columns the companies list can be sorted by across the WHOLE result set, not
# just the loaded page. Keys match the table's column keys in dashboard.html.
COMPANY_SORT_COLUMNS = {
    "name": "c.name COLLATE NOCASE",
    "domain": "c.domain COLLATE NOCASE",
    "domains": "domains",
    "industry": "c.industry COLLATE NOCASE",
    "headcount": "c.headcount COLLATE NOCASE",
    "leads": "leads",
}


def _company_order_by(sort: Optional[str], direction: Optional[str]) -> str:
    """ORDER BY for search_companies.

    Blank and NULL sort together and always last, in both directions. Sorting
    by primary domain is a way of working through the companies that *have*
    one; a descending sort that led with 900 empty cells would answer a
    different question than the one being asked.
    """
    col = COMPANY_SORT_COLUMNS.get(sort or "")
    if not col:
        return "leads DESC, c.name COLLATE NOCASE"
    d = "DESC" if str(direction or "").lower() == "desc" else "ASC"
    return f"({col} IS NULL OR {col} = ''), {col} {d}, c.id DESC"


def search_companies(
    conn: sqlite3.Connection,
    workspace_id: str,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "leads",
    direction: Optional[str] = None,
    tag: Optional[str] = None,
    missing_domain: Optional[bool] = None,
    no_reachable_contact: Optional[bool] = None,
    placeholder_only: Optional[bool] = None,
) -> dict:
    """Companies that have at least one lead in the workspace.

    The three boolean filters are the click-throughs for the corresponding stat
    tiles, so each one must select exactly the population its tile counted.
    """
    where = ["wl.workspace_id = ?"]
    params: list = [workspace_id]
    if q and q.strip():
        term = f"%{q.strip()}%"
        # Searching c.domain alone only hits the *primary* domain, so a company
        # found via any of its other known domains (brand portfolio, a branch
        # site, the old domain before a rebrand) looked missing. The EXISTS is
        # index-backed by idx_company_identities_value.
        where.append(f"(c.name LIKE ? OR c.domain LIKE ? OR c.industry LIKE ?"
                     f" OR {_COMPANY_IDENTITY_MATCH_SQL})")
        params += [term, term, term, term]
    if tag:
        where.append(_company_tag_exists_sql())
        params.append(tag)
    if missing_domain:
        where.append("(c.domain IS NULL OR TRIM(c.domain) = '')")
    if no_reachable_contact:
        where.append(f"""NOT EXISTS (
            SELECT 1 FROM leads l2
            JOIN workspace_leads wl2 ON wl2.lead_id = l2.id
            WHERE l2.company_id = c.id AND wl2.workspace_id = wl.workspace_id
              AND {_REACHABLE_CONTACT_SQL})""")
    if placeholder_only:
        where.append("""NOT EXISTS (
            SELECT 1 FROM leads l3
            JOIN workspace_leads wl3 ON wl3.lead_id = l3.id
            WHERE l3.company_id = c.id AND wl3.workspace_id = wl.workspace_id
              AND l3.record_type = 'contact')""")
    where_sql = " AND ".join(where)
    order = _company_order_by(sort, direction)
    # When a row matched on an alternate domain rather than its primary, say
    # which one -- otherwise a hit on "markquartmenomonie.com" under a company
    # whose primary reads "markquart.com" looks like a search bug.
    if q and q.strip():
        matched_sql = """(SELECT ci.identity_value_normalized FROM company_identities ci
                          WHERE ci.company_id = c.id AND ci.identity_type = 'domain'
                            AND ci.identity_value_normalized LIKE ?
                          LIMIT 1) AS matched_domain"""
        matched_params = [f"%{q.strip()}%"]
    else:
        matched_sql = "NULL AS matched_domain"
        matched_params = []
    rows = conn.execute(
        f"""SELECT c.id, c.name, c.domain, c.industry, c.headcount,
                   c.hq_city, c.hq_country,
                   {matched_sql},
                   COUNT(DISTINCT wl.lead_id) AS leads,
                   (SELECT COUNT(*) FROM company_identities ci
                    WHERE ci.company_id = c.id AND ci.identity_type = 'domain') AS domains,
                   (SELECT GROUP_CONCAT(DISTINCT t.tag) FROM workspace_lead_tags t
                    JOIN leads lt ON lt.id = t.lead_id
                    WHERE t.workspace_id = wl.workspace_id AND lt.company_id = c.id) AS tags
            FROM companies c
            JOIN leads l ON l.company_id = c.id
            JOIN workspace_leads wl ON wl.lead_id = l.id
            WHERE {where_sql}
            GROUP BY c.id
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        # matched_params bind inside the SELECT list, which SQLite numbers
        # BEFORE the WHERE clause -- they have to lead.
        (*matched_params, *params, limit + 1, offset),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = sorted({t for t in (d.get("tags") or "").split(",") if t})
        # Only interesting when it isn't already the primary shown in the row.
        if d.get("matched_domain") == d.get("domain"):
            d["matched_domain"] = None
        out.append(d)
    return {
        "companies": out,
        "limit": limit, "offset": offset, "has_more": has_more, "q": q, "tag": tag,
        "sort": sort, "dir": direction,
    }


def _companies_stats_selects() -> str:
    """The count columns shared by the overall and per-tag company stats.

    Contacts stats measure *reachability* per person. Companies need *coverage
    and penetration* instead: how many accounts you can reach at all, how deep
    you are inside each, and which ones are blocked on missing data. Each tile
    lines up with a search_companies filter so it is click-through.
    """
    reach = f"""EXISTS (SELECT 1 FROM leads l2
                  JOIN workspace_leads wl2 ON wl2.lead_id = l2.id
                 WHERE l2.company_id = c.id AND wl2.workspace_id = ?
                   AND {_REACHABLE_CONTACT_SQL})"""
    real_contact = """EXISTS (SELECT 1 FROM leads l3
                        JOIN workspace_leads wl3 ON wl3.lead_id = l3.id
                       WHERE l3.company_id = c.id AND wl3.workspace_id = ?
                         AND l3.record_type = 'contact')"""
    # workspace_lead_events carries no direction of its own -- it indexes into
    # events, which does. "Replied" uses the canonical reply condition rather
    # than bare inbound so it agrees with the Replies tab and the daily columns.
    reply_cond = (reply_event_sql_condition()
                  .replace("event_type", "e4.event_type")
                  .replace("direction", "e4.direction"))
    contacted = """EXISTS (SELECT 1 FROM workspace_lead_events wle
                     JOIN leads l4 ON l4.id = wle.lead_id
                     JOIN events e4 ON e4.id = wle.event_id
                    WHERE l4.company_id = c.id AND wle.workspace_id = ?
                      AND LOWER(e4.direction) = 'outbound')"""
    replied = f"""EXISTS (SELECT 1 FROM workspace_lead_events wle
                    JOIN leads l4 ON l4.id = wle.lead_id
                    JOIN events e4 ON e4.id = wle.event_id
                   WHERE l4.company_id = c.id AND wle.workspace_id = ?
                     AND {reply_cond})"""
    positive = """EXISTS (SELECT 1 FROM workspace_leads wl5
                    JOIN leads l5 ON l5.id = wl5.lead_id
                   WHERE l5.company_id = c.id AND wl5.workspace_id = ?
                     AND LOWER(wl5.current_status_sentiment) = 'positive')"""
    return f"""
        COUNT(DISTINCT c.id) AS companies,
        SUM(CASE WHEN {reach} THEN 1 ELSE 0 END) AS with_reachable_contact,
        SUM(CASE WHEN {reach} THEN 0 ELSE 1 END) AS no_reachable_contact,
        SUM(CASE WHEN {real_contact} THEN 0 ELSE 1 END) AS placeholder_only,
        SUM(CASE WHEN {contacted} THEN 1 ELSE 0 END) AS contacted,
        SUM(CASE WHEN {replied} THEN 1 ELSE 0 END) AS replied,
        SUM(CASE WHEN {positive} THEN 1 ELSE 0 END) AS positive,
        SUM(CASE WHEN c.domain IS NULL OR TRIM(c.domain) = '' THEN 1 ELSE 0 END) AS missing_domain,
        SUM(CASE WHEN c.industry IS NULL OR TRIM(c.industry) = '' THEN 1 ELSE 0 END) AS missing_industry,
        SUM(CASE WHEN c.headcount IS NULL OR TRIM(c.headcount) = '' THEN 1 ELSE 0 END) AS missing_headcount,
        SUM(lead_count) AS contact_rows"""


# One workspace_id bind per correlated EXISTS in _companies_stats_selects(), in
# the order they appear in the SELECT list: reach ×2 (with/without),
# real_contact, contacted, replied, positive. These bind BEFORE the FROM
# clause's own workspace_id -- SQLite numbers `?` by position in the statement
# text, and the SELECT list is written first.
_COMPANIES_STATS_BINDS = 6


def companies_stats(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """Account coverage and penetration for a workspace, overall and per tag.

    The inner subquery collapses to one row per company FIRST; the aggregates
    then count companies, never lead rows. Doing it the other way -- aggregating
    over the lead join -- weights every tile by how many contacts a company
    happens to have, which reads plausibly and is wrong.
    """
    base = """FROM (
        SELECT c2.id AS id, COUNT(DISTINCT wl.lead_id) AS lead_count
          FROM companies c2
          JOIN leads l ON l.company_id = c2.id
          JOIN workspace_leads wl ON wl.lead_id = l.id
         WHERE wl.workspace_id = ?
         GROUP BY c2.id
    ) g JOIN companies c ON c.id = g.id"""
    selects = _companies_stats_selects()
    binds = [workspace_id] * _COMPANIES_STATS_BINDS
    overall = conn.execute(
        f"SELECT {selects} {base}", (*binds, workspace_id),
    ).fetchone()
    result = dict(overall) if overall else {}
    total = result.get("companies") or 0
    result["avg_contacts_per_company"] = (
        round((result.get("contact_rows") or 0) / total, 1) if total else 0.0
    )

    # Per-tag uses the same derived rule as the tag filter: a company is in tag
    # T if any of its workspace leads carries T.
    tag_base = """FROM (
        SELECT c2.id AS id, t.tag AS tag, COUNT(DISTINCT wl.lead_id) AS lead_count
          FROM companies c2
          JOIN leads l ON l.company_id = c2.id
          JOIN workspace_leads wl ON wl.lead_id = l.id
          JOIN workspace_lead_tags t
            ON t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id
         WHERE wl.workspace_id = ?
         GROUP BY c2.id, t.tag
    ) g JOIN companies c ON c.id = g.id"""
    by_tag = conn.execute(
        f"SELECT g.tag AS tag, {selects} {tag_base} GROUP BY g.tag ORDER BY companies DESC",
        (*binds, workspace_id),
    ).fetchall()
    return {"overall": result, "by_tag": [dict(r) for r in by_tag]}


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
                  verified_mx, is_verified, source, label, purpose
           FROM company_identities WHERE company_id = ?
           ORDER BY identity_type, is_verified DESC, value""",
        (company_id,),
    ).fetchall()
    domains = [dict(r) for r in identities if r["identity_type"] == "domain"]
    public_emails = [dict(r) for r in identities if r["identity_type"] == "public_email"]
    # companies.domain is the canonical identity but predates company_identities,
    # so a company can have one without a matching identity row. Surface it as
    # the primary row rather than leaving the pane's one domain table quietly
    # missing the most important entry.
    if company["domain"] and not any(
        (d["value"] or "").lower() == (company["domain"] or "").lower() for d in domains
    ):
        domains.insert(0, {
            "identity_type": "domain", "value": company["domain"], "role": None,
            "verified_mx": None, "is_verified": 0, "source": "companies.domain",
            "label": None, "purpose": "primary",
        })
    for d in domains:
        d["is_primary"] = (d["value"] or "").lower() == (company["domain"] or "").lower()
        if d["is_primary"] and not d.get("purpose"):
            d["purpose"] = "primary"
    leads = conn.execute(
        f"""SELECT {_lead_columns()}
            FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
            WHERE wl.workspace_id = ? AND l.company_id = ?
            ORDER BY wl.last_activity_at IS NULL, wl.last_activity_at DESC
            LIMIT 200""",
        (workspace_id, company_id),
    ).fetchall()
    # Any open merge candidate that names this company (either side). The rows
    # come back, not just the count: "2 pending candidates — go find them on
    # another card" is a notification, and a notification about a decision you
    # are already looking at the evidence for is worse than useless.
    pending = conn.execute(
        """SELECT * FROM company_merge_candidates
           WHERE status = 'pending'
             AND (existing_company_id = ? OR candidate_company_id = ?)
           ORDER BY received_at DESC LIMIT 20""",
        (company_id, company_id),
    ).fetchall()
    import pipeline as _pipeline

    candidates = []
    for row in pending:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["payload"] = None
        d["confidence"] = (d["payload"] or {}).get("confidence")
        candidates.append(_pipeline._flatten_merge_candidate(conn, d))
    return {
        "company": dict(company),
        "domains": domains,
        "public_emails": public_emails,
        "leads": [dict(r) for r in leads],
        "pending_merges": len(candidates),
        "pending_merge_candidates": candidates,
        "lead_count": len(leads),
    }


def company_contact_activity(
    conn: sqlite3.Connection, workspace_id: str, company_id: int, limit: int = 200,
) -> dict:
    """One row per contact at this company that has at least one event, with its
    event count, latest event, and current sentiment — the roll-up for the
    company pane's aggregated history. The full per-contact timeline is fetched
    lazily via lead_history when a row is expanded, so this stays cheap.

    Carries the same from/to the contact replies view shows (`events.sender` is
    the mailbox; the lead's own address is the other end, and which is which
    depends on direction), and the same last-known campaign as the replies
    table — one definition, so the two panes cannot disagree about which
    campaign a contact came from.
    """
    rows = conn.execute(
        f"""SELECT l.id AS lead_id, l.name, l.title, l.email,
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
                    ORDER BY w2.event_at DESC, w2.id DESC LIMIT 1) AS last_event_direction,
                  (SELECT e2.sender
                     FROM workspace_lead_events w2 JOIN events e2 ON e2.id = w2.event_id
                    WHERE w2.workspace_id = wl.workspace_id AND w2.lead_id = l.id
                    ORDER BY w2.event_at DESC, w2.id DESC LIMIT 1) AS last_event_sender,
                  l.company_id AS company_id
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
        "contacts": attach_last_known_campaigns(
            conn, workspace_id, [dict(r) for r in rows]),
    }


def company_search_for_link(conn: sqlite3.Connection, q: str, limit: int = 10) -> dict:
    """Lightweight company autocomplete for the 'link lead → company' control.

    Matches any known domain, not just the primary -- typing the domain off a
    lead's email address has to find the company even when that address is on
    the brand/branch domain rather than the one in companies.domain.
    """
    term = f"%{(q or '').strip()}%"
    rows = conn.execute(
        f"""SELECT c.id, c.name, c.domain, c.industry,
                   (SELECT ci.identity_value_normalized FROM company_identities ci
                    WHERE ci.company_id = c.id AND ci.identity_type = 'domain'
                      AND ci.identity_value_normalized LIKE ?
                    LIMIT 1) AS matched_domain,
                   (SELECT COUNT(*) FROM leads l WHERE l.company_id = c.id) AS leads
            FROM companies c
            WHERE c.name LIKE ? OR c.domain LIKE ? OR {_COMPANY_IDENTITY_MATCH_SQL}
            ORDER BY leads DESC, c.name COLLATE NOCASE LIMIT ?""",
        (term, term, term, term, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("matched_domain") == d.get("domain"):
            d["matched_domain"] = None
        out.append(d)
    return {"companies": out}


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
    # No-identity leads: name 'unknown', no email/linkedin/company, no history,
    # only a system uid. Reported both for this workspace (what you are looking
    # at) and org-wide (what actually has to be deleted) -- these arrive in bulk
    # from a single bad snapshot pull, so a per-workspace cleanup leaves orphans
    # behind in every other workspace.
    empty_ws = junk_cleanup.cleanup_empty_leads(conn, workspace_id=workspace_id, dry_run=True)
    empty_org = junk_cleanup.cleanup_empty_leads(conn, dry_run=True)
    return {
        "since": since, "until": until,
        "range_active": bool(active_sql),
        "total": row["total"] or 0,
        "buckets": buckets,
        "junk_deletable": junk["selected"],
        "empty_leads_workspace": empty_ws["selected"],
        "empty_leads_org": empty_org["selected"],
        "empty_leads_sources": empty_org["distribution"]["top_sources"][:5],
    }


def _lead_domains(conn: sqlite3.Connection, lead_id: int) -> tuple[list[str], str]:
    """Candidate sending domains for a lead's email-finder run, most-trusted
    first, plus the lead's own email_domain. A company can have several, so the
    caller decides whether to auto-pick the first or prompt.

    Delegates to rank_company_domains() -- the same ranker the CLI path uses via
    om_lookup(). This used to hand-roll a company_identities-only query, which
    made the dashboard blind to the legacy companies.domain column: 44,608 of
    the 47,309 no-email leads that DO have a resolvable domain came back as
    "no_domain" in the UI while `pipeline email-finding-candidates` happily
    listed them. One ranker, one answer -- and the ranking now leads with proven
    found-count rather than role='primary' then alphabetical.
    """
    from pipeline import rank_company_domains

    lead = conn.execute(
        "SELECT company_id, email_domain, company FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    domains: list[str] = []
    company_text = ""
    if lead:
        company_text = (lead["company"] or "").strip()
        if lead["company_id"]:
            domains = list(rank_company_domains(conn, int(lead["company_id"])))
        own = (lead["email_domain"] or "").strip().lower()
        if own and own not in domains:
            domains.append(own)
    return domains, company_text


def enrichment_targets(
    conn: sqlite3.Connection, workspace_id: str, lead_ids: list[int],
) -> dict:
    """Resolve, per lead, the domain(s) the email-finder would use. Surfaces
    multi-domain (needs a choice) and no-domain (can't run) leads so the UI can
    warn before spending provider credits.

    `tried_domains` / `fresh_domains` mirror the per-(lead, domain) re-run guard
    in _run_email_finder(), so the dialog shows the same spend the run will
    actually make -- a lead whose every candidate has been tried reports
    fresh_domains == [] and is what the run will skip as "already_ran".
    """
    from pipeline_provider_attempts import (
        CAPABILITY_PROVIDERS, attempted_domains, has_attempted,
    )

    finder_providers = CAPABILITY_PROVIDERS["email_finding"]
    targets = []
    for lead_id in lead_ids:
        lead = conn.execute(
            """SELECT l.id, l.name, l.company, l.company_id, l.email, l.record_type
               FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id
               WHERE wl.workspace_id = ? AND l.id = ?""",
            (workspace_id, int(lead_id)),
        ).fetchone()
        if lead is None:
            continue
        if lead["record_type"] != "contact":
            # Surfaced rather than dropped: selecting a placeholder and getting
            # silence back is worse than being told why it can't run.
            targets.append({
                "lead_id": lead["id"], "name": lead["name"], "company": lead["company"],
                "domains": [], "domain_count": 0, "email": lead["email"],
                "blocked": "company_placeholder",
                "reason": "no person to find — research a real contact first",
            })
            continue
        domains, _ = _lead_domains(conn, int(lead_id))
        # Mirrors _run_email_finder's guard exactly, including its fallback:
        # no domain history but a prior attempt means the run will skip the
        # lead wholesale, so the dialog must predict that, not promise a run.
        tried = attempted_domains(conn, int(lead_id), finder_providers)
        if tried:
            fresh = [d for d in domains if d not in tried]
        elif any(has_attempted(conn, int(lead_id), p) for p in finder_providers):
            fresh = []
        else:
            fresh = list(domains)
        targets.append({
            "lead_id": lead["id"],
            "name": lead["name"],
            "company": lead["company"],
            "company_id": lead["company_id"],
            "has_email": bool((lead["email"] or "").strip()),
            "domains": domains,
            "tried_domains": [d for d in domains if d in tried],
            "fresh_domains": fresh,
            "chosen_domain": fresh[0] if fresh else (domains[0] if domains else None),
            "multi_domain": len(domains) > 1,
            "no_domain": not domains,
            "already_ran": bool(domains) and not fresh,
        })
    return {"targets": targets}
