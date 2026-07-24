#!/usr/bin/env python3
"""Tests for dashboard_queries: shapes, workspace isolation, thresholds."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import dashboard_queries as dq  # noqa: E402
import lead_actions  # noqa: E402
import pipeline as om  # noqa: E402
from bounces import record_bounce_event  # noqa: E402
from constants import PIPELINE_STAGES  # noqa: E402
from pipeline_sender_accounts import (  # noqa: E402
    ensure_sender_account,
    link_sender_account_to_workspace,
)
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

WS = {}


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.create_workspace("Team Beta", slug="beta")
    conn = om.get_conn()
    for slug in ("alpha", "beta"):
        WS[slug] = om.resolve_workspace_identity(conn, slug)["id"]
    conn.close()


def _conn():
    return om.get_conn()


def _add_lead(name, email, **fields):
    result = om.add_lead(name, company=fields.pop("company", "Acme Corp"),
                         email=email, **fields)
    return result["id"]


def _log(lead_id, event_type, workspace="alpha", **kw):
    return lead_actions.log_event_scoped(
        lead_id, event_type, workspace_slug=workspace, **kw)


def test_list_workspaces():
    conn = _conn()
    try:
        out = dq.list_workspaces(conn)
    finally:
        conn.close()
    assert out["routing_mode"] == "multi"
    slugs = [w["slug"] for w in out["workspaces"]]
    assert "alpha" in slugs and "beta" in slugs


def test_summary_counts_and_isolation():
    a1 = _add_lead("Ann A", "ann@a-example.com")
    a2 = _add_lead("Bob B", "bob@b-example.com")
    b1 = _add_lead("Cat C", "cat@c-example.com")
    _log(a1, "email_sent")
    _log(a2, "email_sent")
    _log(a1, "email_reply", direction="inbound")
    _log(a2, "email_bounce")
    _log(b1, "email_sent", workspace="beta")

    conn = _conn()
    try:
        alpha = dq.summary(conn, WS["alpha"])
        beta = dq.summary(conn, WS["beta"])
    finally:
        conn.close()
    assert alpha["sent"] == 2
    assert alpha["replied"] == 1
    assert alpha["bounced"] == 1
    assert alpha["latest_event_at"]
    # status set at workspace_leads creation (first event was email_sent)
    assert alpha["stages"]["contacted"] == 2
    assert beta["sent"] == 1
    assert beta["replied"] == 0


def test_bounce_series_daily_rate():
    lead = _add_lead("Dan D", "dan@d-example.com")
    _log(lead, "email_sent")
    _log(lead, "email_sent")
    _log(lead, "email_bounce")
    conn = _conn()
    try:
        out = dq.bounce_series(conn, WS["alpha"])
    finally:
        conn.close()
    assert len(out["series"]) == 1
    day = out["series"][0]
    assert day["sent"] == 2
    assert day["bounced"] == 1
    assert day["bounce_rate"] == 0.5


def test_mailbox_health_flags_and_isolation():
    lead = _add_lead("Eve E", "eve@e-example.com")
    conn = _conn()
    try:
        sa_alpha = ensure_sender_account(conn, "ops@send-example.com")
        link_sender_account_to_workspace(conn, WS["alpha"], sa_alpha)
        sa_beta = ensure_sender_account(conn, "other@beta-example.com")
        link_sender_account_to_workspace(conn, WS["beta"], sa_beta)
        record_bounce_event(
            conn, lead_id=lead, event_id=None, platform="smartlead",
            sender_email="ops@send-example.com", lead_email="eve@e-example.com",
            payload={"bounce_type": "hard", "bounce_message": "550 blocked"},
            workspace_id=WS["alpha"])
        conn.commit()
        out = dq.mailbox_health(conn, WS["alpha"])
    finally:
        conn.close()
    emails = [m["email"] for m in out["mailboxes"]]
    assert emails == ["ops@send-example.com"]
    mb = out["mailboxes"][0]
    assert mb["bounces_last_7d"] == 1
    assert mb["trending_down"] is True
    assert out["flagged"] == ["ops@send-example.com"]


def test_domain_health_flags_listed():
    listed_report = json.dumps({
        "checked_at": "2026-07-13T12:00:00+00:00", "all_clean": False,
        "summary": {"clean": 2, "listed": 1, "errors": 0},
        "results": [
            {"name": "Spamhaus_DBL", "status": "clean"},
            {"name": "SURBL", "status": "listed", "addresses": ["127.0.0.64"]},
        ],
    })
    clean_report = json.dumps({
        "checked_at": "2026-07-13T12:00:00+00:00", "all_clean": True,
        "results": [{"name": "Spamhaus_DBL", "status": "clean"}],
    })
    conn = _conn()
    try:
        for email, domain, report in (
            ("ops@send-example.com", "send-example.com", listed_report),
            ("ok@fine-example.com", "fine-example.com", clean_report),
        ):
            sa = ensure_sender_account(conn, email)
            link_sender_account_to_workspace(conn, WS["alpha"], sa)
            conn.execute(
                "INSERT INTO sender_domains (domain, dnsbl_status) VALUES (?, ?)",
                (domain, report))
        conn.commit()
        out = dq.domain_health(conn, WS["alpha"])
        beta = dq.domain_health(conn, WS["beta"])
    finally:
        conn.close()
    by_domain = {d["domain"]: d for d in out["domains"]}
    assert by_domain["send-example.com"]["listed"] is True
    assert by_domain["send-example.com"]["dnsbl_summary"] == "listed: SURBL"
    assert by_domain["fine-example.com"]["listed"] is False
    assert by_domain["fine-example.com"]["dnsbl_summary"] == "clean"
    assert out["flagged"] == ["send-example.com"]
    assert beta["domains"] == []


def test_pipeline_counts_ordered_with_zeroes():
    lead = _add_lead("Fay F", "fay@f-example.com")
    _log(lead, "email_sent")
    conn = _conn()
    try:
        out = dq.pipeline_counts(conn, WS["alpha"])
    finally:
        conn.close()
    stage_names = [s["stage"] for s in out["stages"][:len(PIPELINE_STAGES)]]
    assert stage_names == PIPELINE_STAGES
    counts = {s["stage"]: s["count"] for s in out["stages"]}
    assert counts["contacted"] == 1
    assert counts["won"] == 0


def test_pipeline_leads_drilldown():
    lead = _add_lead("Gus G", "gus@g-example.com", title="CTO")
    _log(lead, "email_sent")
    conn = _conn()
    try:
        out = dq.pipeline_leads(conn, WS["alpha"], "contacted")
        empty = dq.pipeline_leads(conn, WS["alpha"], "won")
    finally:
        conn.close()
    assert out["total"] == 1
    assert out["leads"][0]["name"] == "Gus G"
    assert out["leads"][0]["title"] == "CTO"
    assert empty["total"] == 0 and empty["leads"] == []


def test_attribute_performance_rates_and_threshold():
    l1 = _add_lead("Hal H", "hal@h-example.com", industry="SaaS")
    l2 = _add_lead("Ivy I", "ivy@i-example.com", industry="SaaS")
    l3 = _add_lead("Joy J", "joy@j-example.com", industry="Retail")
    for lid in (l1, l2, l3):
        _log(lid, "email_sent")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_leads SET email_sent_count = 1 WHERE workspace_id = ?",
            (WS["alpha"],))
        conn.execute(
            "UPDATE workspace_leads SET total_replies_count = 1,"
            " current_status_sentiment = 'positive'"
            " WHERE workspace_id = ? AND lead_id = ?",
            (WS["alpha"], l1))
        conn.commit()
        out = dq.attribute_performance(conn, WS["alpha"], field="industry", min_sample=1)
        thresholded = dq.attribute_performance(conn, WS["alpha"], field="industry", min_sample=2)
    finally:
        conn.close()
    by_value = {r["value"]: r for r in out["rows"]}
    assert by_value["SaaS"]["contacted"] == 2
    assert by_value["SaaS"]["replied"] == 1
    assert by_value["SaaS"]["reply_rate"] == 0.5
    assert by_value["SaaS"]["positive"] == 1
    assert by_value["Retail"]["replied"] == 0
    assert [r["value"] for r in thresholded["rows"]] == ["SaaS"]


def test_attribute_performance_rejects_unknown_field():
    conn = _conn()
    try:
        with pytest.raises(ValueError):
            dq.attribute_performance(conn, WS["alpha"], field="notes; DROP TABLE leads")
    finally:
        conn.close()


def test_campaign_audit_and_subjects():
    l1 = _add_lead("Kim K", "kim@k-example.com")
    l2 = _add_lead("Lee L", "lee@l-example.com")
    _log(l1, "email_sent", subject="Quick question",
         metadata={"campaign": "alpha | outbound q3"})
    _log(l2, "email_sent", subject="Quick question",
         metadata={"campaign": "alpha | outbound q3"})
    _log(l1, "email_reply", direction="inbound", subject="Re: Quick question",
         metadata={"campaign": "alpha | outbound q3"})
    conn = _conn()
    try:
        lead_actions.change_stage_scoped(
            l1, "interested", workspace_slug="alpha", sentiment="positive")
        out = dq.campaign_audit(conn, WS["alpha"])
        beta = dq.campaign_audit(conn, WS["beta"])
    finally:
        conn.close()
    assert len(out["campaigns"]) == 1
    c = out["campaigns"][0]
    assert c["name"] == "alpha | outbound q3"
    assert c["sent"] == 2
    assert c["replies"] == 1
    assert c["reply_rate"] == 0.5
    assert c["positive"] == 1
    assert c["bounces"] == 0
    assert beta["campaigns"] == []

    conn = _conn()
    try:
        subjects = dq.campaign_subjects(conn, WS["alpha"], c["id"])
    finally:
        conn.close()
    assert len(subjects["subjects"]) == 1
    subj = subjects["subjects"][0]
    assert subj["subject"] == "Quick question"  # "Re:" merged in
    assert subj["sends"] == 2
    assert subj["replies"] == 1


def test_activity_feed_pagination_and_isolation():
    l1 = _add_lead("Mia M", "mia@m-example.com")
    b1 = _add_lead("Ned N", "ned@n-example.com")
    _log(l1, "email_sent", subject="First")
    _log(l1, "email_reply", direction="inbound", subject="Re: First")
    _log(b1, "email_sent", workspace="beta")
    conn = _conn()
    try:
        page1 = dq.activity_feed(conn, WS["alpha"], limit=1)
        assert len(page1["events"]) == 1
        assert page1["next_before"]
        page2 = dq.activity_feed(conn, WS["alpha"], limit=1, before=page1["next_before"])
        full = dq.activity_feed(conn, WS["alpha"], limit=50)
    finally:
        conn.close()
    assert len(page2["events"]) == 1
    assert page1["events"][0] != page2["events"][0]
    assert len(full["events"]) == 2
    assert all(ev["lead_name"] == "Mia M" for ev in full["events"])
    assert full["next_before"] is None


def test_activity_search_by_text_type_and_range():
    alice = _add_lead("Alice Activity", "alice@acme-act.com", company="Acme Act")
    bob = _add_lead("Bob Activity", "bob@globex-act.com", company="Globex Act")
    _log(alice, "email_sent", subject="Hi Alice")
    _log(alice, "email_reply", direction="inbound", subject="Re: Hi Alice")
    _log(bob, "email_sent", subject="Hi Bob")
    conn = _conn()
    try:
        by_name = dq.activity_feed(conn, WS["alpha"], q="alice")
        by_domain = dq.activity_feed(conn, WS["alpha"], q="globex-act.com")
        by_company = dq.activity_feed(conn, WS["alpha"], q="Acme Act")
        replies = dq.activity_feed(conn, WS["alpha"], event_type="email_reply")
        types = dq.activity_event_types(conn, WS["alpha"])
    finally:
        conn.close()
    assert {e["lead_name"] for e in by_name["events"]} == {"Alice Activity"}
    assert len(by_name["events"]) == 2  # both of Alice's events
    assert {e["lead_name"] for e in by_domain["events"]} == {"Bob Activity"}
    assert {e["lead_name"] for e in by_company["events"]} == {"Alice Activity"}
    assert all(e["event_type"] == "email_reply" for e in replies["events"])
    assert len(replies["events"]) == 1
    type_map = {t["event_type"]: t["n"] for t in types["event_types"]}
    assert type_map["email_sent"] == 2 and type_map["email_reply"] == 1


def test_activity_search_range_excludes_old():
    lead = _add_lead("Ranged Act", "ranged@r-act.com")
    _log(lead, "email_sent")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_lead_events SET event_at = '2000-01-01T00:00:00Z'"
            " WHERE lead_id = ?", (lead,))
        conn.commit()
        in_window = dq.activity_feed(conn, WS["alpha"], since="7d")
        all_time = dq.activity_feed(conn, WS["alpha"])
        typed_window = dq.activity_event_types(conn, WS["alpha"], since="7d")
    finally:
        conn.close()
    assert all_time["events"] and in_window["events"] == []
    assert typed_window["event_types"] == []


def test_attribute_normalization_merges_case_variants():
    l1 = _add_lead("Nia N", "nia@n2-example.com", title="Career Services Coordinator")
    l2 = _add_lead("Oli O", "oli@o2-example.com", title="career services coordinator")
    for lid in (l1, l2):
        _log(lid, "email_sent")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_leads SET email_sent_count = 1 WHERE workspace_id = ?",
            (WS["alpha"],))
        conn.commit()
        out = dq.attribute_performance(conn, WS["alpha"], field="title", min_sample=1)
    finally:
        conn.close()
    assert len(out["rows"]) == 1
    assert out["rows"][0]["contacted"] == 2
    assert out["rows"][0]["value"].lower() == "career services coordinator"


def test_attribute_campaign_filter():
    l1 = _add_lead("Pam P", "pam@p2-example.com", industry="SaaS")
    l2 = _add_lead("Quinn Q", "quinn@q2-example.com", industry="SaaS")
    _log(l1, "email_sent", metadata={"campaign": "alpha | one"})
    _log(l2, "email_sent", metadata={"campaign": "alpha | two"})
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_leads SET email_sent_count = 1 WHERE workspace_id = ?",
            (WS["alpha"],))
        conn.commit()
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'alpha | one'").fetchone()["id"]
        out = dq.attribute_performance(
            conn, WS["alpha"], field="industry", min_sample=1, campaign_id=cid)
    finally:
        conn.close()
    assert out["rows"][0]["contacted"] == 1  # only the lead in campaign "one"


def test_campaign_daily_matrix():
    l1 = _add_lead("Raj R", "raj@r2-example.com")
    _log(l1, "email_sent", metadata={"campaign": "alpha | daily"})
    _log(l1, "email_reply", direction="inbound", metadata={"campaign": "alpha | daily"})
    _log(l1, "email_bounce", metadata={"campaign": "alpha | daily"})
    _log(l1, "linkedin_message", channel="linkedin")
    lead_actions.change_stage_scoped(
        l1, "interested", workspace_slug="alpha", sentiment="positive")
    conn = _conn()
    try:
        all_out = dq.campaign_daily(conn, WS["alpha"])
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'alpha | daily'").fetchone()["id"]
        scoped = dq.campaign_daily(conn, WS["alpha"], campaign_id=cid)
    finally:
        conn.close()
    assert len(all_out["days"]) == 1
    day = all_out["days"][0]
    assert day["email_sent"] == 1
    assert day["email_received"] == 1
    assert day["dm_sent"] == 1
    assert day["bounces"] == 1
    assert day["interested"] == 1
    assert all_out["totals"]["email_sent"] == 1
    # campaign-scoped excludes the DM (no campaign) but keeps the campaign events
    assert scoped["days"][0]["email_sent"] == 1
    assert scoped["days"][0]["dm_sent"] == 0


def test_campaign_detail():
    l1 = _add_lead("Sam S", "sam@s2-example.com")
    _log(l1, "email_sent", metadata={"campaign": "alpha | detail", "sender": "x"})
    conn = _conn()
    try:
        conn.execute(
            "UPDATE events SET sender = 'ops@send-example.com' WHERE lead_id = ?", (l1,))
        conn.commit()
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'alpha | detail'").fetchone()["id"]
        out = dq.campaign_detail(conn, WS["alpha"], cid)
    finally:
        conn.close()
    assert out["campaign"]["name"] == "alpha | detail"
    assert out["senders"][0]["sender"] == "ops@send-example.com"
    assert out["activity"]["leads_touched"] == 1
    assert any(s["stage"] == "contacted" and s["count"] == 1 for s in out["lead_statuses"])
    with pytest.raises(ValueError):
        conn2 = _conn()
        try:
            dq.campaign_detail(conn2, WS["alpha"], 999999)
        finally:
            conn2.close()


def test_lead_history_and_event_body():
    l1 = _add_lead("Tia T", "tia@t2-example.com")
    _log(l1, "email_sent", subject="Hello there",
         metadata={"body": "Hi Tia,\n\nFull message body here with lots of text."})
    b1 = _add_lead("Uma U", "uma@u2-example.com")
    _log(b1, "email_sent", workspace="beta")
    conn = _conn()
    try:
        out = dq.lead_history(conn, l1, workspace_id=WS["alpha"])
    finally:
        conn.close()
    assert out["lead"]["name"] == "Tia T"
    assert len(out["events"]) == 1
    ev = out["events"][0]
    assert ev["subject"] == "Hello there"
    assert ev["has_body"] == 1

    conn = _conn()
    try:
        body = dq.event_body(conn, ev["id"])
        with pytest.raises(ValueError):
            dq.event_body(conn, 999999)
    finally:
        conn.close()
    assert body["body"].startswith("Hi Tia,")
    assert body["body_is_full"] is True
    assert "body" not in body["metadata"]


def test_domain_health_includes_unregistered_domains():
    conn = _conn()
    try:
        sa = ensure_sender_account(conn, "ops@unregistered-example.com")
        link_sender_account_to_workspace(conn, WS["alpha"], sa)
        conn.commit()
        out = dq.domain_health(conn, WS["alpha"])
    finally:
        conn.close()
    d = {x["domain"]: x for x in out["domains"]}["unregistered-example.com"]
    assert d["registered"] is False
    assert d["dnsbl_summary"] == "not monitored"
    assert d["listed"] is False
    assert d["mailboxes"] == 1


def test_domain_detail():
    conn = _conn()
    try:
        sa = ensure_sender_account(conn, "ops@detail-example.com")
        link_sender_account_to_workspace(conn, WS["alpha"], sa)
        conn.execute(
            "INSERT INTO sender_domains (domain, reseller, domain_cost, currency, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            ("detail-example.com", "resellerco", 12.0, "USD", "batch 3"))
        conn.commit()
        out = dq.domain_detail(conn, WS["alpha"], "detail-example.com")
    finally:
        conn.close()
    assert out["registration"]["reseller"] == "resellerco"
    assert out["registration"]["domain_cost"] == 12.0
    assert out["registration"]["notes"] == "batch 3"
    assert out["mailboxes"][0]["email"] == "ops@detail-example.com"


def test_crm_overview_unconfigured():
    l1 = _add_lead("Vic V", "vic@v2-example.com")
    lead_actions.change_stage_scoped(
        l1, "interested", workspace_slug="alpha", sentiment="positive")
    conn = _conn()
    try:
        out = dq.crm_overview(conn, WS["alpha"])
    finally:
        conn.close()
    assert out["configured"] is False
    assert out["counts"]["synced"] == 0
    assert out["counts"]["pending"] >= 1
    assert any(p["lead_id"] == l1 for p in out["pending"])


def test_sync_outbox_audit():
    l1 = _add_lead("Wes W", "wes@w2-example.com")
    _log(l1, "email_sent")
    conn = _conn()
    try:
        out = dq.sync_outbox(conn)
        scoped = dq.sync_outbox(conn, workspace_slug="alpha")
    finally:
        conn.close()
    assert out["total"] > 0
    assert any(g["entity_type"] == "lead_core" for g in out["groups"])
    assert len(out["rows"]) <= 100
    assert scoped["total"] <= out["total"]
    assert out["matched"] == out["total"] and out["showing"] == len(out["rows"])
    # upserts are what the push drains; a fresh add_lead queues upserts, no deletes
    assert out["pushable_total"] > 0
    assert out["delete_total"] == 0
    assert out["total"] == out["pushable_total"] + out["delete_total"]


def test_sync_outbox_entity_filter_and_truncation():
    l1 = _add_lead("Filt Er", "filt@f9-example.com")
    _log(l1, "email_sent")  # queues lead_core + lead_workspace outbox rows
    conn = _conn()
    try:
        all_rows = dq.sync_outbox(conn, limit=2)
        core = dq.sync_outbox(conn, entity_type="lead_core")
        workspace = dq.sync_outbox(conn, entity_type="lead_workspace")
    finally:
        conn.close()
    # limit truncates the row list but matched still reports the full count
    assert all_rows["showing"] <= 2
    assert all_rows["matched"] == all_rows["total"]
    # entity_type filter narrows rows + matched, but groups stay the full overview
    assert core["entity_type"] == "lead_core"
    assert all(r["entity_type"] == "lead_core" for r in core["rows"])
    assert core["matched"] < core["total"]  # excludes lead_workspace rows
    assert any(g["entity_type"] == "lead_workspace" for g in core["groups"])
    assert all(r["entity_type"] == "lead_workspace" for r in workspace["rows"])


def test_outbox_item_detail_lead_core():
    lead = _add_lead("Outbox Person", "op@ob-example.com", company="OB Co")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT entity_type, entity_id, op FROM outbox"
            " WHERE entity_type = 'lead_core' AND entity_id = ? LIMIT 1",
            (str(lead),)).fetchone()
        assert row is not None, "add_lead should have queued a lead_core outbox row"
        out = dq.outbox_item_detail(conn, "lead_core", str(lead))
    finally:
        conn.close()
    assert out["entity_type"] == "lead_core"
    assert out["record_exists"] is True
    assert out["record"]["name"] == "Outbox Person"
    assert out["outbox"] and out["outbox"][0]["op"] in ("upsert", "delete")
    assert isinstance(out["payload"], dict)
    assert out["payload_error"] is None


def test_outbox_item_detail_missing_raises():
    conn = _conn()
    try:
        with pytest.raises(ValueError):
            dq.outbox_item_detail(conn, "lead_core", "999999")
    finally:
        conn.close()


def test_range_clause_until():
    l1 = _add_lead("Xan X", "xan@x2-example.com")
    _log(l1, "email_sent")
    conn = _conn()
    try:
        included = dq.summary(conn, WS["alpha"], since="7d", until="2099-01-01")
        excluded = dq.summary(conn, WS["alpha"], since=None, until="2000-01-01")
    finally:
        conn.close()
    assert included["sent"] == 1
    assert excluded["sent"] == 0


def test_pipeline_counts_active_in_range():
    """With a range, pipeline counts only leads active in that window."""
    recent = _add_lead("Recent R", "recent@r3-example.com")
    _log(recent, "email_sent")  # event dated ~now
    old = _add_lead("Old O", "old@o3-example.com")
    _log(old, "email_sent")
    conn = _conn()
    try:
        # Backdate the old lead's workspace event outside a tight window.
        conn.execute(
            "UPDATE workspace_lead_events SET event_at = '2000-01-01T00:00:00Z'"
            " WHERE lead_id = ?", (old,))
        conn.commit()
        no_range = dq.pipeline_counts(conn, WS["alpha"])
        ranged = dq.pipeline_counts(conn, WS["alpha"], since="7d")
    finally:
        conn.close()
    assert no_range["range_active"] is False
    counts_all = {s["stage"]: s["count"] for s in no_range["stages"]}
    assert counts_all["contacted"] == 2  # both leads, all-time
    assert ranged["range_active"] is True
    counts_ranged = {s["stage"]: s["count"] for s in ranged["stages"]}
    assert counts_ranged["contacted"] == 1  # only the recent lead is active in 7d


def test_pipeline_leads_active_in_range():
    recent = _add_lead("Rex R", "rex@r4-example.com")
    _log(recent, "email_sent")
    old = _add_lead("Otto O", "otto@o4-example.com")
    _log(old, "email_sent")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_lead_events SET event_at = '2000-01-01T00:00:00Z'"
            " WHERE lead_id = ?", (old,))
        conn.commit()
        ranged = dq.pipeline_leads(conn, WS["alpha"], "contacted", since="7d")
        allrows = dq.pipeline_leads(conn, WS["alpha"], "contacted")
    finally:
        conn.close()
    assert allrows["total"] == 2
    assert ranged["total"] == 1
    assert ranged["leads"][0]["name"] == "Rex R"


def test_campaign_detail_senders_respect_range():
    l1 = _add_lead("Cyd C", "cyd@c4-example.com")
    _log(l1, "email_sent", metadata={"campaign": "alpha | ranged"})
    conn = _conn()
    try:
        conn.execute("UPDATE events SET sender = 'ops@send-example.com'")
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'alpha | ranged'").fetchone()["id"]
        conn.execute(
            "UPDATE workspace_lead_events SET event_at = '2000-01-01T00:00:00Z'"
            " WHERE lead_id = ?", (l1,))
        conn.commit()
        in_window = dq.campaign_detail(conn, WS["alpha"], cid, since="7d")
        all_time = dq.campaign_detail(conn, WS["alpha"], cid)
    finally:
        conn.close()
    # The one event is backdated to 2000, so a 7d window sees no senders/activity.
    assert all_time["senders"] and all_time["activity"]["events"] == 1
    assert in_window["senders"] == []
    assert (in_window["activity"]["events"] or 0) == 0


def test_search_leads_by_attributes():
    l1 = _add_lead("Alice Anderson", "alice@acme-example.com", title="VP Sales",
                   company="Acme Corp")
    l2 = _add_lead("Bob Brown", "bob@globex-example.com", company="Globex")
    _log(l1, "email_sent")
    _log(l2, "email_sent")
    conn = _conn()
    try:
        by_name = dq.search_leads(conn, WS["alpha"], q="alice")
        by_domain = dq.search_leads(conn, WS["alpha"], q="globex-example.com")
        by_company = dq.search_leads(conn, WS["alpha"], q="Acme")
        all_leads = dq.search_leads(conn, WS["alpha"])
    finally:
        conn.close()
    assert by_name["total"] == 1 and by_name["leads"][0]["name"] == "Alice Anderson"
    assert by_domain["total"] == 1 and by_domain["leads"][0]["name"] == "Bob Brown"
    assert by_company["total"] == 1
    assert all_leads["total"] == 2


def test_search_leads_missing_filters_and_pagination():
    l1 = _add_lead("Has Email", "has@e5-example.com", company="Comp A")
    l2 = _add_lead("Unknown", "", company=None)  # no email, unknown name, no company
    _log(l1, "email_sent")
    _log(l2, "email_sent")
    conn = _conn()
    try:
        no_email = dq.search_leads(conn, WS["alpha"], missing="email")
        no_company = dq.search_leads(conn, WS["alpha"], missing="company")
        unknown = dq.search_leads(conn, WS["alpha"], missing="name")
        page = dq.search_leads(conn, WS["alpha"], limit=1, offset=0)
    finally:
        conn.close()
    assert {l["name"] for l in no_email["leads"]} == {"Unknown"}
    assert any(l["name"] == "Unknown" for l in no_company["leads"])
    assert unknown["total"] == 1
    assert page["total"] == 2 and len(page["leads"]) == 1


def test_campaign_leads_scoped():
    l1 = _add_lead("In Camp", "inc@c6-example.com")
    l2 = _add_lead("Not In Camp", "noc@c6-example.com")
    _log(l1, "email_sent", metadata={"campaign": "alpha | members"})
    _log(l2, "email_sent")
    conn = _conn()
    try:
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'alpha | members'").fetchone()["id"]
        out = dq.campaign_leads(conn, WS["alpha"], cid)
    finally:
        conn.close()
    assert out["total"] == 1
    assert out["leads"][0]["name"] == "In Camp"


def test_search_and_detail_companies():
    l1 = _add_lead("Cara C", "cara@northwind-example.com", company="Northwind Traders",
                   industry="Logistics")
    _log(l1, "email_sent")
    conn = _conn()
    try:
        found = dq.search_companies(conn, WS["alpha"], q="northwind")
        assert found["companies"], "expected a company match"
        cid = found["companies"][0]["id"]
        # add a second branch domain
        conn.execute(
            "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role)"
            " VALUES ('default', ?, 'domain', 'eu.northwind-example.com', 'email')", (cid,))
        conn.commit()
        detail = dq.company_detail(conn, WS["alpha"], cid)
    finally:
        conn.close()
    assert detail["company"]["name"] == "Northwind Traders"
    assert any(d["value"] == "eu.northwind-example.com" for d in detail["domains"])
    assert detail["lead_count"] == 1
    assert detail["leads"][0]["name"] == "Cara C"


def test_company_autocomplete():
    _add_lead("Dee D", "dee@acmelabs-example.com", company="Acme Labs")
    conn = _conn()
    try:
        out = dq.company_search_for_link(conn, "acme")
    finally:
        conn.close()
    assert any(c["name"] == "Acme Labs" for c in out["companies"])


def test_data_quality_buckets():
    good = _add_lead("Full Person", "full@dq-example.com", company="DQ Corp", title="CEO")
    no_email = _add_lead("No Email", "", company="DQ Corp", title="Rep")
    unknown = _add_lead("Unknown", "", company=None)
    for lid in (good, no_email, unknown):
        _log(lid, "email_sent")
    conn = _conn()
    try:
        # add_lead auto-links a company when text is given; simulate the real
        # import state where company text is present but company_id is NULL.
        conn.execute("UPDATE leads SET company_id = NULL")
        conn.commit()
        out = dq.data_quality(conn, WS["alpha"])
    finally:
        conn.close()
    counts = {b["key"]: b["count"] for b in out["buckets"]}
    assert out["total"] == 3
    assert counts["missing_email"] == 2  # no_email + unknown
    assert counts["unknown_name"] == 1
    assert counts["missing_title"] == 1  # unknown only (good+no_email have titles)
    # all three lack a linked company_id (never linked); linkable = has text, no id
    assert counts["missing_company"] == 3
    assert counts["linkable"] == 2  # good + no_email have company text; unknown has none
    assert "junk_deletable" in out


def test_data_quality_respects_range():
    recent = _add_lead("Recent DQ", "", company="R Co")
    _log(recent, "email_sent")
    old = _add_lead("Old DQ", "", company="O Co")
    _log(old, "email_sent")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE workspace_lead_events SET event_at = '2000-01-01T00:00:00Z'"
            " WHERE lead_id = ?", (old,))
        conn.commit()
        ranged = dq.data_quality(conn, WS["alpha"], since="7d")
        allrows = dq.data_quality(conn, WS["alpha"])
    finally:
        conn.close()
    assert allrows["range_active"] is False and allrows["total"] == 2
    assert ranged["range_active"] is True and ranged["total"] == 1


def test_search_leads_missing_linkable():
    linkable = _add_lead("Has Text", "ht@lk-example.com", company="Linkme LLC")
    already = _add_lead("Linked Up", "lu@lk-example.com", company="Linkme LLC")
    no_text = _add_lead("No Company", "nc@lk-example.com", company=None)
    for lid in (linkable, already, no_text):
        _log(lid, "email_sent")
    conn = _conn()
    try:
        # "Has Text" and "No Company" unlinked; "Linked Up" keeps its company_id.
        conn.execute("UPDATE leads SET company_id = NULL WHERE id IN (?, ?)",
                     (linkable, no_text))
        conn.commit()
        out = dq.search_leads(conn, WS["alpha"], missing="linkable")
    finally:
        conn.close()
    names = {l["name"] for l in out["leads"]}
    assert "Has Text" in names
    assert "Linked Up" not in names  # already linked
    assert "No Company" not in names  # no text to link from


def test_enrichment_targets_domain_resolution():
    lead = _add_lead("Target Person", "", company="Northwind")
    _log(lead, "email_sent")
    conn = _conn()
    try:
        cid = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (lead,)).fetchone()["company_id"]
        conn.execute(
            "INSERT INTO company_identities (org_id, company_id, identity_type,"
            " identity_value_normalized, role, is_verified)"
            " VALUES ('default', ?, 'domain', 'northwind.com', 'primary', 1)", (cid,))
        conn.execute(
            "INSERT INTO company_identities (org_id, company_id, identity_type,"
            " identity_value_normalized, role, is_verified)"
            " VALUES ('default', ?, 'domain', 'eu.northwind.com', 'email', 0)", (cid,))
        conn.commit()
        out = dq.enrichment_targets(conn, WS["alpha"], [lead])
    finally:
        conn.close()
    t = out["targets"][0]
    assert t["lead_id"] == lead
    assert t["chosen_domain"] == "northwind.com"  # primary+verified ranked first
    assert t["multi_domain"] is True
    assert t["no_domain"] is False
    assert t["has_email"] is False


def test_enrichment_targets_no_domain():
    lead = _add_lead("Domainless", "", company="Vague Co")
    _log(lead, "email_sent")
    conn = _conn()
    try:
        out = dq.enrichment_targets(conn, WS["alpha"], [lead])
    finally:
        conn.close()
    t = out["targets"][0]
    assert t["no_domain"] is True and t["chosen_domain"] is None
