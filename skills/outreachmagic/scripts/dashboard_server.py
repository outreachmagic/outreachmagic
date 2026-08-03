#!/usr/bin/env python3
"""Local web dashboard: stdlib HTTP server over the skill's own query/action layers.

Zero third-party dependencies (ThreadingHTTPServer + a route table). One
SQLite connection per request via db_conn.get_conn (WAL, busy_timeout).

Not authenticated — bind 127.0.0.1 (default) on trusted machines only. The
Host-header check blocks DNS-rebinding and the X-OM-Dashboard header blocks
cross-site form POSTs; neither is authentication.

Run: python3 scripts/pipeline.py dashboard   (or python3 scripts/dashboard_server.py)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

if __package__ is None and str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import dashboard_actions
import dashboard_queries
from db_conn import database_has_schema, format_database_recovery_message, get_conn
from workspace_routing import IdentityConflict, resolve_workspace_identity

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 64 * 1024
CSRF_HEADER = "X-OM-Dashboard"

_HTML_PATH = Path(__file__).parent / "dashboard.html"


# ---------------------------------------------------------------------------
# Handlers: (match, query, body) -> (status, payload). No HTTP objects inside.
# ---------------------------------------------------------------------------

def _q(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default

def _int_q(query: dict, name: str, default: int, lo: int = 0, hi: int = 500) -> int:
    try:
        return max(lo, min(hi, int(_q(query, name, default))))
    except (TypeError, ValueError):
        return default


def _resolve_workspace(conn: sqlite3.Connection, slug) -> dict:
    if not slug:
        raise ValueError("workspace query parameter is required")
    ws = resolve_workspace_identity(conn, slug)
    if not ws:
        raise ValueError(f"workspace not found: {slug}")
    return ws


def _workspace_scoped(fn):
    """GET handler wrapper: opens a connection, resolves ?workspace=slug."""
    def handler(match, query, body):
        conn = get_conn()
        try:
            ws = _resolve_workspace(conn, _q(query, "workspace"))
            return 200, fn(conn, ws["id"], match, query)
        finally:
            conn.close()
    return handler


def handle_workspaces(match, query, body):
    conn = get_conn()
    try:
        return 200, dashboard_queries.list_workspaces(conn)
    finally:
        conn.close()


@_workspace_scoped
def handle_summary(conn, ws_id, match, query):
    return dashboard_queries.summary(
        conn, ws_id, since=_q(query, "since", "30d"), until=_q(query, "until"))


@_workspace_scoped
def handle_deliverability(conn, ws_id, match, query):
    since, until = _q(query, "since", "30d"), _q(query, "until")
    series = dashboard_queries.bounce_series(conn, ws_id, since=since, until=until)
    # Mailbox / domain reply%/bounce% are live from events, scoped to the range.
    mailboxes = dashboard_queries.mailbox_health(conn, ws_id, since=since, until=until)
    domains = dashboard_queries.domain_health(conn, ws_id, since=since, until=until)
    return {
        "since": series["since"],
        "series": series["series"],
        "mailboxes": mailboxes["mailboxes"],
        "domains": domains["domains"],
        "flagged_mailboxes": mailboxes["flagged"],
        "flagged_domains": domains["flagged"],
    }


@_workspace_scoped
def handle_domain_detail(conn, ws_id, match, query):
    domain = _q(query, "domain")
    if not domain:
        raise ValueError("domain query parameter is required")
    return dashboard_queries.domain_detail(conn, ws_id, domain)


@_workspace_scoped
def handle_companies(conn, ws_id, match, query):
    return dashboard_queries.search_companies(
        conn, ws_id, q=_q(query, "q"),
        sort=_q(query, "sort", "leads"),
        direction=_q(query, "dir"),
        limit=_int_q(query, "limit", 50, lo=1, hi=200),
        offset=_int_q(query, "offset", 0, hi=10_000_000),
        tag=_q(query, "tag"),
        missing_domain=_bool_q(query, "missing_domain"),
        no_reachable_contact=_bool_q(query, "no_reachable_contact"),
        placeholder_only=_bool_q(query, "placeholder_only"))


@_workspace_scoped
def handle_companies_stats(conn, ws_id, match, query):
    return dashboard_queries.companies_stats(conn, ws_id)


@_workspace_scoped
def handle_company_detail(conn, ws_id, match, query):
    return dashboard_queries.company_detail(conn, ws_id, int(match.group(1)))


@_workspace_scoped
def handle_company_contact_activity(conn, ws_id, match, query):
    return dashboard_queries.company_contact_activity(
        conn, ws_id, int(match.group(1)),
        limit=_int_q(query, "limit", 200, lo=1, hi=500))


def handle_company_search(match, query, body):
    q = _q(query, "q")
    if not q:
        return 200, {"companies": []}
    conn = get_conn()
    try:
        return 200, dashboard_queries.company_search_for_link(
            conn, q, limit=_int_q(query, "limit", 10, lo=1, hi=50))
    finally:
        conn.close()


def handle_merge_candidates(match, query, body):
    import pipeline as _pipeline

    result = _pipeline.list_company_merge_candidates(
        status=_q(query, "status", "pending"),
        reason=_q(query, "reason"),
        min_confidence=(_q(query, "min_confidence") or "ALL").upper(),
        limit=_int_q(query, "limit", 50, lo=1, hi=200),
        offset=_int_q(query, "offset", 0, hi=1_000_000))
    return 200, result


def handle_update_company(match, query, body):
    body = body or {}
    return 200, dashboard_actions.update_company(int(match.group(1)), body)


def handle_link_company(match, query, body):
    body = body or {}
    cid = body.get("company_id")
    return 200, dashboard_actions.link_company(
        int(match.group(1)),
        company_id=int(cid) if cid else None,
        company_name=body.get("company_name"))


def handle_company_domains(match, query, body):
    """A company's own domain set, from company_identities — NOT sender_domains,
    which is your sending infrastructure and is unrelated to this company."""
    conn = get_conn()
    try:
        company_id = int(match.group(1))
        detail = dashboard_queries.company_detail(
            conn, _resolve_workspace(conn, _q(query, "workspace"))["id"], company_id)
        return 200, {"company_id": company_id, "domains": detail["domains"]}
    finally:
        conn.close()


def handle_set_company_domain(match, query, body):
    return 200, dashboard_actions.company_domain_action(int(match.group(1)), body or {})


def handle_edit_sender(match, query, body):
    body = body or {}
    email = body.pop("email", None)
    return 200, dashboard_actions.edit_sender_account(email, body)


def handle_edit_domain(match, query, body):
    body = body or {}
    domain = body.pop("domain", None)
    return 200, dashboard_actions.edit_sender_domain(domain, body)


def handle_delete_company(match, query, body):
    body = body or {}
    return 200, dashboard_actions.delete_company(
        int(match.group(1)), confirm=bool(body.get("confirm")))


def handle_delete_sender(match, query, body):
    body = body or {}
    return 200, dashboard_actions.delete_sender_account(
        int(body.get("id") or 0), confirm=bool(body.get("confirm")))


def handle_delete_domain(match, query, body):
    body = body or {}
    return 200, dashboard_actions.delete_sender_domain(
        body.get("domain"), confirm=bool(body.get("confirm")))


def handle_resolve_merge(match, query, body):
    body = body or {}
    keep_id = body.get("keep_id")
    return 200, dashboard_actions.resolve_merge_candidate(
        match.group(1), approve=bool(body.get("approve")), note=body.get("note"),
        keep_id=int(keep_id) if keep_id else None)


def handle_resolve_merges_bulk(match, query, body):
    body = body or {}
    return 200, dashboard_actions.resolve_merge_candidates_bulk(
        body.get("ids") or [], approve=bool(body.get("approve")),
        keep=body.get("keep") or "existing", note=body.get("note"))


@_workspace_scoped
def handle_pipeline(conn, ws_id, match, query):
    return dashboard_queries.pipeline_counts(
        conn, ws_id, since=_q(query, "since"), until=_q(query, "until"))


@_workspace_scoped
def handle_pipeline_leads(conn, ws_id, match, query):
    status = _q(query, "status")
    if not status:
        raise ValueError("status query parameter is required")
    return dashboard_queries.pipeline_leads(
        conn, ws_id, status,
        limit=_int_q(query, "limit", 50, lo=1),
        offset=_int_q(query, "offset", 0, hi=1_000_000),
        since=_q(query, "since"), until=_q(query, "until"))


@_workspace_scoped
def handle_attributes(conn, ws_id, match, query):
    campaign_id = _q(query, "campaign_id")
    return dashboard_queries.attribute_performance(
        conn, ws_id,
        field=_q(query, "field", "industry"),
        min_sample=_int_q(query, "min_sample", 20, lo=1, hi=10_000),
        campaign_id=int(campaign_id) if campaign_id else None)


@_workspace_scoped
def handle_campaigns(conn, ws_id, match, query):
    return dashboard_queries.campaign_audit(
        conn, ws_id, since=_q(query, "since"), until=_q(query, "until"))


@_workspace_scoped
def handle_campaign_daily(conn, ws_id, match, query):
    campaign_id = _q(query, "campaign_id")
    return dashboard_queries.campaign_daily(
        conn, ws_id,
        campaign_id=int(campaign_id) if campaign_id else None,
        since=_q(query, "since", "30d"), until=_q(query, "until"))


@_workspace_scoped
def handle_campaign_detail(conn, ws_id, match, query):
    campaign_id = _q(query, "campaign_id")
    if not campaign_id:
        raise ValueError("campaign_id query parameter is required")
    return dashboard_queries.campaign_detail(
        conn, ws_id, int(campaign_id),
        since=_q(query, "since"), until=_q(query, "until"))


def _bool_q(query, name):
    v = _q(query, name)
    return str(v).lower() in ("1", "true", "yes") if v is not None else None


def _list_q(query: dict, name: str):
    """Repeated params (?tags_any=a&tags_any=b) or one comma-joined value.

    Both forms occur: the fetch layer builds repeated params, hand-written
    URLs and the CLI tend to comma-join.
    """
    values = query.get(name) or []
    out = []
    for value in values:
        out.extend(part.strip() for part in str(value).split(",") if part.strip())
    return out or None


def _lead_filters_from(query: dict) -> dict:
    """The contacts filter set, read once.

    Every surface that means "the contacts you are currently looking at" —
    the list, the id selection behind "select all N matching", the stat tiles
    and the CSV export — parses its filters here. They used to parse them in
    four places, which is how an export drifts from the list it claims to
    mirror. The keys line up 1:1 with dashboard_queries.LEAD_FILTER_KEYS.
    """
    campaign_id = _q(query, "campaign_id")
    return {
        "q": _q(query, "q"), "status": _q(query, "status"),
        "campaign_id": int(campaign_id) if campaign_id else None,
        "missing": _q(query, "missing"),
        "since": _q(query, "since"), "until": _q(query, "until"),
        "tag": _q(query, "tag"),
        "tags_any": _list_q(query, "tags_any"),
        "tags_all": _list_q(query, "tags_all"),
        "tags_none": _list_q(query, "tags_none"),
        "connected": _bool_q(query, "connected"),
        "sender": _q(query, "sender"),
        "has_linkedin": _bool_q(query, "has_linkedin"),
        "verify": _q(query, "verify"),
        "qualify_finding": _bool_q(query, "qualify_finding"),
        "has_domain": _bool_q(query, "has_domain"),
        "record_type": _q(query, "record_type"),
        # Absent means exclude. The safe answer is the one you get by default.
        "suppressed": _q(query, "suppressed"),
    }


@_workspace_scoped
def handle_contacts(conn, ws_id, match, query):
    return dashboard_queries.search_leads(
        conn, ws_id,
        **_lead_filters_from(query),
        sort=_q(query, "sort", "last_activity"),
        direction=_q(query, "dir"),
        limit=_int_q(query, "limit", 50, lo=1, hi=200),
        offset=_int_q(query, "offset", 0, hi=10_000_000))


@_workspace_scoped
def handle_contacts_ids(conn, ws_id, match, query):
    # Every lead_id matching the current contacts filter, for "select all N
    # matching" across pages. Capped so a runaway filter can't return the world.
    return dashboard_queries.search_leads(
        conn, ws_id,
        **_lead_filters_from(query),
        limit=_int_q(query, "limit", 5000, lo=1, hi=50000),
        ids_only=True)




@_workspace_scoped
def handle_contacts_export_fields(conn, ws_id, match, query):
    import lead_export
    return lead_export.export_field_options(conn, ws_id)


def handle_contacts_export(match, query, body):
    """Server-side CSV: writes to the export dir and returns the path. The
    browser gets a download link, not 100k rows to assemble into a Blob.

    Filters ride the URL query string — the same keys, read by the same helpers,
    as GET /api/contacts. The body carries the column choice, and optionally an
    explicit `lead_ids` selection. That way "export what's on screen" cannot
    drift from what's on screen.

    `lead_ids` in the body *replaces* the filters rather than narrowing them:
    a tick list is a literal answer to "which rows", and re-applying the filters
    on top of it would quietly drop rows whose filter state changed after they
    were ticked. Ids go in the body because 50k of them do not fit in a URL.
    """
    import lead_export

    body = body or {}
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, _q(query, "workspace"))
        lead_ids = body.get("lead_ids")
        if lead_ids is not None:
            filters = {"lead_ids": _parse_lead_ids(lead_ids), "record_type": "all"}
        else:
            filters = _lead_filters_from(query)
        try:
            return 200, lead_export.export_to_csv(
                conn, ws["id"],
                workspace_slug=ws.get("slug"),
                preset=body.get("preset"),
                fields=body.get("fields") or None,
                limit=max(1, min(int(body.get("limit") or 50000), 200000)),
                **filters)
        except lead_export.LeadExportError as exc:
            return 400, {"error": str(exc)}
    finally:
        conn.close()


@_workspace_scoped
def handle_contacts_stats(conn, ws_id, match, query):
    # Same filters as the list, so a tile and the rows under it always agree.
    return dashboard_queries.contacts_stats(conn, ws_id, **_lead_filters_from(query))


@_workspace_scoped
def handle_tags(conn, ws_id, match, query):
    # Existing workspace tags, for the bulk add/remove tag type-ahead.
    import pipeline_tags
    return {"tags": pipeline_tags.tag_list(ws_id, conn=conn)}


@_workspace_scoped
def handle_linkedin_senders(conn, ws_id, match, query):
    # LinkedIn sender accounts + their connection counts, for the contacts
    # 1st-degree sender picker.
    import pipeline_tags
    return pipeline_tags.linkedin_status_summary(ws_id, conn=conn)


def handle_contacts_bulk(match, query, body):
    body = body or {}
    return 200, dashboard_actions.bulk_edit_contacts(
        body.get("lead_ids") or [], body.get("op") or "",
        value=body.get("value"),
        workspace_slug=body.get("workspace"),
        force=bool(body.get("force")))


@_workspace_scoped
def handle_suppression_list(conn, ws_id, match, query):
    import suppression
    return {
        "entries": suppression.list_entries(
            conn, workspace_id=ws_id,
            entry_type=_q(query, "type"), reason=_q(query, "reason"),
            include_revoked=bool(_bool_q(query, "include_revoked"))),
        "stats": suppression.stats(conn, ws_id),
        "entry_types": list(suppression.ENTRY_TYPES),
        "reasons": list(suppression.REASONS),
    }


def handle_suppression_add(match, query, body):
    import suppression
    body = body or {}
    conn = get_conn()
    try:
        # org_wide is opt-in and explicit. The default is workspace scope,
        # because a rule that quietly reaches every client's list is not
        # something anyone should be able to create by omission.
        ws_id = (None if body.get("org_wide")
                 else _resolve_workspace(conn, body.get("workspace"))["id"])
        rows = body.get("entries")
        if rows:
            return 200, suppression.add_entries_bulk(
                conn, rows, workspace_id=ws_id,
                default_reason=body.get("reason") or suppression.DEFAULT_REASON,
                source="ui")
        return 200, suppression.add_entry(
            conn, entry_type=body.get("type") or "", value=body.get("value"),
            workspace_id=ws_id,
            reason=body.get("reason") or suppression.DEFAULT_REASON,
            note=body.get("note"), source="ui")
    except suppression.SuppressionError as exc:
        return 400, {"error": str(exc)}
    finally:
        conn.close()


def handle_suppression_remove(match, query, body):
    import suppression
    body = body or {}
    conn = get_conn()
    try:
        ws_id = (None if body.get("org_wide")
                 else _resolve_workspace(conn, body.get("workspace"))["id"])
        return 200, suppression.revoke_entry(
            conn, entry_type=body.get("type") or "", value=body.get("value"),
            workspace_id=ws_id)
    except suppression.SuppressionError as exc:
        return 400, {"error": str(exc)}
    finally:
        conn.close()


@_workspace_scoped
def handle_data_quality(conn, ws_id, match, query):
    return dashboard_queries.data_quality(
        conn, ws_id, since=_q(query, "since"), until=_q(query, "until"))


def _parse_lead_ids(raw) -> list:
    """Lead ids from either a query-string blob ("1,2 3") or a JSON body list."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw if str(x).strip().lstrip("-").isdigit()]
    return [int(x) for x in str(raw).replace(",", " ").split() if x.strip().isdigit()]


@_workspace_scoped
def handle_enrich_targets(conn, ws_id, match, query):
    return dashboard_queries.enrichment_targets(
        conn, ws_id, _parse_lead_ids(_q(query, "lead_ids")))


def handle_bulk_link_company(match, query, body):
    body = body or {}
    return 200, dashboard_actions.bulk_link_companies(body.get("lead_ids") or [])


def handle_cleanup_preview(match, query, body):
    return 200, dashboard_actions.cleanup_preview()


def handle_cleanup_run(match, query, body):
    return 200, dashboard_actions.cleanup_run()


def _empty_leads_scope(value) -> Optional[str]:
    """`scope=org` (or an absent workspace) means org-wide.

    Explicit, because the difference is 10k rows vs a few hundred and the two
    read identically in a URL otherwise.
    """
    return None if str(value or "").lower() in ("", "org", "all") else value


def handle_empty_leads_preview(match, query, body):
    return 200, dashboard_actions.empty_leads_preview(
        _empty_leads_scope(_q(query, "workspace")))


def handle_empty_leads_run(match, query, body):
    return 200, dashboard_actions.empty_leads_run(
        _empty_leads_scope((body or {}).get("workspace")))


def handle_email_finder(match, query, body):
    body = body or {}
    status = dashboard_actions.sync_manager.start_email_finder(
        body.get("workspace") or "",
        body.get("lead_ids") or [],
        domains=body.get("domains"),
        force=bool(body.get("force")),
        providers=body.get("providers"))
    if status is None:
        return 409, {"error": "a sync is already running"}
    return 202, status


def handle_serper(match, query, body):
    body = body or {}
    status = dashboard_actions.sync_manager.start_serper(
        body.get("workspace") or "", body.get("lead_ids") or [],
        force=bool(body.get("force")), deep=bool(body.get("deep")))
    if status is None:
        return 409, {"error": "a sync is already running"}
    return 202, status


@_workspace_scoped
def handle_campaign_leads(conn, ws_id, match, query):
    campaign_id = _q(query, "campaign_id")
    if not campaign_id:
        raise ValueError("campaign_id query parameter is required")
    return dashboard_queries.campaign_leads(
        conn, ws_id, int(campaign_id),
        q=_q(query, "q"), since=_q(query, "since"), until=_q(query, "until"),
        sort=_q(query, "sort", "last_activity"),
        direction=_q(query, "dir"),
        limit=_int_q(query, "limit", 50, lo=1, hi=200),
        offset=_int_q(query, "offset", 0, hi=10_000_000))


@_workspace_scoped
def handle_campaign_subjects(conn, ws_id, match, query):
    campaign_id = _q(query, "campaign_id")
    if not campaign_id:
        raise ValueError("campaign_id query parameter is required")
    return dashboard_queries.campaign_subjects(
        conn, ws_id, int(campaign_id), limit=_int_q(query, "limit", 10, lo=1, hi=100))


@_workspace_scoped
def handle_campaign_replies(conn, ws_id, match, query):
    # campaign_id is optional here: omitted = all campaigns for the range.
    campaign_id = _q(query, "campaign_id")
    return dashboard_queries.campaign_replies(
        conn, ws_id,
        campaign_id=int(campaign_id) if campaign_id else None,
        since=_q(query, "since"), until=_q(query, "until"),
        limit=_int_q(query, "limit", 200, lo=1, hi=500),
        sentiment=_q(query, "sentiment"), status_label=_q(query, "status_label"))


@_workspace_scoped
def handle_activity(conn, ws_id, match, query):
    return dashboard_queries.activity_feed(
        conn, ws_id,
        limit=_int_q(query, "limit", 50, lo=1, hi=200),
        before=_q(query, "before"),
        q=_q(query, "q"), event_type=_q(query, "event_type"),
        since=_q(query, "since"), until=_q(query, "until"))


@_workspace_scoped
def handle_activity_types(conn, ws_id, match, query):
    return dashboard_queries.activity_event_types(
        conn, ws_id, since=_q(query, "since"), until=_q(query, "until"))


@_workspace_scoped
def handle_lead_history(conn, ws_id, match, query):
    return dashboard_queries.lead_history(
        conn, int(match.group(1)), workspace_id=ws_id,
        limit=_int_q(query, "limit", 200, lo=1, hi=500))


def handle_event_body(match, query, body):
    conn = get_conn()
    try:
        return 200, dashboard_queries.event_body(conn, int(match.group(1)))
    finally:
        conn.close()


def handle_lead_emails(match, query, body):
    import lead_emails
    return 200, lead_emails.list_lead_emails(int(match.group(1)))


def handle_lead_phones(match, query, body):
    import phone_numbers
    return 200, phone_numbers.list_phones("lead", int(match.group(1)))


def handle_company_phones(match, query, body):
    import phone_numbers
    return 200, phone_numbers.list_phones("company", int(match.group(1)))


def handle_lead_phone_action(match, query, body):
    return 200, dashboard_actions.phone_action("lead", int(match.group(1)), body or {})


def handle_company_phone_action(match, query, body):
    return 200, dashboard_actions.phone_action("company", int(match.group(1)), body or {})


def handle_lead_custom_fields(match, query, body):
    conn = get_conn()
    try:
        return 200, dashboard_queries.lead_custom_fields(conn, int(match.group(1)))
    finally:
        conn.close()


def handle_lead_provider_runs(match, query, body):
    conn = get_conn()
    try:
        return 200, dashboard_queries.lead_provider_runs(conn, int(match.group(1)))
    finally:
        conn.close()


def handle_lead_identity(match, query, body):
    body = body or {}
    return 200, dashboard_actions.update_lead_identity(
        int(match.group(1)),
        name=body.get("name"), title=body.get("title"),
        linkedin=body.get("linkedin"))


def handle_leads_merge_preview(match, query, body):
    body = body or {}
    return 200, dashboard_actions.merge_leads_preview(
        int(body.get("keep_id") or 0),
        [int(i) for i in (body.get("merge_ids") or [])])


def handle_leads_merge(match, query, body):
    body = body or {}
    return 200, dashboard_actions.merge_leads_action(
        int(body.get("keep_id") or 0),
        [int(i) for i in (body.get("merge_ids") or [])])


def handle_sender_workspaces(match, query, body):
    import pipeline_sender_accounts as psa

    email = _q(query, "email")
    if not email:
        raise ValueError("email query parameter is required")
    return 200, psa.sender_account_workspaces(email)


def handle_sender_workspaces_set(match, query, body):
    import pipeline_sender_accounts as psa

    body = body or {}
    email = body.get("email")
    if not email:
        raise ValueError("email is required")
    # The full membership set, not a delta -- see set_sender_account_workspaces.
    return 200, psa.set_sender_account_workspaces(email, body.get("workspaces") or [])


def handle_domain_workspaces(match, query, body):
    import pipeline_sender_accounts as psa

    domain = _q(query, "domain")
    if not domain:
        raise ValueError("domain query parameter is required")
    return 200, psa.domain_workspace_summary(domain)


def handle_domain_workspaces_set(match, query, body):
    import pipeline_sender_accounts as psa

    body = body or {}
    domain = body.get("domain")
    if not domain:
        raise ValueError("domain is required")
    return 200, psa.set_domain_workspaces(domain, body.get("workspaces") or [])


def handle_lead_serper_candidates(match, query, body):
    import serper_review

    conn = get_conn()
    try:
        return 200, serper_review.lead_candidates(conn, int(match.group(1)))
    finally:
        conn.close()


@_workspace_scoped
def handle_serper_review(conn, ws_id, match, query):
    """The triage queue: leads with Serper candidates and no decision yet.

    Returns candidates in score order and nothing marked as chosen — the picker
    starts on "none of these" and the operator moves it. Ranking is not a
    recommendation.
    """
    import serper_review

    return serper_review.review_queue(
        conn, ws_id, field=_q(query, "field", "linkedin"),
        limit=_int_q(query, "limit", 25, lo=1, hi=100),
        offset=_int_q(query, "offset", 0, hi=1_000_000))


def handle_serper_apply(match, query, body):
    import serper_review

    body = body or {}
    decisions = body.get("decisions")
    if decisions is not None:
        # A triage session posts its whole batch at once; all-or-nothing, so a
        # partial failure can't leave the operator guessing which half landed.
        return 200, serper_review.apply_batch(
            decisions, dry_run=bool(body.get("dry_run")))
    return 200, serper_review.apply_decision(
        int(body.get("lead_id") or 0), str(body.get("field") or ""),
        value=body.get("value"), dismissed=bool(body.get("dismissed")),
        dry_run=bool(body.get("dry_run")))


def handle_record_type(match, query, body):
    body = body or {}
    return 200, dashboard_actions.set_record_type(
        int(match.group(1)), body.get("record_type") or "")


def handle_contact_order(match, query, body):
    body = body or {}
    return 200, dashboard_actions.set_contact_order(
        int(match.group(1)), body.get("contact_order"),
        workspace_slug=body.get("workspace"))


def handle_delete_leads(match, query, body):
    body = body or {}
    return 200, dashboard_actions.delete_leads(
        _parse_lead_ids(body.get("lead_ids")), confirm=bool(body.get("confirm")))


def handle_lead_custom_field_set(match, query, body):
    body = body or {}
    return 200, dashboard_actions.set_lead_custom_field(
        int(match.group(1)), body.get("scope") or "lead",
        body.get("field") or "", body.get("value") or "")


def handle_lead_email_action(match, query, body):
    body = body or {}
    return 200, dashboard_actions.lead_email_action(
        int(match.group(1)), body.get("op") or "", body.get("email") or "")


@_workspace_scoped
def handle_crm(conn, ws_id, match, query):
    return dashboard_queries.crm_overview(conn, ws_id)


def handle_outbox(match, query, body):
    conn = get_conn()
    try:
        return 200, dashboard_queries.sync_outbox(
            conn,
            workspace_slug=_q(query, "workspace"),
            limit=_int_q(query, "limit", 100, lo=1, hi=500),
            entity_type=_q(query, "entity_type"),
            op=_q(query, "op"))
    finally:
        conn.close()


def handle_outbox_detail(match, query, body):
    entity_type = _q(query, "entity_type")
    entity_id = _q(query, "entity_id")
    if not entity_type or entity_id is None:
        raise ValueError("entity_type and entity_id query parameters are required")
    conn = get_conn()
    try:
        return 200, dashboard_queries.outbox_item_detail(
            conn, entity_type, entity_id, op=_q(query, "op"))
    finally:
        conn.close()


def handle_crm_sync(match, query, body):
    body = body or {}
    lead_id = body.get("lead_id")
    status = dashboard_actions.sync_manager.start_crm_sync(
        body.get("workspace") or "",
        lead_id=int(lead_id) if lead_id else None,
        max_age=body.get("max_age"))
    if status is None:
        return 409, {"error": "a sync is already running"}
    return 202, status


def handle_sync_status(match, query, body):
    return 200, dashboard_actions.sync_manager.status()


def handle_sync_log(match, query, body):
    return 200, dashboard_actions.sync_manager.read_log(
        after=_int_q(query, "after", 0, hi=1_000_000_000))


def handle_sync_pull(match, query, body):
    status = dashboard_actions.sync_manager.start_pull()
    if status is None:
        return 409, {"error": "a sync is already running"}
    return 202, status


def handle_sync_push(match, query, body):
    status = dashboard_actions.sync_manager.start_push((body or {}).get("workspace"))
    if status is None:
        return 409, {"error": "a sync is already running"}
    return 202, status


def handle_change_stage(match, query, body):
    body = body or {}
    result = dashboard_actions.change_stage(
        int(match.group(1)), body.get("stage") or "",
        workspace_slug=body.get("workspace"),
        label=body.get("label"),
        sentiment=body.get("sentiment"))
    return 200, result


def handle_enrich(match, query, body):
    body = body or {}
    fields = {k: v for k, v in body.items() if k != "overwrite"}
    result = dashboard_actions.enrich(
        int(match.group(1)), fields, overwrite=bool(body.get("overwrite")))
    return 200, result


def handle_log_event(match, query, body):
    body = body or {}
    result = dashboard_actions.log_event(
        int(match.group(1)), body.get("event_type") or "",
        direction=body.get("direction") or "outbound",
        channel=body.get("channel") or "email",
        subject=body.get("subject"),
        body=body.get("body"),
        metadata=body.get("metadata"),
        workspace_slug=body.get("workspace"))
    return 200, result


ROUTES = [
    ("GET", re.compile(r"^/api/workspaces$"), handle_workspaces),
    ("GET", re.compile(r"^/api/summary$"), handle_summary),
    ("GET", re.compile(r"^/api/deliverability$"), handle_deliverability),
    ("GET", re.compile(r"^/api/pipeline$"), handle_pipeline),
    ("GET", re.compile(r"^/api/pipeline/leads$"), handle_pipeline_leads),
    ("GET", re.compile(r"^/api/attributes$"), handle_attributes),
    ("GET", re.compile(r"^/api/campaigns$"), handle_campaigns),
    ("GET", re.compile(r"^/api/campaigns/subjects$"), handle_campaign_subjects),
    ("GET", re.compile(r"^/api/campaigns/replies$"), handle_campaign_replies),
    ("GET", re.compile(r"^/api/campaigns/daily$"), handle_campaign_daily),
    ("GET", re.compile(r"^/api/campaigns/detail$"), handle_campaign_detail),
    ("GET", re.compile(r"^/api/campaigns/leads$"), handle_campaign_leads),
    ("GET", re.compile(r"^/api/contacts$"), handle_contacts),
    ("GET", re.compile(r"^/api/contacts/ids$"), handle_contacts_ids),
    ("GET", re.compile(r"^/api/contacts/stats$"), handle_contacts_stats),
    ("GET", re.compile(r"^/api/contacts/export/fields$"), handle_contacts_export_fields),
    ("POST", re.compile(r"^/api/contacts/export$"), handle_contacts_export),
    ("GET", re.compile(r"^/api/tags$"), handle_tags),
    ("GET", re.compile(r"^/api/linkedin/senders$"), handle_linkedin_senders),
    ("POST", re.compile(r"^/api/contacts/bulk$"), handle_contacts_bulk),
    ("GET", re.compile(r"^/api/suppression$"), handle_suppression_list),
    ("POST", re.compile(r"^/api/suppression/add$"), handle_suppression_add),
    ("POST", re.compile(r"^/api/suppression/remove$"), handle_suppression_remove),
    ("GET", re.compile(r"^/api/data-quality$"), handle_data_quality),
    ("GET", re.compile(r"^/api/serper/review$"), handle_serper_review),
    ("GET", re.compile(r"^/api/leads/(\d+)/serper-candidates$"), handle_lead_serper_candidates),
    ("GET", re.compile(r"^/api/senders/workspaces$"), handle_sender_workspaces),
    ("GET", re.compile(r"^/api/domains/workspaces$"), handle_domain_workspaces),
    ("GET", re.compile(r"^/api/enrich/targets$"), handle_enrich_targets),
    ("GET", re.compile(r"^/api/cleanup/preview$"), handle_cleanup_preview),
    ("GET", re.compile(r"^/api/cleanup/empty-leads/preview$"), handle_empty_leads_preview),
    ("GET", re.compile(r"^/api/companies$"), handle_companies),
    ("GET", re.compile(r"^/api/companies/stats$"), handle_companies_stats),
    ("GET", re.compile(r"^/api/companies/search$"), handle_company_search),
    ("GET", re.compile(r"^/api/companies/(\d+)$"), handle_company_detail),
    ("GET", re.compile(r"^/api/companies/(\d+)/contact-activity$"), handle_company_contact_activity),
    ("GET", re.compile(r"^/api/companies/(\d+)/domains$"), handle_company_domains),
    ("GET", re.compile(r"^/api/merge-candidates$"), handle_merge_candidates),
    ("GET", re.compile(r"^/api/domains/detail$"), handle_domain_detail),
    ("GET", re.compile(r"^/api/leads/(\d+)/history$"), handle_lead_history),
    ("GET", re.compile(r"^/api/leads/(\d+)/emails$"), handle_lead_emails),
    ("GET", re.compile(r"^/api/leads/(\d+)/phones$"), handle_lead_phones),
    ("GET", re.compile(r"^/api/companies/(\d+)/phones$"), handle_company_phones),
    ("GET", re.compile(r"^/api/leads/(\d+)/custom-fields$"), handle_lead_custom_fields),
    ("GET", re.compile(r"^/api/leads/(\d+)/provider-runs$"), handle_lead_provider_runs),
    ("GET", re.compile(r"^/api/events/(\d+)/body$"), handle_event_body),
    ("GET", re.compile(r"^/api/crm$"), handle_crm),
    ("GET", re.compile(r"^/api/outbox$"), handle_outbox),
    ("GET", re.compile(r"^/api/outbox/detail$"), handle_outbox_detail),
    ("POST", re.compile(r"^/api/crm/sync$"), handle_crm_sync),
    ("GET", re.compile(r"^/api/activity$"), handle_activity),
    ("GET", re.compile(r"^/api/activity/types$"), handle_activity_types),
    ("GET", re.compile(r"^/api/sync/status$"), handle_sync_status),
    ("GET", re.compile(r"^/api/sync/log$"), handle_sync_log),
    ("POST", re.compile(r"^/api/sync/pull$"), handle_sync_pull),
    ("POST", re.compile(r"^/api/sync/push$"), handle_sync_push),
    ("POST", re.compile(r"^/api/leads/(\d+)/stage$"), handle_change_stage),
    ("POST", re.compile(r"^/api/leads/(\d+)/enrich$"), handle_enrich),
    ("POST", re.compile(r"^/api/leads/(\d+)/identity$"), handle_lead_identity),
    ("POST", re.compile(r"^/api/serper/apply$"), handle_serper_apply),
    ("POST", re.compile(r"^/api/leads/merge/preview$"), handle_leads_merge_preview),
    ("POST", re.compile(r"^/api/leads/merge$"), handle_leads_merge),
    ("POST", re.compile(r"^/api/leads/delete$"), handle_delete_leads),
    ("POST", re.compile(r"^/api/leads/(\d+)/record-type$"), handle_record_type),
    ("POST", re.compile(r"^/api/leads/(\d+)/contact-order$"), handle_contact_order),
    ("POST", re.compile(r"^/api/senders/workspaces$"), handle_sender_workspaces_set),
    ("POST", re.compile(r"^/api/domains/workspaces$"), handle_domain_workspaces_set),
    ("POST", re.compile(r"^/api/leads/(\d+)/custom-fields$"), handle_lead_custom_field_set),
    ("POST", re.compile(r"^/api/leads/(\d+)/emails$"), handle_lead_email_action),
    ("POST", re.compile(r"^/api/leads/(\d+)/phones$"), handle_lead_phone_action),
    ("POST", re.compile(r"^/api/companies/(\d+)/phones$"), handle_company_phone_action),
    ("POST", re.compile(r"^/api/leads/(\d+)/events$"), handle_log_event),
    ("POST", re.compile(r"^/api/leads/(\d+)/link-company$"), handle_link_company),
    ("POST", re.compile(r"^/api/leads/bulk-link-company$"), handle_bulk_link_company),
    ("POST", re.compile(r"^/api/cleanup/run$"), handle_cleanup_run),
    ("POST", re.compile(r"^/api/cleanup/empty-leads/run$"), handle_empty_leads_run),
    ("POST", re.compile(r"^/api/enrich/email-finder$"), handle_email_finder),
    ("POST", re.compile(r"^/api/enrich/serper$"), handle_serper),
    ("POST", re.compile(r"^/api/companies/(\d+)/edit$"), handle_update_company),
    ("POST", re.compile(r"^/api/companies/(\d+)/domains$"), handle_set_company_domain),
    ("POST", re.compile(r"^/api/senders/edit$"), handle_edit_sender),
    ("POST", re.compile(r"^/api/domains/edit$"), handle_edit_domain),
    # Danger-zone deletes. Each files a relay tombstone via the BEFORE DELETE
    # outbox trigger, so they remove the row upstream too -- all require
    # confirm:true in the body.
    ("POST", re.compile(r"^/api/companies/(\d+)/delete$"), handle_delete_company),
    ("POST", re.compile(r"^/api/senders/delete$"), handle_delete_sender),
    ("POST", re.compile(r"^/api/domains/delete$"), handle_delete_domain),
    ("POST", re.compile(r"^/api/merge-candidates/bulk-resolve$"), handle_resolve_merges_bulk),
    ("POST", re.compile(r"^/api/merge-candidates/([\w-]+)/resolve$"), handle_resolve_merge),
]


def dispatch(method: str, path: str, query: dict, body) -> tuple[int, dict]:
    """Pure route dispatch, unit-testable without sockets."""
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        try:
            return handler(match, query, body)
        except IdentityConflict as exc:
            # 409, and the pair travels with it: "this LinkedIn URL belongs to
            # lead 19397" is only actionable if the surface can offer to merge
            # 19397 with the lead you were editing. Caught before ValueError,
            # which it subclasses. Queued here rather than at the raise site
            # because the raise unwinds the transaction that would carry it.
            dashboard_actions.queue_identity_conflict_merge(exc)
            return 409, {"error": str(exc), "conflict": exc.as_payload()}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except sqlite3.Error as exc:
            return 500, {"error": f"database error: {exc}"}
        except Exception as exc:  # noqa: BLE001 - see below
            # Anything else used to escape into the HTTP plumbing, which closes
            # the connection without a response -- the browser then reports
            # "Failed to fetch", which reads as a network problem and hides the
            # actual error. A one-word bad import cost a day that way. The
            # server is loopback-only and host-checked, so the message and
            # traceback go back to the operator rather than into a void.
            traceback.print_exc()
            return 500, {"error": f"{type(exc).__name__}: {exc}",
                         "traceback": traceback.format_exc()}
    return 404, {"error": f"no such endpoint: {method} {path}"}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "OMDashboard/1.0"
    extra_allowed_hosts: frozenset = frozenset()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in {"localhost", "127.0.0.1", "::1"} | self.extra_allowed_hosts

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_allowed():
            self._send_json(403, {"error": "host not allowed"})
            return
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            self._serve_html()
            return
        status, payload = dispatch("GET", url.path, parse_qs(url.query), None)
        self._send_json(status, payload)

    def do_POST(self):
        if not self._host_allowed():
            self._send_json(403, {"error": "host not allowed"})
            return
        if self.headers.get(CSRF_HEADER) != "1":
            self._send_json(403, {"error": f"missing {CSRF_HEADER} header"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        url = urlsplit(self.path)
        status, payload = dispatch("POST", url.path, parse_qs(url.query), body)
        self._send_json(status, payload)

    def _serve_html(self):
        try:
            data = _HTML_PATH.read_bytes()
        except OSError:
            self._send_json(500, {"error": "dashboard.html not found next to dashboard_server.py"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundDashboardHandler", (DashboardHandler,), {
        "extra_allowed_hosts": frozenset({host.lower()}),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def run(host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    if not database_has_schema():
        print(format_database_recovery_message())
        sys.exit(1)
    import pipeline as _pipeline

    _pipeline.migrate_db()
    _pipeline.sync_workspace_routing_mode_from_config()

    server = make_server(host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Outreach Magic dashboard: {url}  (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to a non-loopback address with no authentication.")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Outreach Magic local dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    run(host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
