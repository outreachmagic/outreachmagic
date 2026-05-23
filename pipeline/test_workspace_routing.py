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
from relay_extractors import extract_relay_fields  # noqa: E402
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


def test_prosp_camelcase_campaign_fields_are_extracted():
    raw = {
        "eventType": "send_connection",
        "eventData": {
            "campaignId": "43795293-6fcf-444b-b246-04671a947fcd",
            "campaignName": "popcam | nace",
            "lead": "https://www.linkedin.com/in/ashley-m-rose-mba-07a36913",
            "profileInfo": {
                "linkedinUrl": "https://www.linkedin.com/in/ashley-m-rose-mba-07a36913",
            },
        },
    }
    extracted = extract_relay_fields("prosp", raw)
    event_fields = extracted.get("event", {})
    assert event_fields.get("campaign_id") == "43795293-6fcf-444b-b246-04671a947fcd"
    assert event_fields.get("campaign_name") == "popcam | nace"

    ctx = extract_campaign_context("prosp", event_fields, raw)
    assert ctx.campaign_id == "43795293-6fcf-444b-b246-04671a947fcd"
    assert ctx.campaign_name_raw == "popcam | nace"
    assert ctx.campaign_name_normalized == "popcam | nace"


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


def test_multi_mode_no_default_workspace_on_init():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    conn = om.get_conn()
    config = om.get_org_routing_config(conn, DEFAULT_ORG_ID)
    conn.close()
    routing = om.get_workspace_routing()
    assert config.mode == WORKSPACE_ROUTING_MULTI
    assert config.default_workspace_id is None
    assert "default_workspace_id" not in routing
    assert routing.get("message")


def test_multi_mode_quarantines_unmapped():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_id="c1", campaign_name="Alpha")
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
    conn = om.get_conn()
    events_before = conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0]
    conn.close()
    assert om.ingest_relay_event(event, quiet=True) is None
    conn = om.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE email = ?", ("ghost@test.com",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0] == events_before
    conn.close()
    pending = om.list_quarantine()
    assert any("Ghost Campaign" in (p.get("campaign_name_raw") or "") for p in pending)
    assert any("message" in p for p in pending)
    assert any("not processed" in (p.get("message") or "") for p in pending)


def test_multi_mode_resolves_mapped_campaign():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_id="c1", campaign_name="Alpha")
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


def test_quarantine_summary_requires_rules_or_manual_mapping():
    summary = om.format_quarantine_campaign_summary(
        [
            {
                "source_platform": "smartlead",
                "campaign": "Campaign One",
                "campaign_id": "c1",
                "event_count": 2,
            },
            {
                "source_platform": "plusvibe",
                "campaign": "Campaign Two",
                "campaign_id": "",
                "event_count": 1,
            },
        ]
    )
    assert "Default" not in summary
    assert "either a campaign rule or a manual mapping" in summary
    assert "campaign-map add" in summary


def test_ingest_quarantine_and_route():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_id="c1", campaign_name="Alpha")
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


def test_replay_pending_quarantine_applies_prefix_rules():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("LeadgenPH", slug="leadgenph")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "prefix@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 301,
        "raw": {"campaign_name": "leadgenph scholarship", "to_email": "prefix@test.com"},
    }
    assert om.ingest_relay_event(event, quiet=True) is None
    pending_before = om.list_quarantine()
    assert any((row.get("campaign_name_raw") or "") == "leadgenph scholarship" for row in pending_before)

    om.add_campaign_map_cli(
        "smartlead",
        "leadgenph",
        campaign_name="leadgenph",
        match_strategy="rule_prefix",
    )
    result = om.replay_pending_quarantine(limit=100)
    assert result["replayed"] >= 1

    pending_after = om.list_quarantine()
    assert not any((row.get("campaign_name_raw") or "") == "leadgenph scholarship" for row in pending_after)
    conn = om.get_conn()
    event_count = conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0]
    conn.close()
    assert event_count >= 1


def test_replay_pending_quarantine_applies_unknown_name_mapping():
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Popcam", slug="popcam")
    event = {
        "platform": "prosp",
        "event_type": "linkedin_message",
        "lead": "https://linkedin.com/in/unknown-campaign",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 302,
        "raw": {},
    }
    assert om.ingest_relay_event(event, quiet=True) is None
    om.add_campaign_map_cli("prosp", "popcam", campaign_name="unknown", match_strategy="name_exact")

    result = om.replay_pending_quarantine(limit=100)
    assert result["replayed"] >= 1
    pending_after = om.list_quarantine()
    assert not any(row.get("source_platform") == "prosp" for row in pending_after)


def test_campaign_stats_splits_workspace_and_counts_interested():
    om.init_db()
    lead_a = om.add_lead(name="Lead A", email="leada@test.com").get("id")
    lead_b = om.add_lead(name="Lead B", email="leadb@test.com").get("id")
    assert lead_a and lead_b

    conn = om.get_conn()
    campaign_id = om.ensure_campaign(conn, "leadgenph | scholarship", int(lead_a))
    conn.execute(
        "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
        (campaign_id, int(lead_b)),
    )
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, campaign_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(lead_a), "lead_status_updated", "inbound", "email", '{"lead_status_raw":"interested"}', campaign_id),
    )
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, campaign_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(lead_b), "email_sent", "outbound", "email", "{}", campaign_id),
    )
    conn.commit()
    conn.close()

    stats = om.get_campaign_stats()
    row = next((c for c in stats["campaigns"] if c.get("campaign") == "leadgenph | scholarship"), None)
    assert row is not None
    assert row.get("workspace") == "leadgenph"
    assert row.get("campaign_name") == "scholarship"
    assert row.get("event_count") == 2
    assert row.get("lead_count") == 2
    assert row.get("interested_count") == 1
    assert row.get("event_type_counts", {}).get("email_sent") == 1
    assert row.get("event_type_counts", {}).get("lead_status_updated") == 1
    assert row.get("direction_counts", {}).get("outbound") == 1
    assert row.get("direction_counts", {}).get("inbound") == 1
    assert row.get("channel_counts", {}).get("email") == 2
    assert row.get("event_summary")
    assert "types:" in row.get("event_summary", "")

    rendered = "\n".join(om.format_campaign_stats(stats, include_header=False))
    assert "Workspace" in rendered
    assert "Interested" in rendered
    assert "leadgenph" in rendered
    assert "scholarship" in rendered


if __name__ == "__main__":
    test_normalization()
    test_single_mode_routes_all_to_default()
    test_campaign_routing()
    test_prosp_camelcase_campaign_fields_are_extracted()
    test_multi_mode_no_default_workspace_on_init()
    test_multi_mode_quarantines_unmapped()
    test_multi_mode_resolves_mapped_campaign()
    test_quarantine_summary_requires_rules_or_manual_mapping()
    test_ingest_quarantine_and_route()
    test_replay_pending_quarantine_applies_prefix_rules()
    test_replay_pending_quarantine_applies_unknown_name_mapping()
    test_campaign_stats_splits_workspace_and_counts_interested()
    print("ok")
