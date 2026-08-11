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
from platform_registry import reply_event_sql_condition, reply_event_types_sql_list
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
                  sa.last_outbound_at, sa.last_inbound_at,
                  -- Reseller and provider are properties of the DOMAIN, not of
                  -- the mailbox, but "who did I buy this from" is a question you
                  -- ask while looking at a mailbox. Carried down rather than
                  -- making the reader cross-reference two tables.
                  sd.reseller AS domain_reseller,
                  sd.provider AS domain_provider
           FROM sender_accounts sa
           JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
           LEFT JOIN sender_domains sd ON sd.domain = sa.email_domain
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


def _domain_age_days(registration_date):
    """Whole days since registration, computed on read (a stored age goes stale)."""
    from pipeline_sender_accounts import domain_age_days

    return domain_age_days(registration_date)


def _dnsbl_detail(raw):
    """The full per-list report from sender_domains.dnsbl_status.

    _parse_dnsbl_status answers "listed?"; this answers "on what, checked when,
    and which checks could not be run" -- the evidence behind the verdict, which
    every surface showing the verdict needs and none of them had.
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return {"results": [], "partial": False, "tiers_scanned": [],
                "checks_failed": 0, "listed_on": []}
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        return {"results": [], "partial": False, "tiers_scanned": [],
                "checks_failed": 0, "listed_on": []}
    results = report.get("results") or []
    return {
        "results": results,
        "partial": bool(report.get("partial")),
        "tiers_scanned": report.get("tiers_scanned") or [],
        # A check that could not run is NOT a listing. Keeping the count visible
        # is what stops a rate-limited scan reading as a blacklisting.
        "checks_failed": sum(1 for r in results if r.get("status") == "check_failed"),
        "listed_on": [r.get("name") for r in results if r.get("status") == "listed"],
    }


def domain_health(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str] = None, until: Optional[str] = None,
    exclude_never_sent: bool = True,
) -> dict:
    """Sending domains for the workspace, with live reply/bounce rates.

    `exclude_never_sent` drops domains no mailbox has ever sent from. Those are
    not sending domains at all -- they arrived because inbound events used to
    bootstrap a sender account from the *other* party's address, so a reply from
    isenberg.umass.edu made it look like somewhere we send from. The write path
    no longer does that (see log_event), but existing rows persist until
    `sender-domains prune-never-sent` clears them, and they should not be
    padding the page or the blacklist sweep in the meantime.
    """
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
                  sd.dnsbl_status, sd.sending_ip, sd.reseller, sd.provider,
                  sd.registration_date, sd.registration_checked_at,
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
        # Never sent from, by any mailbox, ever -- not a date-range statement.
        never_sent = not r["last_outbound_at"]
        if never_sent and exclude_never_sent:
            continue
        listed, summary, checked_at = _parse_dnsbl_status(r["dnsbl_status"])
        detail = _dnsbl_detail(r["dnsbl_status"])
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
            "never_sent": never_sent,
            "registered": bool(r["registered"]),
            "is_active": bool(r["domain_active"]),
            "sending_ip": r["sending_ip"],
            "reseller": r["reseller"],
            # This used to be `r["reseller"]` under a column headed "Provider",
            # so a domain bought through a reseller reported the reseller as the
            # platform that sends its mail. They are different facts: provider =
            # Google Workspace / Microsoft 365 / an SMTP relay; reseller = who
            # you bought from.
            "provider": r["provider"],
            "registration_date": r["registration_date"],
            "registration_checked_at": r["registration_checked_at"],
            "domain_age_days": _domain_age_days(r["registration_date"]),
            "domain_cost": r["domain_cost"],
            "currency": r["currency"],
            "listed": listed,
            "dnsbl_summary": summary,
            "dnsbl_checked_at": checked_at,
            # The per-list report travels with the row. Collapsing it to a
            # verdict here is what forced a second round trip to answer "listed
            # on what, and when did we last actually manage to check?".
            "dnsbl_results": detail.get("results") or [],
            "dnsbl_partial": detail.get("partial", False),
            "dnsbl_tiers_scanned": detail.get("tiers_scanned") or [],
            "dnsbl_checks_failed": detail.get("checks_failed", 0),
            "dnsbl_listed_on": detail.get("listed_on") or [],
        })
    return {
        "domains": domains,
        "flagged": [d["domain"] for d in domains if d["listed"]],
        "exclude_never_sent": exclude_never_sent,
        "never_sent_hidden": sum(1 for r in rows if not r["last_outbound_at"])
                             if exclude_never_sent else 0,
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


# A campaign whose every event came from a booking tool is a calendar, not
# outbound. Calendly publishes the booked event TYPE ("30 Minute Meeting",
# "Popcam Rental Onboarding") with each webhook and ingest stores that as the
# campaign, so those slots showed up in the Campaigns table beside real
# sequences -- and the same lead appeared twice, once under the campaign that
# actually reached them and once under the calendar slot they picked.
#
# ALL rather than ANY: a genuine outbound campaign that somehow collected one
# scheduling event must not be reclassified as a calendar. (Confirmed against
# the live database: no campaign mixes the two, so the two rules agree today --
# "all" is the one that stays correct if that ever stops being true.)
_SCHEDULING_CAMPAIGN_IDS_SQL = f"""
    SELECT e.campaign_id
      FROM events e
     WHERE e.campaign_id IS NOT NULL
  GROUP BY e.campaign_id
    HAVING SUM(CASE WHEN LOWER(COALESCE(json_extract(e.metadata_json, '$.platform'), ''))
                         IN ({SCHEDULING_PLATFORMS_SQL_LIST}) THEN 0 ELSE 1 END) = 0
"""


def scheduling_campaign_ids(conn: sqlite3.Connection) -> set:
    """Campaign ids that are booking-tool event types, not outbound campaigns."""
    return {r["campaign_id"] for r in conn.execute(_SCHEDULING_CAMPAIGN_IDS_SQL).fetchall()}


def _scheduling_filter(include_scheduling: bool, column: str = "campaign_id") -> str:
    """SQL fragment excluding scheduling campaigns, or '' when they're wanted."""
    if include_scheduling:
        return ""
    return f" AND {column} NOT IN ({_SCHEDULING_CAMPAIGN_IDS_SQL})"


# The synthetic row that catches meetings we could not trace to a campaign.
# Without it, "exclude scheduling tools" would silently drop meetings entirely,
# which trades a double count for a missing one.
UNATTRIBUTED_SCHEDULING_ID = -1
UNATTRIBUTED_SCHEDULING_NAME = "Unattributed (scheduling)"


def _meetings_by_campaign(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str], until: Optional[str],
) -> tuple[dict, int]:
    """{campaign_id: meetings} attributed to the campaign that EARNED the meeting.

    A booking webhook carries the calendar slot as its campaign, so counting
    meetings off `events.campaign_id` credits "30 Minute Meeting" instead of the
    sequence that put the meeting there. Every meeting is therefore resolved
    through the same last-known-campaign chain the replies table and the company
    pane use -- the lead's most recent campaign-bearing non-scheduling event,
    else a colleague's at the same company.

    Returns (per-campaign counts, unattributed count). Meetings that resolve to
    nothing are counted separately rather than dropped: an untraceable meeting
    still happened.
    """
    range_sql, range_params = _range_clause(since, until, "wle.event_at")
    rows = conn.execute(
        f"""SELECT DISTINCT wle.lead_id AS lead_id, l.company_id AS company_id
              FROM workspace_lead_events wle
              JOIN events e ON e.id = wle.event_id
              JOIN leads l ON l.id = wle.lead_id
             WHERE wle.workspace_id = ? AND e.event_type = 'meeting_booked'{range_sql}""",
        (workspace_id, *range_params),
    ).fetchall()
    if not rows:
        return {}, 0
    resolved = resolve_last_known_campaigns(conn, workspace_id, [dict(r) for r in rows])
    counts: dict = defaultdict(int)
    unattributed = 0
    for row in rows:
        campaign_id = resolved.get(int(row["lead_id"]), _EMPTY_CAMPAIGN)["campaign_id"]
        if campaign_id is None:
            unattributed += 1
        else:
            counts[campaign_id] += 1
    return dict(counts), unattributed


def _sentiment_by_campaign(
    conn: sqlite3.Connection, workspace_id: str,
    since: Optional[str], until: Optional[str],
) -> tuple[dict, dict]:
    """{campaign_id: {sentiment: n}} counting each lead exactly ONCE.

    A lead can sit in several campaigns, so summing per-campaign sentiment over
    campaign_leads double-counts: the workspace total and the column totals stop
    agreeing, and a positive reply inflates every campaign the lead ever touched.
    Each lead is credited to its last known campaign only -- the same attribution
    the Meetings column and the Contacts list use, so all three tell one story.

    Returns (per-campaign counts, unattributed counts).
    """
    range_sql, range_params = _range_clause(since, until, "wl.current_sentiment_since")
    rows = conn.execute(
        f"""SELECT wl.lead_id AS lead_id, l.company_id AS company_id,
                   lower(wl.current_status_sentiment) AS sentiment
              FROM workspace_leads wl
              JOIN leads l ON l.id = wl.lead_id
             WHERE wl.workspace_id = ?
               AND wl.current_status_sentiment IS NOT NULL
               AND wl.current_sentiment_since IS NOT NULL{range_sql}""",
        (workspace_id, *range_params),
    ).fetchall()
    if not rows:
        return {}, {}
    from lead_links import CONVERSATION_ROOT_SQL

    roots = {int(r["lead_id"]): int(r["root_id"]) for r in
             conn.execute(CONVERSATION_ROOT_SQL).fetchall()}
    # Resolve the ROOTS too, not just the leads that carry a sentiment. A
    # colleague who was forwarded in may be the only one with a positive, while
    # the campaign that started the thread reached the root -- so the root has
    # to be in the set being attributed, or the credit falls back to whoever
    # happened to reply.
    to_resolve = {int(r["lead_id"]): dict(r) for r in rows}
    for lead_id in list(to_resolve):
        root = roots.get(lead_id, lead_id)
        if root not in to_resolve:
            root_row = conn.execute(
                "SELECT id AS lead_id, company_id FROM leads WHERE id = ?", (root,)
            ).fetchone()
            if root_row:
                to_resolve[root] = dict(root_row)
    resolved = resolve_last_known_campaigns(
        conn, workspace_id, list(to_resolve.values()))
    # One conversation, one count. Without this, a positive reply and the
    # colleague they forwarded us to both land in the same campaign's Positive
    # column -- two rows for one opportunity, which is the thing the link exists
    # to stop. Leads in no conversation are their own root.
    per_campaign: dict = defaultdict(lambda: defaultdict(set))
    unattributed: dict = defaultdict(set)
    for row in rows:
        sentiment = row["sentiment"]
        if sentiment not in SENTIMENT_KEYS:
            continue
        lead_id = int(row["lead_id"])
        # The whole conversation is credited to the campaign that reached its
        # ROOT -- the outbound that started the thread, not the one that
        # happened to touch whoever was forwarded in later.
        root = roots.get(lead_id, lead_id)
        campaign_id = resolved.get(root, resolved.get(lead_id, _EMPTY_CAMPAIGN))["campaign_id"]
        if campaign_id is None:
            unattributed[sentiment].add(root)
        else:
            per_campaign[campaign_id][sentiment].add(root)
    return ({cid: {k: len(v) for k, v in sents.items()}
             for cid, sents in per_campaign.items()},
            {k: len(v) for k, v in unattributed.items()})


def campaign_audit(
    conn: sqlite3.Connection,
    workspace_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    include_scheduling: bool = False,
) -> dict:
    """Per-campaign activity, in the same columns as the daily-activity matrix.

    `include_scheduling` brings booking-tool event types back into the list. Off
    by default: a Calendly slot is a calendar, not a campaign you can judge.
    """
    cte, since_params = _ws_events(since, until)
    # The same column expressions the daily matrix uses, grouped by campaign
    # instead of by day -- one definition, so "DMs sent" cannot mean one thing
    # per day and another per campaign.
    metric_rows = conn.execute(
        cte + f"""
        SELECT campaign_id,
               {_daily_columns_sql(exclude=("meetings",))},
               -- Bookings that landed on THIS calendar slot, straight from the
               -- event stream. Only ever shown for scheduling rows, which are
               -- opt-in -- so it cannot double-count the attributed figure.
               SUM(event_type = 'meeting_booked') AS meetings_on_calendar,
               SUM({reply_event_sql_condition()}) AS replies,
               SUM(event_type IN {BOUNCE_EVENT_TYPES_SQL}) AS bounces
        FROM ws_events
        WHERE campaign_id IS NOT NULL
        GROUP BY campaign_id""",
        (workspace_id, *since_params),
    ).fetchall()
    metrics = {r["campaign_id"]: r for r in metric_rows}

    scheduling_ids = scheduling_campaign_ids(conn)

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

    meetings_by_campaign, meetings_unattributed = _meetings_by_campaign(
        conn, workspace_id, since, until)
    sentiment_by_campaign, sentiment_unattributed = _sentiment_by_campaign(
        conn, workspace_id, since, until)

    def _row(campaign_id, name, sent, m, meetings, sentiments):
        """One table row. `meetings` is the attributed count for a real campaign
        and the raw on-calendar count for a scheduling row -- a calendar's own
        question is "what got booked here", which is not the same question."""
        replies = (m["replies"] if m else 0) or 0
        bounces = (m["bounces"] if m else 0) or 0
        positive = sentiments.get("positive", 0)
        row = {
            "id": campaign_id,
            "name": name,
            "sent": sent,
            "replies": replies,
            "reply_rate": round(replies / sent, 4) if sent else None,
            "positive": positive,
            "positive_rate": round(positive / sent, 4) if sent else None,
            "meetings": meetings,
            "bounces": bounces,
            "bounce_rate": round(bounces / sent, 4) if sent else None,
        }
        for key in DAILY_EVENT_KEYS:
            if key == "meetings":
                continue
            row[key] = (m[key] if m else 0) or 0
        for key in SENTIMENT_KEYS:
            row[key] = sentiments.get(key, 0)
        return row

    out = []
    for c in campaigns:
        is_scheduling = c["id"] in scheduling_ids
        if is_scheduling and not include_scheduling:
            continue
        m = metrics.get(c["id"])
        meetings = (((m["meetings_on_calendar"] if m else 0) or 0) if is_scheduling
                    else meetings_by_campaign.get(c["id"], 0))
        row = _row(
            c["id"], c["name"], (m["email_sent"] if m else 0) or 0, m, meetings,
            sentiment_by_campaign.get(c["id"], {}),
        )
        row["is_scheduling"] = is_scheduling
        out.append(row)

    # Meetings and sentiment that resolved to no campaign at all. Surfaced as one
    # synthetic row so excluding scheduling tools never silently loses a booking.
    if meetings_unattributed or sentiment_unattributed:
        row = _row(UNATTRIBUTED_SCHEDULING_ID, UNATTRIBUTED_SCHEDULING_NAME,
                   0, None, meetings_unattributed, sentiment_unattributed)
        row["is_scheduling"] = True
        row["is_unattributed"] = True
        out.append(row)

    # Hide campaigns with zero activity in the selected range -- an all-zero row
    # is just noise (the campaign exists but did nothing in this window).
    activity_keys = ("sent", "replies", "bounces", "meetings", *SENTIMENT_KEYS,
                     *(k for k in DAILY_EVENT_KEYS if k != "meetings"))
    out = [c for c in out if any(c.get(k) for k in activity_keys)]
    out.sort(key=lambda c: (c.get("is_unattributed", False), -c["sent"], -c["meetings"]))
    return {
        "since": since,
        "campaigns": out,
        "include_scheduling": include_scheduling,
        "scheduling_excluded": sum(1 for c in campaigns if c["id"] in scheduling_ids),
        "columns": list(DAILY_COLUMN_KEYS),
    }


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
    reply_kind: Optional[str] = None,
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

    Each row also carries a `reply_kind` (human / auto_reply / bounce /
    status_only). A bounce and an out-of-office both set a lead's sentiment, so
    a list built on sentiment alone shows all three under a heading that says
    "Replies" -- which reads as "we got four real replies" when we got none.
    The classification is campaign_stats.classify_reply_kind, shared with the
    CLI preset so the two can never drift.

    The campaign column is `last_known_campaign_sql`, not the latest reply's
    own campaign_id: a lead whose sentiment came from a bounce has no campaign
    on that event, and showing "—" for it was wrong rather than unknown.
    """
    reply_cond = reply_event_sql_condition()
    # When the lead's most recent inbound reply happened. Needed because not
    # every channel reports a sentiment: email sequencers ship a status label
    # with each reply and that is what sets current_status_sentiment, but Prosp
    # sends none -- all 715 inbound LinkedIn events carried lead_status_raw
    # NULL. Anchoring this list on sentiment alone therefore hid an entire
    # channel: 92 leads had a LinkedIn DM reply and only 12 appeared here.
    # Materialized ONCE as a CTE, not as a correlated subquery. Written inline
    # it ran per candidate row and per reference (range clause, WHERE, SELECT),
    # taking this query from 0.4s to 4.1s on a 126k-event workspace. As a CTE it
    # is one indexed pass over workspace_lead_events.
    last_reply_cte = f"""WITH last_reply AS (
        SELECT wle_r.lead_id AS lead_id, MAX(wle_r.event_at) AS reply_at
          FROM workspace_lead_events wle_r
          JOIN events e_r ON e_r.id = wle_r.event_id
         WHERE wle_r.workspace_id = ?
           -- Indexed pre-filter (idx_ws_events_type) before the join to
           -- `events`: without it every workspace event is joined just to read
           -- its direction. Lossless -- see reply_event_types_sql_list().
           AND wle_r.event_type IN ({reply_event_types_sql_list()})
           AND {reply_cond.replace('event_type', 'e_r.event_type').replace('direction', 'e_r.direction')}
      GROUP BY wle_r.lead_id
    )"""
    last_reply_join = " LEFT JOIN last_reply lrx ON lrx.lead_id = wl.lead_id"
    # A lead is on this list from the day it entered its current sentiment, or
    # -- lacking one -- the day it last replied. Sentiment still wins where it
    # exists, so nothing that used to reconcile with the daily sentiment
    # columns stops reconciling.
    anchor_sql = "COALESCE(wl.current_sentiment_since, lrx.reply_at)"

    # The date filter is applied to each branch on its OWN column rather than
    # once to the COALESCE. A range predicate over COALESCE(a, b) is not
    # sargable -- SQLite cannot use the index on either column and falls back to
    # scanning every workspace_lead, which cost 0.4s -> 5.8s on a 126k-event
    # workspace. Split, each branch keeps its index and the two are equivalent:
    # a lead with a sentiment is filtered on the sentiment anchor, one without
    # on its reply time, which is exactly what the COALESCE selected anyway.
    sent_range, sent_params = _range_clause(since, until, "wl.current_sentiment_since")
    reply_range, reply_params = _range_clause(since, until, "lrx.reply_at")

    # The CTE is scoped to the range too: a lead whose last reply falls outside
    # it can only qualify through the sentiment branch, which does not read this.
    cte_range, cte_params = _range_clause(since, until, "wle_r.event_at")
    last_reply_cte = last_reply_cte.replace(
        "      GROUP BY wle_r.lead_id",
        cte_range + "\n      GROUP BY wle_r.lead_id")

    # The CTE takes the workspace and its range first, then the outer query's.
    base_params: list = [workspace_id, *cte_params, workspace_id,
                         *sent_params, *reply_params]
    camp = ""
    if campaign_id is not None:
        camp = (" AND wl.lead_id IN (SELECT lead_id FROM campaign_leads"
                " WHERE campaign_id = ?)")
        base_params.append(int(campaign_id))

    # Either a materialized sentiment, or a real inbound reply with no verdict
    # attached yet. The second kind renders with sentiment "–" rather than being
    # assigned one: inventing "positive" for every LinkedIn DM would corrupt
    # every positive count on the Campaigns page.
    base_where = f"""wl.workspace_id = ?
          AND ((wl.current_status_sentiment IS NOT NULL
                AND wl.current_sentiment_since IS NOT NULL{sent_range})
               OR (wl.current_status_sentiment IS NULL
                   AND lrx.reply_at IS NOT NULL{reply_range})){camp}"""

    facets = {
        key: [
            dict(r) for r in conn.execute(
                f"""{last_reply_cte}
                    SELECT {col} AS value, COUNT(*) AS n
                      FROM workspace_leads wl{last_reply_join}
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
    # reply_kind is computed in SQL so the filter runs BEFORE the row cap.
    # Classifying the fetched page instead meant asking for human replies on a
    # page that happened to hold five bounces returned nothing at all.
    from campaign_stats import reply_kind_case_sql

    kind_sql = reply_kind_case_sql("lr", "wl.current_status_sentiment")
    kind_filter = ""
    kind_params: list = []
    if reply_kind and reply_kind != "all":
        wanted = [k.strip() for k in str(reply_kind).split(",") if k.strip()]
        if wanted:
            kind_filter = (" AND reply_kind IN (%s)"
                           % ", ".join("?" for _ in wanted))
            kind_params = wanted

    inner = f"""
        SELECT wl.lead_id AS lead_id,
               wl.current_status_sentiment AS sentiment,
               wl.current_status_label AS status_label,
               {anchor_sql} AS event_at,
               wl.last_activity_at AS last_activity_at,
               l.name AS lead_name, l.company_id AS company_id,
               l.company AS company, l.title AS title, l.email AS email,
               l.linkedin_url, l.linkedin_sales_nav_id,
               (SELECT GROUP_CONCAT(t.tag, ',') FROM workspace_lead_tags t
                WHERE t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id) AS tags,
               lr.id AS event_id, lr.subject AS subject,
               lr.event_type AS reply_event_type,
               CAST(json_extract(lr.metadata_json, '$.is_auto_reply') AS INTEGER)
                   AS is_auto_reply,
               EXISTS(SELECT 1 FROM bounce_events be WHERE be.lead_id = wl.lead_id)
                   AS has_bounce,
               {kind_sql} AS reply_kind,
               (lr.id IS NOT NULL AND json_extract(lr.metadata_json, '$.body') IS NOT NULL)
                   AS has_body
        FROM workspace_leads wl{last_reply_join}
        JOIN leads l ON l.id = wl.lead_id
        LEFT JOIN events lr ON lr.id = (
            SELECT wle2.event_id
            FROM workspace_lead_events wle2 JOIN events e2 ON e2.id = wle2.event_id
            WHERE wle2.workspace_id = wl.workspace_id
              AND wle2.lead_id = wl.lead_id
              AND {reply_cond.replace('event_type', 'e2.event_type').replace('direction', 'e2.direction')}
            ORDER BY wle2.event_at DESC, wle2.id DESC LIMIT 1)
        WHERE {base_where}{narrow}"""

    # Counted over the whole selection, never the page: the split is the point,
    # so it has to stay true whichever kind is being shown.
    kind_counts = {
        r["reply_kind"]: r["n"] for r in conn.execute(
            f"{last_reply_cte} SELECT reply_kind, COUNT(*) AS n FROM ({inner})"
            " GROUP BY reply_kind",
            params,
        ).fetchall()
    }

    rows = conn.execute(
        f"""{last_reply_cte}
            SELECT * FROM ({inner})
             WHERE 1=1{kind_filter}
             ORDER BY event_at DESC
             LIMIT ?""",
        [*params, *kind_params, limit],
    ).fetchall()
    # Attribution is resolved for the whole page at once, after the list is
    # known -- doing it inline per row is what made this query take minutes.
    out = attach_last_known_campaigns(conn, workspace_id, [dict(r) for r in rows])

    facets["reply_kind"] = [
        {"value": k, "n": kind_counts[k]}
        for k in sorted(kind_counts, key=lambda k: -kind_counts[k])
    ]
    return {
        "campaign_id": campaign_id,
        "sentiment": sentiment,
        "status_label": status_label,
        "reply_kind": reply_kind,
        "facets": facets,
        "kind_counts": kind_counts,
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
# One expression per column, so the daily matrix and the per-campaign table are
# built from the same definitions with different GROUP BYs. Assembled rather
# than hand-written twice: "DMs received" must not mean one thing per day and
# another per campaign.
_DAILY_COLUMN_EXPRESSIONS = {
    "email_sent": "SUM(event_type = 'email_sent')",
    "email_received": (
        "SUM(direction = 'inbound' AND channel = 'email'"
        f" AND {reply_event_sql_condition()})"),
    "dm_sent": "SUM(event_type = 'linkedin_message' AND direction = 'outbound')",
    "dm_received": ("SUM((event_type = 'linkedin_message' AND direction = 'inbound')"
                    " OR event_type = 'linkedin_dm_reply')"),
    "connects_sent": "SUM(event_type IN ('linkedin_connect_sent', 'linkedin_connect'))",
    "connects_accepted": "SUM(event_type = 'linkedin_connection_accepted')",
    "meetings": "SUM(event_type = 'meeting_booked')",
}


def _daily_columns_sql(*, exclude: tuple = ()) -> str:
    """The daily column expressions as a SELECT list.

    campaign_audit excludes `meetings`: counting them off events.campaign_id
    credits the calendar slot rather than the campaign that earned the booking,
    so it resolves them through last-known-campaign instead (_meetings_by_campaign).
    """
    return ",\n    ".join(
        f"{expr} AS {name}"
        for name, expr in _DAILY_COLUMN_EXPRESSIONS.items() if name not in exclude)


_DAILY_COLUMNS_SQL = "\n    " + _daily_columns_sql() + "\n"

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
    range -- counted once per CONVERSATION.

    Two people in one thread (someone replied, then forwarded us to a colleague
    who also replied) are one opportunity, not two positives. Leads that are in
    no conversation are their own root, so this equals a plain COUNT(DISTINCT
    lead_id) until somebody records a link -- the join is safe to apply
    unconditionally. See lead_links.py.
    """
    from lead_links import CONVERSATION_ROOT_SQL

    range_sql, params = _range_clause(since, until, "wl.current_sentiment_since")
    return conn.execute(
        f"""SELECT COUNT(DISTINCT COALESCE(cr.root_id, wl.lead_id)) AS n
              FROM workspace_leads wl
              LEFT JOIN ({CONVERSATION_ROOT_SQL}) cr ON cr.lead_id = wl.lead_id
             WHERE wl.workspace_id = ?
               AND lower(wl.current_status_sentiment) = lower(?)
               AND wl.current_sentiment_since IS NOT NULL{range_sql}""",
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
    include_scheduling: bool = False,
) -> dict:
    """Per-day event-type matrix for the workspace, optionally one campaign.

    Booking-tool event types are excluded by default, matching campaign_audit --
    a meeting counted here under "30 Minute Meeting" is the same double count
    the Campaigns table had. Selecting a scheduling campaign explicitly still
    shows it; the exclusion is about the unfiltered view.
    """
    range_sql, range_params = _range_clause(since, until, "wle.event_at")
    campaign_sql = ""
    params: list = [workspace_id, *range_params]
    if campaign_id is not None:
        campaign_sql = " AND e.campaign_id = ?"
        params.append(int(campaign_id))
    elif not include_scheduling:
        campaign_sql = (" AND (e.campaign_id IS NULL OR e.campaign_id NOT IN"
                        f" ({_SCHEDULING_CAMPAIGN_IDS_SQL}))")
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
                  l.email_verification_status, l.email_verified_at,
                  l.original_source, l.original_source_detail, l.original_source_at,
                  l.latest_source, l.latest_source_detail, l.latest_source_at,
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
    "lead_link": ("""SELECT k.id, k.kind, k.note, k.created_at,
                            k.from_lead_id, k.to_lead_id,
                            lf.name AS from_name, lt.name AS to_name
                       FROM lead_links k
                       JOIN leads lf ON lf.id = k.from_lead_id
                       JOIN leads lt ON lt.id = k.to_lead_id
                      WHERE k.id = ?""", "str"),
    "suppression": ("""SELECT e.id, e.scope, e.entry_type, e.value_raw,
                              e.value_normalized, e.reason, e.note, e.source,
                              e.created_at, e.expires_at, e.revoked_at,
                              w.slug AS workspace_slug
                         FROM suppression_entries e
                         LEFT JOIN workspaces w ON w.id = e.workspace_id
                        WHERE e.id = ?""", "str"),
}

# Entity types the relay has no snapshot action for, so their outbox rows can
# never drain. The relay's allowlist is lead_core / lead_workspace / company /
# sender_account / sender_domain; `suppression` is queued by the outbox triggers
# (sync_contract.SYNC_MAP) but has no push handler on either side yet. Saying so
# in the panel is the point: without it, a row sitting at attempts=0 since the
# day it was written looks like a stuck push rather than a missing endpoint.
UNPUSHABLE_ENTITY_TYPES = {
    "lead_link": (
        "The relay has no lead-link endpoint yet, so these rows stay queued. "
        "Conversation grouping is fully functional locally -- the counts on "
        "this machine are already correct; only cross-machine sync is pending."
    ),
    "suppression": (
        "The relay has no suppression endpoint yet, so these rows stay queued. "
        "They are not lost and not failing -- nothing drains them. Local "
        "suppression is fully functional; only cross-machine sync is pending."
    ),
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
        elif entity_type == "suppression":
            import suppression
            payload = suppression.build_suppression_sync_payload(conn, entity_id)
        elif entity_type == "lead_link":
            import lead_links
            payload = lead_links.build_lead_link_sync_payload(conn, entity_id)
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
        # Not an error: the payload is correct, there is just nowhere to send it.
        "pushable": entity_type not in UNPUSHABLE_ENTITY_TYPES,
        "unpushable_reason": UNPUSHABLE_ENTITY_TYPES.get(entity_type),
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
              WHERE t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id) AS tags,
             -- Last inbound message, for the Contacts list's "Last reply"
             -- column. Scoped through workspace_lead_events so it means the
             -- same thing the rest of the row does: activity in THIS workspace.
             (SELECT wr.event_at FROM workspace_lead_events wr
               JOIN events er ON er.id = wr.event_id
              WHERE wr.workspace_id = wl.workspace_id AND wr.lead_id = wl.lead_id
                AND lower(COALESCE(er.direction, '')) = 'inbound'
              ORDER BY wr.event_at DESC, wr.id DESC LIMIT 1) AS last_reply_at,
             (SELECT er.subject FROM workspace_lead_events wr
               JOIN events er ON er.id = wr.event_id
              WHERE wr.workspace_id = wl.workspace_id AND wr.lead_id = wl.lead_id
                AND lower(COALESCE(er.direction, '')) = 'inbound'
              ORDER BY wr.event_at DESC, wr.id DESC LIMIT 1) AS last_reply_subject"""


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
    # One page (<= 200 rows), so this costs what the company pane already pays.
    # Resolved here rather than in SQL because the fallback chain -- most recent
    # campaign-bearing event, else a colleague's at the same company -- is the
    # same one the Campaigns page uses, and a second implementation would drift.
    leads = attach_last_known_campaigns(conn, workspace_id, [dict(r) for r in rows])
    return {
        "total": total, "limit": limit, "offset": offset,
        "q": q, "sort": sort, "leads": leads,
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


# ---------------------------------------------------------------------------
# Bounce explorer
#
# bounce_events is never pruned -- it is full history -- so the only thing
# deciding how far back you look is the caller's date range.
#
# Grouping is what makes hundreds of rows navigable: on a real corpus, 184
# bounces carrying a diagnostic message collapse to ~40 distinct failures, and
# 94% of bounces carry no message at all (the sequencer never sent one), which
# is itself worth seeing as one row rather than three thousand.
# ---------------------------------------------------------------------------

_SIG_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SIG_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_SIG_URL = re.compile(r"https?://\S+")
#: Anchored on real TLDs so diagnostic tokens that merely look like hostnames
#: (TRANSPORT.RULES.RejectMessage, RESOLVER.RST.SenderNotAuthenticated) survive
#: -- those tokens ARE the failure, and blanking them merged unrelated causes.
_SIG_HOST = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|edu|gov|mil|int|io|co|us|uk|de|fr|ca|au|nl|es|it|"
    r"jp|cn|in|br|mx|ru|ch|se|no|dk|fi|pl|be|at|nz|ie|za|info|biz|me|tv|cc|ai|app|dev|"
    r"xyz|online|site|email|cloud)\b"
)
_SIG_NUM = re.compile(r"\b\d{2,}\b")
_SIG_ESC = re.compile(r"^\s*(\d\.\d{1,3}\.\d{1,3})\b")

#: 60 characters. Measured against a live corpus: 40 chars merged distinct
#: failures, 80 split single failures into a dozen rows (timestamps and ids
#: leaking past the normalizer), and 60 sat on the plateau between them while
#: still reading as a label.
BOUNCE_SIGNATURE_CHARS = 60


def bounce_signature(message: Optional[str], chars: int = BOUNCE_SIGNATURE_CHARS) -> str:
    """A stable grouping key for a bounce diagnostic.

    The enhanced status code (5.7.193 and friends) is kept as a prefix because
    it is the real classifier -- smtp_code is null on ~98% of rows, so the code
    embedded in the message text is often the only one there is.
    """
    text = (message or "").strip().lower()
    if not text:
        return ""
    prefix = ""
    match = _SIG_ESC.match(text)
    if match:
        prefix = match.group(1) + " "
        text = text[match.end():]
    text = _SIG_URL.sub(" ", text)
    text = _SIG_EMAIL.sub(" <email> ", text)
    text = _SIG_IP.sub(" <ip> ", text)
    text = _SIG_HOST.sub(" <host> ", text)
    text = _SIG_NUM.sub(" <n> ", text)
    text = re.sub(r"[^a-z0-9<>\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (prefix + text)[:chars].strip()


def _bounce_filters(
    workspace_id: Optional[str], since, until, domain, mailbox, campaign_id, bounce_type,
) -> tuple[str, list]:
    where = ["1=1"]
    params: list = []
    if workspace_id:
        # Older rows predate the workspace_id column; a workspace filter that
        # silently hid them would under-report history that is genuinely theirs.
        where.append("(b.workspace_id = ? OR b.workspace_id IS NULL)")
        params.append(workspace_id)
    range_sql, range_params = _range_clause(since, until, "b.last_seen_at")
    if range_sql:
        where.append(range_sql.replace(" AND ", "", 1) if range_sql.startswith(" AND ") else range_sql)
        params.extend(range_params)
    if domain:
        where.append("lower(b.sender_email) LIKE ?")
        params.append(f"%@{str(domain).lower()}")
    if mailbox:
        where.append("lower(b.sender_email) = lower(?)")
        params.append(mailbox)
    if campaign_id:
        where.append("b.campaign_id = ?")
        params.append(int(campaign_id))
    if bounce_type:
        where.append("lower(b.bounce_type) = lower(?)")
        params.append(bounce_type)
    return " AND ".join(where), params


def bounce_explorer(
    conn: sqlite3.Connection,
    workspace_id: Optional[str] = None,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    domain: Optional[str] = None,
    mailbox: Optional[str] = None,
    campaign_id: Optional[int] = None,
    bounce_type: Optional[str] = None,
    group_by: str = "signature",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Bounces for a range, grouped for triage and paginated for reading."""
    where, params = _bounce_filters(
        workspace_id, since, until, domain, mailbox, campaign_id, bounce_type)

    total = conn.execute(
        f"SELECT COUNT(*) c, COALESCE(SUM(occurrence_count), 0) occ FROM bounce_events b WHERE {where}",
        params,
    ).fetchone()

    rows = conn.execute(
        f"""SELECT b.id, b.lead_id, b.lead_email, b.sender_email, b.bounce_type,
                   b.bounce_message, b.smtp_code, b.recipient_mx, b.platform,
                   b.campaign_id, b.campaign_name, b.occurrence_count,
                   b.first_seen_at, b.last_seen_at,
                   l.name AS lead_name, l.company AS company
              FROM bounce_events b
              LEFT JOIN leads l ON l.id = b.lead_id
             WHERE {where}
             ORDER BY b.last_seen_at DESC
             LIMIT ? OFFSET ?""",
        [*params, int(limit), int(offset)],
    ).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        item["signature"] = bounce_signature(r["bounce_message"])
        items.append(item)

    groups: dict[str, dict] = {}
    if group_by:
        group_rows = conn.execute(
            f"""SELECT b.id, b.bounce_message, b.smtp_code, b.bounce_type,
                       b.sender_email, b.lead_email, b.campaign_name, b.platform,
                       b.occurrence_count, b.last_seen_at
                  FROM bounce_events b WHERE {where}""",
            params,
        ).fetchall()
        for r in group_rows:
            if group_by == "signature":
                sig = bounce_signature(r["bounce_message"])
                # The no-diagnostic majority is one honest row saying so, not
                # thousands of empty ones -- and it names the platform, because
                # "your sequencer never sent us a reason" is the finding.
                key = sig or f"(no diagnostic message — {r['platform'] or 'unknown platform'})"
            elif group_by == "type":
                key = r["bounce_type"] or "unknown"
            elif group_by == "mailbox":
                key = r["sender_email"] or "unknown"
            elif group_by == "domain":
                key = (r["sender_email"] or "").split("@")[-1] or "unknown"
            elif group_by == "recipient_domain":
                # Whose mail server is rejecting us, rather than which of our
                # domains is being rejected. One university bouncing 400 sends
                # is a list problem; four hundred domains bouncing one each is
                # a reputation problem, and the sending-domain grouping cannot
                # tell those apart.
                key = (r["lead_email"] or "").split("@")[-1].lower() or "unknown"
            elif group_by == "campaign":
                key = r["campaign_name"] or "unattributed"
            else:
                key = "all"
            entry = groups.setdefault(key, {
                "key": key, "bounces": 0, "occurrences": 0,
                "example_id": r["id"], "example_message": r["bounce_message"],
                "smtp_code": r["smtp_code"], "bounce_type": r["bounce_type"],
                "last_seen_at": r["last_seen_at"],
            })
            entry["bounces"] += 1
            entry["occurrences"] += int(r["occurrence_count"] or 1)
            if (r["last_seen_at"] or "") > (entry["last_seen_at"] or ""):
                entry["last_seen_at"] = r["last_seen_at"]

    return {
        "total": int(total["c"] or 0),
        "total_occurrences": int(total["occ"] or 0),
        "group_by": group_by,
        "groups": sorted(groups.values(), key=lambda g: -g["bounces"]),
        "bounces": items,
        "limit": limit,
        "offset": offset,
        "signature_chars": BOUNCE_SIGNATURE_CHARS,
    }


def bounce_detail(
    conn: sqlite3.Connection, bounce_id: str, *, sends_before: int = 5,
) -> dict:
    """One bounce, its full message, and the sends that preceded it.

    "Is this the copy or is this the list?" is unanswerable from a bounce row
    alone, so the messages we sent this lead before it bounced come back with
    it -- the same event shape the lead timeline renders.
    """
    row = conn.execute(
        """SELECT b.*, l.name AS lead_name, l.company AS company, l.title AS title
             FROM bounce_events b LEFT JOIN leads l ON l.id = b.lead_id
            WHERE b.id = ?""",
        (bounce_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"bounce not found: {bounce_id}")
    bounce = dict(row)
    bounce["signature"] = bounce_signature(row["bounce_message"])

    prior = conn.execute(
        """SELECT e.id, e.event_type, e.direction, e.channel, e.subject,
                  e.created_at, e.sender, c.name AS campaign_name,
                  substr(COALESCE(json_extract(e.metadata_json, '$.body'), ''), 1, 400) AS body_preview,
                  (json_extract(e.metadata_json, '$.body') IS NOT NULL) AS has_body
             FROM events e LEFT JOIN campaigns c ON c.id = e.campaign_id
            WHERE e.lead_id = ?
              AND lower(COALESCE(e.direction, '')) = 'outbound'
              AND e.created_at <= ?
            ORDER BY e.created_at DESC LIMIT ?""",
        (row["lead_id"], row["first_seen_at"], int(sends_before)),
    ).fetchall()

    def _siblings(where: str, params: tuple) -> list[dict]:
        return [dict(r) for r in conn.execute(
            f"""SELECT id, lead_email, sender_email, last_seen_at, bounce_message,
                       campaign_name
                  FROM bounce_events
                 WHERE id != ? AND {where}
                 ORDER BY last_seen_at DESC LIMIT 10""",
            (bounce_id, *params),
        ).fetchall()]

    # Three different questions, and the pane used to answer only the first.
    # "Other bounces in this campaign" tells you about the copy. "Other bounces
    # from this mailbox" tells you the mailbox is burnt. "Other bounces on this
    # sending domain" tells you the whole domain is -- which is the one that
    # decides whether you pause a sender or pause everything on that domain.
    sender_email = (row["sender_email"] or "").strip()
    sending_domain = sender_email.rpartition("@")[2].lower() if "@" in sender_email else ""

    same_campaign = (_siblings("campaign_id IS NOT NULL AND campaign_id = ?",
                               (row["campaign_id"],))
                     if row["campaign_id"] is not None else [])
    same_sender = (_siblings("lower(sender_email) = lower(?)", (sender_email,))
                   if sender_email else [])
    same_domain = (_siblings("lower(sender_email) LIKE ?", (f"%@{sending_domain}",))
                   if sending_domain else [])

    return {
        "bounce": bounce,
        "sends_before": [dict(r) for r in prior],
        "same_campaign": same_campaign,
        "same_sender": same_sender,
        "same_domain": same_domain,
        "sender_email": sender_email or None,
        "sending_domain": sending_domain or None,
    }


def bounce_trend(
    conn: sqlite3.Connection,
    *,
    mailbox: Optional[str] = None,
    domain: Optional[str] = None,
    days: int = 14,
) -> dict:
    """Daily sends and bounces plus the inputs to the trending-down rule.

    The UI renders this; it must never recompute the rule, or the flag and the
    explanation behind it can disagree.
    """
    if not mailbox and not domain:
        raise ValueError("mailbox or domain is required")
    if mailbox:
        bounce_where, bparams = "lower(sender_email) = lower(?)", [mailbox]
        send_where, sparams = "lower(e.sender) = lower(?)", [mailbox]
    else:
        bounce_where, bparams = "lower(sender_email) LIKE ?", [f"%@{str(domain).lower()}"]
        send_where, sparams = "lower(e.sender) LIKE ?", [f"%@{str(domain).lower()}"]

    bounce_rows = conn.execute(
        f"""SELECT date(last_seen_at) d, COALESCE(SUM(occurrence_count), COUNT(*)) n
              FROM bounce_events
             WHERE {bounce_where} AND last_seen_at >= datetime('now', ?)
             GROUP BY d ORDER BY d""",
        [*bparams, f"-{int(days)} days"],
    ).fetchall()
    send_rows = conn.execute(
        f"""SELECT date(e.created_at) d, COUNT(*) n
              FROM events e
             WHERE {send_where} AND lower(COALESCE(e.direction, '')) = 'outbound'
               AND e.created_at >= datetime('now', ?)
             GROUP BY d ORDER BY d""",
        [*sparams, f"-{int(days)} days"],
    ).fetchall()

    window = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN last_seen_at >= datetime('now', '-7 days')
                                THEN occurrence_count ELSE 0 END), 0) recent,
              COALESCE(SUM(CASE WHEN last_seen_at < datetime('now', '-7 days')
                                 AND last_seen_at >= datetime('now', '-14 days')
                                THEN occurrence_count ELSE 0 END), 0) prior
            FROM bounce_events WHERE {bounce_where}""",
        bparams,
    ).fetchone()

    bounces_by_day = {r["d"]: int(r["n"] or 0) for r in bounce_rows}
    sends_by_day = {r["d"]: int(r["n"] or 0) for r in send_rows}
    all_days = sorted(set(bounces_by_day) | set(sends_by_day))
    recent, prior = int(window["recent"] or 0), int(window["prior"] or 0)
    rule = None
    if recent > prior and recent > 0:
        rule = "rising_bounces"
    return {
        "subject": mailbox or domain,
        "scope": "mailbox" if mailbox else "domain",
        "days": days,
        "series": [{"date": d, "sends": sends_by_day.get(d, 0),
                    "bounces": bounces_by_day.get(d, 0)} for d in all_days],
        "bounces_last_7d": recent,
        "bounces_prior_7d": prior,
        "delta": recent - prior,
        "rule_fired": rule,
        "bounce_rate_threshold": MAILBOX_BOUNCE_RATE_THRESHOLD,
    }


def deliverability_vocab(conn: sqlite3.Connection) -> dict:
    """Provider and reseller values already in use, for pickers.

    No vocabulary table: the values ARE the data, and a lookup table would need
    its own CRUD, sync and cleanup to hold the same strings. The seeds cover the
    common platforms so the first domain someone edits still offers a choice.
    """
    seeds_provider = ["google_workspace", "microsoft_365", "smtp_relay", "zoho", "other"]
    providers = {r[0] for r in conn.execute(
        "SELECT DISTINCT provider FROM sender_domains WHERE provider IS NOT NULL"
        " AND TRIM(provider) != ''").fetchall()}
    providers |= {r[0] for r in conn.execute(
        "SELECT DISTINCT provider FROM sender_accounts WHERE provider IS NOT NULL"
        " AND TRIM(provider) != ''").fetchall()}
    resellers = {r[0] for r in conn.execute(
        "SELECT DISTINCT reseller FROM sender_domains WHERE reseller IS NOT NULL"
        " AND TRIM(reseller) != ''").fetchall()}
    # Deduped case-insensitively, with the value already in the data winning:
    # offering both GOOGLE_WORKSPACE and google_workspace is the exact confusion
    # the picker exists to remove.
    def _fold(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.strip().lower())

    def _merge(in_use: set, seeds: list) -> list:
        # In-use values always win, and a seed is dropped when the data already
        # says the same thing in a different shape -- MICROSOFT365 in the table
        # must not sit next to a seeded microsoft_365 in the same picker.
        used = {v.strip() for v in in_use if v and v.strip()}
        folded = {_fold(v) for v in used}
        extra = [v for v in seeds if v and v.strip() and _fold(v) not in folded]
        return sorted(used | set(extra), key=str.lower)

    return {
        "providers": _merge(providers, seeds_provider),
        "resellers": _merge(resellers, []),
        "in_use": {"providers": sorted(providers, key=str.lower),
                   "resellers": sorted(resellers, key=str.lower)},
    }
