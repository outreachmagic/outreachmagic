#!/usr/bin/env python3
"""Lightweight tests for workspace routing (run: python3 test_workspace_routing.py)."""

import os
import sqlite3
import tempfile
from pathlib import Path

# Use isolated DB
_tmp = tempfile.mkdtemp()
os.environ["HERMES_HOME"] = _tmp
os.environ["OUTREACHMAGIC_SKIP_AUTO_UPDATE"] = "1"

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    extract_campaign_context,
    format_unmapped_campaign_message,
    normalize_campaign_name,
    normalize_email,
    normalize_linkedin,
    resolve_workspace,
    resolve_workspace_for_ingest,
)


def test_normalization():
    assert normalize_email("  Jane@Example.COM ") == "jane@example.com"
    assert normalize_linkedin("https://www.linkedin.com/in/jane/") == "linkedin.com/in/jane"
    assert normalize_campaign_name("  Foo   Bar  ") == "foo bar"


def test_campaign_routing():
    om.init_db()
    conn = om.get_conn()
    ws_id = om.ensure_default_org_workspace(conn)
    om.assign_campaign_map(
        conn,
        DEFAULT_ORG_ID,
        source_platform="heyreach",
        workspace_id=ws_id,
        campaign_id="hr_99",
        match_strategy="id_exact",
    )
    conn.commit()
    ctx = extract_campaign_context(
        "heyreach",
        {"campaign_id": "hr_99", "campaign_name": "Outbound"},
        {"campaign_id": "hr_99"},
    )
    result = resolve_workspace(conn, DEFAULT_ORG_ID, ctx)
    conn.close()
    assert result is not None
    assert result.workspace_id == ws_id
    assert result.match_strategy == "id_exact"


def test_single_mode_routes_all_to_default():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_SINGLE, workspace_slug="default")
    om.add_campaign_map_cli("smartlead", "default", campaign_id="c1", campaign_name="Alpha")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "alice@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 200,
        "raw": {"campaign_id": "unmapped-99", "to_email": "alice@test.com"},
    }
    lead_id = om.ingest_relay_event(event, quiet=True)
    assert lead_id is not None
    conn = om.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM unmapped_campaign_queue").fetchone()[0] == 0
    conn.close()


def test_multi_mode_quarantines_unmapped():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.add_campaign_map_cli("smartlead", "default", campaign_id="c1", campaign_name="Alpha")
    ctx = extract_campaign_context(
        "smartlead",
        {"campaign_id": "missing", "campaign_name": "Ghost Campaign"},
        {"campaign_id": "missing", "campaign_name": "Ghost Campaign"},
    )
    msg = format_unmapped_campaign_message(ctx)
    assert "Ghost Campaign" in msg
    assert "campaign-map add" in msg
    assert "quarantine assign" in msg

    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "ghost@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 201,
        "raw": {"campaign_id": "missing", "campaign_name": "Ghost Campaign", "to_email": "ghost@test.com"},
    }
    assert om.ingest_relay_event(event, quiet=True) is None
    pending = om.list_quarantine()
    assert any("Ghost Campaign" in (p.get("campaign_name_raw") or "") for p in pending)
    assert any("message" in p for p in pending)


def test_multi_mode_resolves_mapped_campaign():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.add_campaign_map_cli("smartlead", "default", campaign_id="c1", campaign_name="Alpha")
    conn = om.get_conn()
    ctx = extract_campaign_context(
        "smartlead",
        {"campaign_id": "c1"},
        {"campaign_id": "c1"},
    )
    result = resolve_workspace_for_ingest(conn, DEFAULT_ORG_ID, ctx)
    conn.close()
    assert result is not None
    assert result.match_strategy == "id_exact"


def test_ingest_quarantine_and_route():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.add_campaign_map_cli("smartlead", "default", campaign_id="c1", campaign_name="Alpha")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "bob@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 42,
        "raw": {"campaign_id": "c1", "to_email": "bob@test.com"},
    }
    lead_id = om.ingest_relay_event(event, quiet=True)
    assert lead_id is not None
    conn = om.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0]
    conn.close()
    assert n >= 1

    bad = dict(event)
    bad["relay_id"] = 43
    bad["raw"] = {"campaign_id": "missing", "to_email": "bob@test.com"}
    assert om.ingest_relay_event(bad, quiet=True) is None
    pending = om.list_quarantine()
    assert len(pending) >= 1


if __name__ == "__main__":
    test_normalization()
    test_single_mode_routes_all_to_default()
    test_campaign_routing()
    test_multi_mode_quarantines_unmapped()
    test_multi_mode_resolves_mapped_campaign()
    test_ingest_quarantine_and_route()
    print("ok")
