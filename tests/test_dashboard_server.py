#!/usr/bin/env python3
"""HTTP-level tests for dashboard_server: routing, hardening, end-to-end writes."""

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import dashboard_actions  # noqa: E402
import dashboard_server  # noqa: E402
import pipeline as om  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

CSRF = {dashboard_server.CSRF_HEADER: "1"}


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    dashboard_actions.sync_manager = dashboard_actions.SyncManager()


@pytest.fixture()
def base_url():
    server = dashboard_server.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _request(base, path, method="GET", body=None, headers=None, host=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, {"raw": payload.decode(errors="replace")}


def _add_lead():
    return om.add_lead("Pat Tester", company="Acme Corp", email="pat@acme-example.com")["id"]


def test_workspaces_endpoint(base_url):
    status, payload = _request(base_url, "/api/workspaces")
    assert status == 200
    assert payload["routing_mode"] == "multi"
    assert any(w["slug"] == "alpha" for w in payload["workspaces"])


def test_summary_requires_and_validates_workspace(base_url):
    status, payload = _request(base_url, "/api/summary?workspace=alpha")
    assert status == 200
    assert "stages" in payload and "sent" in payload

    status, payload = _request(base_url, "/api/summary")
    assert status == 400
    assert "workspace" in payload["error"]

    status, payload = _request(base_url, "/api/summary?workspace=ghost")
    assert status == 400
    assert "not found" in payload["error"]


def test_read_endpoints_return_json(base_url):
    for path in (
        "/api/deliverability?workspace=alpha",
        "/api/pipeline?workspace=alpha",
        "/api/pipeline/leads?workspace=alpha&status=contacted",
        "/api/attributes?workspace=alpha&min_sample=1",
        "/api/campaigns?workspace=alpha",
        "/api/activity?workspace=alpha",
        "/api/sync/status",
    ):
        status, payload = _request(base_url, path)
        assert status == 200, (path, payload)
        assert isinstance(payload, dict)


def test_unknown_route_404(base_url):
    status, payload = _request(base_url, "/api/nope")
    assert status == 404


def test_serves_html_page(base_url):
    req = urllib.request.Request(base_url + "/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        assert b"<title>" in resp.read()


def test_post_requires_csrf_header(base_url):
    lead_id = _add_lead()
    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/stage", method="POST",
        body={"stage": "contacted", "workspace": "alpha"})
    assert status == 403
    assert dashboard_server.CSRF_HEADER in payload["error"]


def test_host_header_guard(base_url):
    status, payload = _request(base_url, "/api/workspaces", host="evil.example.com")
    assert status == 403
    assert "host" in payload["error"].lower()


def test_change_stage_end_to_end(base_url):
    lead_id = _add_lead()
    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/stage", method="POST",
        body={"stage": "interested", "workspace": "alpha", "sentiment": "positive"},
        headers=CSRF)
    assert status == 200, payload
    assert payload == {
        "status": "updated", "id": lead_id, "stage": "interested", "workspace": "alpha",
    }
    conn = om.get_conn()
    try:
        lead = conn.execute("SELECT stage FROM leads WHERE id = ?", (lead_id,)).fetchone()
        wl = conn.execute(
            "SELECT status, current_status_sentiment FROM workspace_leads WHERE lead_id = ?",
            (lead_id,)).fetchone()
    finally:
        conn.close()
    assert lead["stage"] == "interested"
    assert wl["status"] == "interested"
    assert wl["current_status_sentiment"] == "positive"


def test_change_stage_validation_errors(base_url):
    lead_id = _add_lead()
    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/stage", method="POST",
        body={"stage": "bogus", "workspace": "alpha"}, headers=CSRF)
    assert status == 400
    assert "Invalid stage" in payload["error"]

    status, payload = _request(
        base_url, "/api/leads/999999/stage", method="POST",
        body={"stage": "contacted", "workspace": "alpha"}, headers=CSRF)
    assert status == 400
    assert "lead not found" in payload["error"]


def test_enrich_end_to_end(base_url):
    lead_id = _add_lead()
    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/enrich", method="POST",
        body={"title": "VP Sales", "industry": "SaaS"}, headers=CSRF)
    assert status == 200, payload
    assert sorted(payload["filled"]) == ["industry", "title"]

    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/enrich", method="POST",
        body={"favorite_color": "green"}, headers=CSRF)
    assert status == 400
    assert "Unknown enrich fields" in payload["error"]


def test_log_event_end_to_end(base_url):
    lead_id = _add_lead()
    status, payload = _request(
        base_url, f"/api/leads/{lead_id}/events", method="POST",
        body={"event_type": "email_sent", "subject": "Hi", "workspace": "alpha"},
        headers=CSRF)
    assert status == 200, payload
    assert payload == {"status": "logged", "lead_id": lead_id, "workspace": "alpha"}
    conn = om.get_conn()
    try:
        wse = conn.execute(
            "SELECT idempotency_key FROM workspace_lead_events WHERE lead_id = ?",
            (lead_id,)).fetchone()
    finally:
        conn.close()
    assert wse["idempotency_key"].startswith("dashboard_")


def test_sync_endpoints_and_concurrency(base_url, monkeypatch):
    release = threading.Event()

    def slow_pull():
        release.wait(timeout=10)
        return {"imported": 3, "skipped": 0}

    monkeypatch.setattr(dashboard_actions.sync_manager, "_run_pull", slow_pull)

    status, payload = _request(base_url, "/api/sync/pull", method="POST",
                               body={}, headers=CSRF)
    assert status == 202
    assert payload["state"] == "running" and payload["kind"] == "pull"

    status, payload = _request(base_url, "/api/sync/pull", method="POST",
                               body={}, headers=CSRF)
    assert status == 409

    release.set()
    for _ in range(100):
        status, payload = _request(base_url, "/api/sync/status")
        if payload["state"] != "running":
            break
        time.sleep(0.05)
    assert payload["state"] == "done"
    assert payload["summary"] == {"imported": 3, "skipped": 0}


def test_sync_error_state(base_url, monkeypatch):
    def failing_push(workspace_slug=None):
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(dashboard_actions.sync_manager, "_run_push", failing_push)
    status, payload = _request(base_url, "/api/sync/push", method="POST",
                               body={"workspace": "alpha"}, headers=CSRF)
    assert status == 202
    for _ in range(100):
        status, payload = _request(base_url, "/api/sync/status")
        if payload["state"] != "running":
            break
        time.sleep(0.05)
    assert payload["state"] == "error"
    assert "relay unreachable" in payload["error"]


def test_new_read_endpoints_return_json(base_url):
    lead_id = _add_lead()
    import lead_actions
    lead_actions.log_event_scoped(
        lead_id, "email_sent", subject="Hi", workspace_slug="alpha",
        metadata={"body": "Full body text", "campaign": "alpha | x"})
    for path in (
        f"/api/leads/{lead_id}/history?workspace=alpha",
        "/api/campaigns/daily?workspace=alpha",
        "/api/crm?workspace=alpha",
        "/api/outbox",
        "/api/outbox?workspace=alpha",
    ):
        status, payload = _request(base_url, path)
        assert status == 200, (path, payload)
        assert isinstance(payload, dict)

    status, history = _request(base_url, f"/api/leads/{lead_id}/history?workspace=alpha")
    event_id = history["events"][0]["id"]
    status, body = _request(base_url, f"/api/events/{event_id}/body")
    assert status == 200
    assert body["body"] == "Full body text"

    status, daily = _request(base_url, "/api/campaigns/daily?workspace=alpha")
    assert daily["totals"]["email_sent"] == 1

    status, payload = _request(base_url, "/api/campaigns/detail?workspace=alpha")
    assert status == 400  # campaign_id required


def test_crm_sync_endpoint(base_url, monkeypatch):
    calls = {}

    def fake_crm(workspace_slug=None, lead_id=None, max_age=None):
        calls.update(workspace=workspace_slug, lead_id=lead_id, max_age=max_age)
        return {"workspace": workspace_slug, "results": []}

    monkeypatch.setattr(dashboard_actions.sync_manager, "_run_crm", fake_crm)
    status, payload = _request(
        base_url, "/api/crm/sync", method="POST",
        body={"workspace": "alpha", "max_age": "30d"}, headers=CSRF)
    assert status == 202
    assert payload["kind"] == "crm"
    for _ in range(100):
        status, payload = _request(base_url, "/api/sync/status")
        if payload["state"] != "running":
            break
        time.sleep(0.05)
    assert payload["state"] == "done"
    assert calls == {"workspace": "alpha", "lead_id": None, "max_age": "30d"}


def test_contacts_and_campaign_leads_endpoints(base_url):
    lead_id = _add_lead()
    import lead_actions
    lead_actions.log_event_scoped(
        lead_id, "email_sent", subject="Hi", workspace_slug="alpha",
        metadata={"campaign": "alpha | ep"})
    status, payload = _request(base_url, "/api/contacts?workspace=alpha&q=Pat")
    assert status == 200
    assert payload["total"] == 1 and payload["leads"][0]["name"] == "Pat Tester"

    status, payload = _request(base_url, "/api/contacts?workspace=alpha&missing=title")
    assert status == 200 and isinstance(payload["leads"], list)

    # campaign leads requires campaign_id
    status, payload = _request(base_url, "/api/campaigns/leads?workspace=alpha")
    assert status == 400


def test_company_edit_and_link(base_url):
    # seed a lead with a company, and an unlinked lead
    import pipeline as om2
    om2.add_lead("Linked Lead", company="Globex Inc", email="ll@globex2-example.com")
    unlinked = om2.add_lead("Orphan Lead", email="orphan@nowhere2-example.com")["id"]
    import lead_actions
    lead_actions.log_event_scoped(unlinked, "email_sent", workspace_slug="alpha")

    # find the Globex company id
    status, companies = _request(base_url, "/api/companies/search?q=Globex")
    assert status == 200 and companies["companies"]
    cid = companies["companies"][0]["id"]

    # edit the company
    status, payload = _request(base_url, f"/api/companies/{cid}/edit", method="POST",
                               body={"industry": "Software", "headcount": "500"}, headers=CSRF)
    assert status == 200 and sorted(payload["fields"]) == ["headcount", "industry"]

    # link the orphan lead to it
    status, payload = _request(base_url, f"/api/leads/{unlinked}/link-company", method="POST",
                               body={"company_id": cid}, headers=CSRF)
    assert status == 200 and payload["status"] == "linked" and payload["company_id"] == cid

    conn = om.get_conn()
    try:
        row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (unlinked,)).fetchone()
        comp = conn.execute("SELECT industry FROM companies WHERE id = ?", (cid,)).fetchone()
    finally:
        conn.close()
    assert row["company_id"] == cid
    assert comp["industry"] == "Software"


def test_activity_search_endpoints(base_url):
    import lead_actions
    lead = om.add_lead("Search Me", company="Findable Co", email="sm@findable-example.com")["id"]
    lead_actions.log_event_scoped(lead, "email_sent", subject="Hello", workspace_slug="alpha")
    lead_actions.log_event_scoped(
        lead, "email_reply", direction="inbound", subject="Re: Hello", workspace_slug="alpha")

    status, payload = _request(base_url, "/api/activity?workspace=alpha&q=Search")
    assert status == 200 and len(payload["events"]) == 2

    status, payload = _request(base_url, "/api/activity?workspace=alpha&event_type=email_reply")
    assert status == 200 and len(payload["events"]) == 1
    assert payload["events"][0]["event_type"] == "email_reply"

    status, payload = _request(base_url, "/api/activity?workspace=alpha&q=findable-example.com")
    assert status == 200 and payload["events"], payload

    status, payload = _request(base_url, "/api/activity/types?workspace=alpha")
    assert status == 200
    assert {t["event_type"] for t in payload["event_types"]} == {"email_sent", "email_reply"}


def test_data_quality_and_enrich_targets_endpoints(base_url):
    import lead_actions
    lead = om.add_lead("DQ Lead", company="DQ Co", email="")["id"]
    lead_actions.log_event_scoped(lead, "email_sent", workspace_slug="alpha")
    status, payload = _request(base_url, "/api/data-quality?workspace=alpha")
    assert status == 200, payload
    assert "buckets" in payload and "junk_deletable" in payload
    assert any(b["key"] == "missing_email" for b in payload["buckets"])

    status, payload = _request(
        base_url, f"/api/enrich/targets?workspace=alpha&lead_ids={lead}")
    assert status == 200
    assert payload["targets"][0]["lead_id"] == lead
    assert payload["targets"][0]["has_email"] is False


def test_bulk_link_company_endpoint(base_url):
    import lead_actions
    lead = om.add_lead("Bulk Lead", company="Bulklink Inc", email="b@bulk-example.com")["id"]
    lead_actions.log_event_scoped(lead, "email_sent", workspace_slug="alpha")
    conn = om.get_conn()
    try:
        conn.execute("UPDATE leads SET company_id = NULL WHERE id = ?", (lead,))
        conn.commit()
    finally:
        conn.close()
    status, payload = _request(
        base_url, "/api/leads/bulk-link-company", method="POST",
        body={"lead_ids": [lead]}, headers=CSRF)
    assert status == 200, payload
    assert payload["linked"] == 1
    conn = om.get_conn()
    try:
        row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead,)).fetchone()
    finally:
        conn.close()
    assert row["company_id"] is not None


def test_cleanup_preview_is_dry_run(base_url):
    status, payload = _request(base_url, "/api/cleanup/preview")
    assert status == 200, payload
    assert payload["dry_run"] is True
    assert "selected" in payload and payload["deleted"] == 0


def test_email_finder_and_serper_routing(base_url, monkeypatch):
    # Don't hit real providers: stub the background runners, verify routing.
    def fake_finder(workspace_slug, lead_ids, domains=None, force=False):
        return {"found": len(lead_ids), "workspace": workspace_slug}

    monkeypatch.setattr(dashboard_actions.sync_manager, "_run_email_finder", fake_finder)
    status, payload = _request(
        base_url, "/api/enrich/email-finder", method="POST",
        body={"workspace": "alpha", "lead_ids": [1, 2]}, headers=CSRF)
    assert status == 202 and payload["kind"] == "email-finder"
    for _ in range(100):
        status, st = _request(base_url, "/api/sync/status")
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["summary"]["found"] == 2

    def fake_serper(workspace_slug, lead_ids, force=False):
        return {"searched": len(lead_ids)}

    monkeypatch.setattr(dashboard_actions.sync_manager, "_run_serper", fake_serper)
    status, payload = _request(
        base_url, "/api/enrich/serper", method="POST",
        body={"workspace": "alpha", "lead_ids": [3]}, headers=CSRF)
    assert status == 202 and payload["kind"] == "serper"


def test_outbox_detail_endpoint(base_url):
    lead_id = _add_lead()
    # find the queued lead_core row
    status, ob = _request(base_url, "/api/outbox")
    assert status == 200
    row = next((r for r in ob["rows"] if r["entity_type"] == "lead_core"
                and r["entity_id"] == str(lead_id)), None)
    assert row is not None, ob["rows"]
    status, payload = _request(
        base_url,
        f"/api/outbox/detail?entity_type=lead_core&entity_id={lead_id}")
    assert status == 200, payload
    assert payload["record_exists"] is True
    assert payload["record"]["email"] == "pat@acme-example.com"
    assert isinstance(payload["payload"], dict)

    status, payload = _request(base_url, "/api/outbox/detail?entity_type=lead_core")
    assert status == 400  # entity_id required


def test_edit_sender_and_domain(base_url):
    conn = om.get_conn()
    try:
        from pipeline_sender_accounts import ensure_sender_account
        ensure_sender_account(conn, "ops@editable-example.com")
        conn.commit()
    finally:
        conn.close()
    status, payload = _request(base_url, "/api/senders/edit", method="POST",
                               body={"email": "ops@editable-example.com",
                                     "provider": "Google", "first_name": "Ops"},
                               headers=CSRF)
    assert status == 200, payload
    status, payload = _request(base_url, "/api/domains/edit", method="POST",
                               body={"domain": "editable-example.com", "reseller": "ResellerX",
                                     "domain_cost": 12.0, "notes": "batch 9"}, headers=CSRF)
    assert status == 200, payload
    conn = om.get_conn()
    try:
        d = conn.execute("SELECT reseller, notes FROM sender_domains WHERE domain = ?",
                         ("editable-example.com",)).fetchone()
    finally:
        conn.close()
    assert d["reseller"] == "ResellerX" and d["notes"] == "batch 9"
