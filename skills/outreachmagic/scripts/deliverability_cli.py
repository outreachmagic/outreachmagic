"""CLI for the deliverability reporting surface.

Everything the Deliverability page shows, the agent can ask for directly: the
domain and mailbox rosters with the same field names the dashboard receives,
the full blacklist report behind a listed/clean verdict, the bounce explorer
with its grouping, one bounce with the sends that preceded it, and the trend
series behind a "trending down" flag.

The dashboard is a second consumer of these functions, not the owner of them --
a capability only reachable by clicking is a capability an agent does not have.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import reporting_queries
from data_freshness import attach_freshness, print_freshness_stderr
from db_conn import get_conn


def register_campaign_replies_parser(sub) -> None:
    """`campaign-replies` — the Replies card, with its kinds separated.

    A bounce and an out-of-office both set a lead's sentiment, so a list built
    on sentiment alone reports all three as replies. --kind is how you ask for
    the one you meant; the counts for every kind come back either way.
    """
    p = sub.add_parser(
        "campaign-replies",
        help="Leads by current sentiment, split into replies / auto-replies / bounces",
    )
    p.add_argument("--workspace", help="Workspace slug (or set OM_WORKSPACE)")
    p.add_argument("--campaign-id", dest="campaign_id", type=int)
    p.add_argument("--since", help="Start of range: YYYY-MM-DD, 7d, 30d, today, all")
    p.add_argument("--until", help="End of range: YYYY-MM-DD")
    p.add_argument(
        "--kind", default="all",
        help="all (default) | human | auto_reply | bounce | status_only "
             "(comma-separate to combine)")
    p.add_argument("--sentiment")
    p.add_argument("--status-label", dest="status_label")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(command="campaign-replies")


def cmd_campaign_replies(args) -> None:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, getattr(args, "workspace", None))
        payload = reporting_queries.campaign_replies(
            conn, ws["id"],
            campaign_id=getattr(args, "campaign_id", None),
            since=getattr(args, "since", None), until=getattr(args, "until", None),
            limit=args.limit,
            sentiment=getattr(args, "sentiment", None),
            status_label=getattr(args, "status_label", None),
            reply_kind=getattr(args, "kind", "all"),
        )
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return
    finally:
        conn.close()
    payload["workspace"] = ws["slug"]

    def text(p):
        counts = p.get("kind_counts") or {}
        head = " · ".join(f"{n} {k.replace('_', '-')}" for k, n in sorted(
            counts.items(), key=lambda kv: -kv[1])) or "nothing in range"
        lines = [f"{sum(counts.values())} leads — {head}", ""]
        for r in p["replies"]:
            lines.append(
                f"  [{r['reply_kind']:<11}] {(r.get('lead_name') or '—')[:26]:<26} "
                f"{(r.get('email') or '—')[:34]:<34} {r.get('subject') or ''}"[:150])
        return "\n".join(lines)

    _emit(args, payload, text)


def register_campaign_audit_parser(sub) -> None:
    """`campaign-audit` — the Campaigns table, with its filters and its total.

    This is the command to reach for instead of hand-writing reply SQL. The
    reply condition is an OR nested inside an AND; written out by hand it loses
    its parentheses and the date filter gets swallowed by operator precedence,
    which is how a 30-day question quietly returns all-time totals.

        campaign-audit --workspace acme --since 30d --kind human --json

    is "positive human replies in the last 30 days, by campaign" -- read
    `positive` off each row, and `totals` for the whole workspace.
    """
    p = sub.add_parser(
        "campaign-audit",
        help="Per-campaign sends, replies, sentiments, meetings and rates, with a total",
    )
    p.add_argument("--workspace", help="Workspace slug (or set OM_WORKSPACE)")
    p.add_argument("--since", help="Start of range: YYYY-MM-DD, 7d, 30d, today, all")
    p.add_argument("--until", help="End of range: YYYY-MM-DD")
    p.add_argument(
        "--kind", default="all",
        help="all (default) | human | auto_reply | bounce | status_only "
             "(comma-separate to combine). Narrows replies and the sentiment "
             "columns only -- sends are always counted in full")
    p.add_argument("--tag", "--tags-any", dest="tags_any", action="append",
                   help="Only leads carrying any of these tags (repeatable)")
    p.add_argument("--tags-all", dest="tags_all", action="append",
                   help="Only leads carrying all of these tags (repeatable)")
    p.add_argument("--exclude-tag", dest="tags_none", action="append",
                   help="Drop leads carrying this tag, from every column (repeatable)")
    p.add_argument("--include-scheduling", action="store_true",
                   help="Include booking-tool calendars as campaigns")
    p.add_argument("--positive-anchor", dest="positive_anchor",
                   choices=("current", "first"), default="first",
                   help="first (default): dated from when a lead FIRST turned "
                        "positive, so history does not move. current: leads "
                        "positive right now, dated from the start of that run")
    p.add_argument("--sort", default="sent",
                   choices=("sent", "replies", "positive", "meetings", "bounces",
                            "reply_rate", "positive_rate", "bounce_rate", "name"),
                   help="Sort key (default: sent)")
    p.add_argument("--limit", type=int, default=0, help="Max rows (0 = all)")
    p.add_argument("--by-day", dest="by_day", action="store_true",
                   help="Attach a per-day sent/replies/bounces/meetings array "
                        "to each campaign. Use this instead of writing the "
                        "breakdown as SQL -- same window, same definitions.")
    p.add_argument("--compare-previous-window", dest="compare", action="store_true",
                   help="Also return the same window immediately before this "
                        "one, per campaign and in total (needs --since)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(command="campaign-audit")


def cmd_campaign_audit(args) -> None:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, getattr(args, "workspace", None))
        audit = (reporting_queries.campaign_audit_compared
                 if getattr(args, "compare", False) else reporting_queries.campaign_audit)
        payload = audit(
            conn, ws["id"],
            since=getattr(args, "since", None), until=getattr(args, "until", None),
            include_scheduling=bool(getattr(args, "include_scheduling", False)),
            positive_anchor=getattr(args, "positive_anchor", None),
            reply_kind=getattr(args, "kind", "all"),
            tags_any=getattr(args, "tags_any", None),
            tags_all=getattr(args, "tags_all", None),
            tags_none=getattr(args, "tags_none", None),
            by_day=bool(getattr(args, "by_day", False)),
        )
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return
    finally:
        conn.close()
    payload["workspace"] = ws["slug"]
    rows = _sorted(payload["campaigns"], args.sort,
                   reverse=args.sort != "name")
    if getattr(args, "limit", 0):
        rows = rows[:args.limit]
    payload["campaigns"] = rows

    def text(p):
        t = p["totals"]
        head = (f"{t['campaigns']} campaigns · {t['sent']} sent · "
                f"{t['replies']} replies · {t['positive']} positive")
        scope = []
        if p.get("reply_kind") not in (None, "all"):
            scope.append(f"kind={p['reply_kind']} (replies & sentiments only)")
        for key, label in (("tags_any", "tags"), ("tags_all", "tags-all"),
                           ("tags_none", "excluding")):
            if p.get(key):
                scope.append(f"{label}={','.join(p[key])}")
        lines = [head + (f"  [{'; '.join(scope)}]" if scope else ""), ""]
        fmt = "  {:<34} {:>7} {:>7} {:>8} {:>8} {:>7} {:>7} {:>7}"
        lines.append(fmt.format("CAMPAIGN", "SENT", "REPLIES", "POSITIVE",
                                "MEETINGS", "BOUNCE", "RPLY%", "POS%"))
        for r in p["campaigns"]:
            lines.append(fmt.format(
                (r["name"] or "—")[:34], r["sent"], r["replies"], r["positive"],
                r["meetings"], r["bounces"],
                _fmt_pct(r["reply_rate"]), _fmt_pct(r["positive_rate"])))
            if r.get("previous") is not None:
                prev = r["previous"]
                lines.append(fmt.format(
                    "  ↳ previous window", prev["sent"], prev["replies"],
                    prev["positive"], prev["meetings"], prev["bounces"],
                    _fmt_pct(prev["reply_rate"]), _fmt_pct(prev["positive_rate"])))
            for d in r.get("daily") or []:
                lines.append(f"      {d['date']}  {d['sent']:>6} sent"
                             f"  {d['replies']:>4} rply  {d['bounces']:>4} bnc"
                             f"  {d['meetings']:>3} mtg")
        # Straight from the payload, never re-summed here: one definition of the
        # total, whatever is reading it.
        lines += ["", fmt.format(
            "TOTAL", t["sent"], t["replies"], t["positive"], t["meetings"],
            t["bounces"], _fmt_pct(t["reply_rate"]), _fmt_pct(t["positive_rate"]))]
        return "\n".join(lines)

    _emit(args, payload, text)


def register_deliverability_parser(sub) -> None:
    p = sub.add_parser(
        "deliverability",
        help="Sending domains, mailboxes, bounces and DNSBL detail (agent-readable)",
    )
    dsub = p.add_subparsers(dest="deliverability_cmd")

    def add_range(target, *, workspace_required=True):
        target.add_argument("--workspace", required=workspace_required,
                            help="Workspace slug (or set OM_WORKSPACE)")
        target.add_argument("--since", help="Start of range: YYYY-MM-DD, 7d, 30d, today, all")
        target.add_argument("--until", help="End of range: YYYY-MM-DD")
        target.add_argument("--json", action="store_true", help="JSON output for agents")
        target.add_argument("--quiet", action="store_true",
                            help="Suppress the stderr freshness line")

    domains_p = dsub.add_parser(
        "domains", help="Sending domains: sends, rates, provider, reseller, age, DNSBL")
    add_range(domains_p)
    domains_p.add_argument("--sort", default="sends",
                           choices=("sends", "domain", "bounce_rate", "reply_rate",
                                    "mailboxes", "age"),
                           help="Sort key (default: sends)")

    mailboxes_p = dsub.add_parser(
        "mailboxes", help="Mailboxes: sends, rates, provider, the domain's reseller, trend flag")
    add_range(mailboxes_p)
    mailboxes_p.add_argument("--domain", help="Only mailboxes on this sending domain")
    mailboxes_p.add_argument("--flagged", action="store_true",
                             help="Only mailboxes flagged as trending down")
    mailboxes_p.add_argument("--sort", default="sends",
                             choices=("sends", "email", "bounce_rate", "reply_rate",
                                      "last_send"),
                             help="Sort key (default: sends)")

    vocab_p = dsub.add_parser(
        "vocab", help="Provider and reseller values already in use (for pickers)")
    vocab_p.add_argument("--json", action="store_true")
    vocab_p.add_argument("--quiet", action="store_true")

    trend_p = dsub.add_parser(
        "trend", help="Daily sends/bounces and the inputs behind a trending-down flag")
    trend_p.add_argument("--mailbox")
    trend_p.add_argument("--domain")
    trend_p.add_argument("--days", type=int, default=14)
    trend_p.add_argument("--json", action="store_true")
    trend_p.add_argument("--quiet", action="store_true")

    bounces_p = dsub.add_parser(
        "bounces", help="Bounce explorer: grouped failures over the full stored history")
    add_range(bounces_p, workspace_required=False)
    bounces_p.add_argument("--domain")
    bounces_p.add_argument("--mailbox")
    bounces_p.add_argument("--campaign-id", dest="campaign_id", type=int)
    bounces_p.add_argument("--bounce-type", dest="bounce_type",
                           choices=("hard", "soft", "unknown"))
    bounces_p.add_argument("--group-by", dest="group_by", default="signature",
                           choices=("signature", "type", "mailbox", "domain", "campaign", "none"),
                           help="How to group (default: signature)")
    bounces_p.add_argument("--limit", type=int, default=200)
    bounces_p.add_argument("--offset", type=int, default=0)

    detail_p = dsub.add_parser(
        "bounce-detail", help="One bounce: full message plus the sends that preceded it")
    detail_p.add_argument("--id", dest="bounce_id", required=True)
    detail_p.add_argument("--sends-before", dest="sends_before", type=int, default=5)
    detail_p.add_argument("--json", action="store_true")
    detail_p.add_argument("--quiet", action="store_true")

    blacklist_p = dsub.add_parser(
        "blacklist", help="Stored DNSBL report per domain: every list, verdict and check time")
    blacklist_p.add_argument("--domain")
    blacklist_p.add_argument("--stale-hours", dest="stale_hours", type=int)
    blacklist_p.add_argument("--json", action="store_true")
    blacklist_p.add_argument("--quiet", action="store_true")

    p.set_defaults(command="deliverability")


def _resolve_workspace(conn, slug: Optional[str]) -> dict:
    if not slug:
        raise ValueError("--workspace is required (or set OM_WORKSPACE)")
    row = conn.execute(
        "SELECT id, slug FROM workspaces WHERE id = ? OR slug = ? OR name = ?"
        " ORDER BY (id = ?) DESC LIMIT 1",
        (slug, slug, slug, slug),
    ).fetchone()
    if row is None:
        available = [r[0] for r in conn.execute(
            "SELECT slug FROM workspaces ORDER BY slug").fetchall()]
        raise ValueError(f"Unknown workspace: {slug}. Available: {', '.join(available) or 'none'}")
    return {"id": row[0], "slug": row[1]}


def _emit(args, payload, text_fn) -> None:
    import pipeline as _pipeline

    last_pull = _pipeline.get_last_pull()
    if not (getattr(args, "quiet", False) or __import__("os").environ.get("OM_QUIET")):
        print_freshness_stderr(last_pull)
    if getattr(args, "json", False):
        print(json.dumps(attach_freshness(payload, last_pull=last_pull), indent=2))
    else:
        print(text_fn(payload))


def _fmt_pct(value) -> str:
    return f"{value * 100:.1f}%" if value is not None else "–"


def _sorted(rows: list, key: str, reverse: bool = True) -> list:
    return sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=reverse)


def cmd_deliverability(args) -> None:
    action = getattr(args, "deliverability_cmd", None)
    if not action:
        print("Usage: pipeline.py deliverability <domains|mailboxes|bounces|bounce-detail"
              "|blacklist|trend|vocab> [--workspace SLUG] --json", file=sys.stderr)
        sys.exit(2)

    try:
        conn = get_conn()
        try:
            payload, text_fn = _dispatch(args, action, conn)
        finally:
            conn.close()
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return
    _emit(args, payload, text_fn)


def _dispatch(args, action: str, conn):
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)

    if action == "domains":
        ws = _resolve_workspace(conn, getattr(args, "workspace", None))
        data = reporting_queries.domain_health(conn, ws["id"], since=since, until=until)
        key = {"age": "domain_age_days", "domain": "domain"}.get(args.sort, args.sort)
        rows = _sorted(data["domains"], key, reverse=(key != "domain"))
        payload = {"workspace": ws["slug"], "since": since or "all", "until": until,
                   "domains": rows, "flagged": data["flagged"]}

        def text(p):
            lines = [f"{'DOMAIN':<32} {'SENDS':>7} {'REPLY%':>7} {'BOUNCE%':>8} "
                     f"{'MAILBOX':>7}  {'PROVIDER':<18} {'RESELLER':<18} {'AGE':>6}  BLACKLIST"]
            for d in p["domains"]:
                age = f"{d['domain_age_days']}d" if d.get("domain_age_days") is not None else "–"
                state = ("listed" if d["listed"] else
                         ("clean" if d["registered"] else "not monitored"))
                lines.append(
                    f"{d['domain'][:32]:<32} {d['sends']:>7} {_fmt_pct(d['reply_rate']):>7} "
                    f"{_fmt_pct(d['bounce_rate']):>8} {d['mailboxes']:>7}  "
                    f"{(d.get('provider') or '–')[:18]:<18} {(d.get('reseller') or '–')[:18]:<18} "
                    f"{age:>6}  {state}")
            return "\n".join(lines)
        return payload, text

    if action == "mailboxes":
        ws = _resolve_workspace(conn, getattr(args, "workspace", None))
        data = reporting_queries.mailbox_health(conn, ws["id"], since=since, until=until)
        rows = data["mailboxes"]
        if getattr(args, "domain", None):
            rows = [m for m in rows if m.get("email_domain") == args.domain]
        if getattr(args, "flagged", False):
            rows = [m for m in rows if m.get("trending_down")]
        key = {"email": "email", "last_send": "last_outbound_at"}.get(args.sort, args.sort)
        rows = _sorted(rows, key, reverse=(key != "email"))
        payload = {"workspace": ws["slug"], "since": since or "all", "until": until,
                   "mailboxes": rows, "flagged": data["flagged"]}

        def text(p):
            lines = [f"{'MAILBOX':<38} {'PROVIDER':<16} {'RESELLER':<16} {'SENDS':>7} "
                     f"{'REPLY%':>7} {'BOUNCE%':>8}  STATUS"]
            for m in p["mailboxes"]:
                lines.append(
                    f"{m['email'][:38]:<38} {(m.get('provider') or '–')[:16]:<16} "
                    f"{(m.get('domain_reseller') or '–')[:16]:<16} {m['sends']:>7} "
                    f"{_fmt_pct(m['reply_rate']):>7} {_fmt_pct(m['bounce_rate']):>8}  "
                    f"{'trending down' if m['trending_down'] else 'ok'}")
            return "\n".join(lines)
        return payload, text

    if action == "vocab":
        payload = reporting_queries.deliverability_vocab(conn)

        def text(p):
            return ("providers: " + (", ".join(p["providers"]) or "–")
                    + "\nresellers: " + (", ".join(p["resellers"]) or "–"))
        return payload, text

    if action == "trend":
        payload = reporting_queries.bounce_trend(
            conn, mailbox=getattr(args, "mailbox", None),
            domain=getattr(args, "domain", None), days=args.days)

        def text(p):
            head = (f"{p['subject']} — {p['bounces_last_7d']} bounces in the last 7d vs "
                    f"{p['bounces_prior_7d']} the 7d before (delta {p['delta']:+d})")
            rule = f"\nrule fired: {p['rule_fired'] or 'none'}"
            rows = "\n".join(
                f"  {d['date']}  sends {d['sends']:>5}  bounces {d['bounces']:>5}"
                for d in p["series"])
            return f"{head}{rule}\n{rows}"
        return payload, text

    if action == "bounces":
        workspace_id = None
        if getattr(args, "workspace", None):
            workspace_id = _resolve_workspace(conn, args.workspace)["id"]
        payload = reporting_queries.bounce_explorer(
            conn, workspace_id, since=since, until=until,
            domain=getattr(args, "domain", None), mailbox=getattr(args, "mailbox", None),
            campaign_id=getattr(args, "campaign_id", None),
            bounce_type=getattr(args, "bounce_type", None),
            group_by=(None if args.group_by == "none" else args.group_by),
            limit=args.limit, offset=args.offset)

        def text(p):
            lines = [f"{p['total']} bounces ({p['total_occurrences']} occurrences)"]
            if p["groups"]:
                lines.append(f"\ngrouped by {p['group_by']}:")
                for g in p["groups"]:
                    lines.append(f"  {g['bounces']:>6}  {g['key']}")
            return "\n".join(lines)
        return payload, text

    if action == "bounce-detail":
        payload = reporting_queries.bounce_detail(
            conn, args.bounce_id, sends_before=args.sends_before)

        def text(p):
            b = p["bounce"]
            lines = [f"{b.get('lead_email')} <- {b.get('sender_email')}  "
                     f"[{b.get('bounce_type')}] {b.get('smtp_code') or ''}".rstrip(),
                     f"campaign: {b.get('campaign_name') or '–'}",
                     f"last seen: {b.get('last_seen_at')}  "
                     f"occurrences: {b.get('occurrence_count')}",
                     "", "message:", (b.get("bounce_message") or "(none sent by the platform)"),
                     "", f"sends before the bounce ({len(p['sends_before'])}):"]
            for e in p["sends_before"]:
                lines.append(f"  {e['created_at']}  {e.get('subject') or '(no subject)'}")
            return "\n".join(lines)
        return payload, text

    if action == "blacklist":
        from pipeline_sender_accounts import blacklist_status_report

        payload = blacklist_status_report(
            getattr(args, "domain", None), stale_hours=getattr(args, "stale_hours", None))

        def text(p):
            lines = [f"clean {p['counts']['clean']} · listed {p['counts']['listed']} · "
                     f"unchecked {p['counts']['unchecked']}"]
            for d in p["domains"]:
                lines.append(f"\n{d['domain']} — {d['state']}"
                             f" (checked {d.get('checked_at') or 'never'}"
                             f"{', partial' if d.get('partial') else ''})")
                for r in d.get("results") or []:
                    addr = ", ".join(r.get("addresses") or []) or "–"
                    lines.append(f"    {r.get('name'):<18} {r.get('status'):<13} {addr}")
            return "\n".join(lines)
        return payload, text

    raise ValueError(f"unknown deliverability action: {action}")
