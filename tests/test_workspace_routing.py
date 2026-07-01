#!/usr/bin/env python3
"""Lightweight tests for workspace routing."""

import os
import sys
import tempfile
from pathlib import Path

# Tests use an isolated temp data root; never pull live routing from env agent keys.
os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from relay_extractors import extract_relay_fields  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    build_import_identities,
    build_import_key_fingerprint,
    extract_campaign_context,
    find_lead_by_identity,
    format_unmapped_campaign_message,
    lead_entity_key,
    normalize_campaign_name,
    normalize_email,
    normalize_external_id,
    normalize_linkedin,
    parse_entity_key,
    parse_linkedin_value,
    resolve_workspace,
    resolve_workspace_for_ingest,
)


def _reset_db():
    """Fresh SQLite per test."""
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_normalization():
    assert normalize_email("  Jane@Example.COM ") == "jane@example.com"
    assert normalize_linkedin("https://www.linkedin.com/in/jane/") == "linkedin.com/in/jane"
    assert normalize_linkedin("amy-hudock") == "linkedin.com/in/amy-hudock"
    assert normalize_campaign_name("  Foo   Bar  ") == "foo bar"


def test_parse_linkedin_value():
    sales = "ACwAAAN-JhcBW3CyV59ymKdIvvot8il9llc-L8w"
    parsed = dict(parse_linkedin_value("urn:li:member:58598935"))
    assert parsed.get("linkedin_member_id") == "58598935"
    parsed = dict(parse_linkedin_value(f"urn:li:fs_salesProfile:({sales},NAME_SEARCH,OWos)"))
    assert parsed.get("linkedin_sales_nav_id") == sales
    parsed = dict(parse_linkedin_value(sales))
    assert parsed.get("linkedin_sales_nav_id") == sales
    assert "linkedin_url" not in parsed
    parsed = dict(parse_linkedin_value("https://www.linkedin.com/in/jane/"))
    assert parsed.get("linkedin_url") == "linkedin.com/in/jane"
    parsed = dict(parse_linkedin_value(f"linkedin.com/in/{sales}"))
    assert parsed.get("linkedin_sales_nav_id") == sales
    assert "linkedin_url" not in parsed


def test_linkedin_sales_nav_then_public_slug_same_lead():
    _reset_db()
    sales = "ACwAAAN-JhcBW3CyV59ymKdIvvot8il9llc-L8w"
    r1 = om.resolve_lead(
        name="Jane", email="jane-linkedin@test.com",
        linkedin_url=sales,
    )
    assert r1["status"] == "created"
    conn = om.get_conn()
    row = conn.execute(
        "SELECT linkedin_url FROM leads WHERE id = ?", (r1["id"],),
    ).fetchone()
    assert not row["linkedin_url"]
    r2 = om.resolve_lead(
        name="Jane", email="jane-linkedin@test.com",
        linkedin_url="https://www.linkedin.com/in/jane-doe",
    )
    assert r2["status"] == "matched"
    assert r2["id"] == r1["id"]
    row = conn.execute(
        "SELECT linkedin_url FROM leads WHERE id = ?", (r1["id"],),
    ).fetchone()
    conn.close()
    assert row["linkedin_url"] == "linkedin.com/in/jane-doe"


def test_campaign_routing():
    _reset_db()
    conn = om.get_conn()
    ws_id = om.ensure_default_org_workspace(conn)
    om.assign_campaign_map(
        conn,
        DEFAULT_ORG_ID,
        source_platform="heyreach",
        workspace_id=ws_id,
        campaign_platform_id="hr_99",
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


def test_heyreach_campaign_extraction_from_raw_payload():
    raw = {
        "event_type": "message_sent",
        "correlation_id": "test-123",
        "sender": {"profile_url": "https://linkedin.com/in/sender"},
        "lead": {"profile_url": "https://linkedin.com/in/lead"},
        "campaign": {
            "name": "Malaysia - HC Decrease Signal",
            "id": 425166,
            "status": 1,
        },
    }
    extracted = extract_relay_fields("heyreach", raw)
    event_fields = extracted.get("event", {})
    assert event_fields.get("campaign_name") == "Malaysia - HC Decrease Signal"
    assert event_fields.get("campaign_id") == "425166"

    ctx = extract_campaign_context("heyreach", event_fields, raw)
    assert ctx.campaign_platform_id == "425166"
    assert ctx.campaign_name_raw == "Malaysia - HC Decrease Signal"
    assert ctx.campaign_name_normalized == "malaysia - hc decrease signal"


def test_calendly_scheduled_event_fields_are_extracted():
    raw = {
        "event": "invitee.created",
        "payload": {
            "email": "spencer@outreachmagic.io",
            "name": "Spencer Testing",
            "tracking": {"utm_campaign": None},
            "scheduled_event": {
                "name": "Acme Corp Discovery Call ",
                "event_type": "https://api.calendly.com/event_types/9907c70d-fd3d-4f8f-8613-d834ddf4dae4",
            },
        },
    }
    extracted = extract_relay_fields("calendly", raw)
    event_fields = extracted.get("event", {})
    assert event_fields.get("campaign_name") == "Acme Corp Discovery Call"
    assert "9907c70d" in (event_fields.get("campaign_id") or "")

    ctx = extract_campaign_context("calendly", event_fields, raw)
    assert ctx.campaign_platform_id == "9907c70d-fd3d-4f8f-8613-d834ddf4dae4"
    assert ctx.campaign_name_raw == "Acme Corp Discovery Call"
    assert ctx.campaign_name_normalized == "acme corp discovery call"


def test_calendly_utm_does_not_change_routing_campaign():
    raw = {
        "event": "invitee.created",
        "payload": {
            "scheduled_event": {
                "name": "Shared Demo",
                "event_type": "https://api.calendly.com/event_types/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
            "tracking": {"utm_campaign": "acme", "utm_source": "instantly"},
        },
    }
    ctx = extract_campaign_context("calendly", {}, raw)
    assert ctx.campaign_platform_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert ctx.campaign_name_raw == "Shared Demo"
    assert ctx.campaign_name_normalized == "shared demo"


def test_calendly_meeting_note_includes_details_and_utm():
    from relay_ingest import build_calendly_meeting_note

    raw = {
        "event": "invitee.created",
        "payload": {
            "name": "Jane Doe",
            "email": "jane@acme.com",
            "timezone": "America/New_York",
            "scheduled_event": {
                "name": "Acme Corp Discovery Call",
                "start_time": "2026-06-18T19:30:00.000000Z",
                "end_time": "2026-06-18T20:00:00.000000Z",
                "status": "active",
                "event_memberships": [
                    {"user_name": "Alex Host", "user_email": "alex@acme.com"},
                ],
                "location": {"type": "google_conference"},
            },
            "tracking": {"utm_campaign": "acme", "utm_source": "instantly", "utm_medium": "email"},
            "questions_and_answers": [{"question": "Company", "answer": "Acme Corp"}],
        },
    }
    note = build_calendly_meeting_note(raw, "invitee.created")
    assert "Acme Corp Discovery Call" in note
    assert "Invitee: Jane Doe (jane@acme.com)" in note
    assert "Hosts: Alex Host (alex@acme.com)" in note
    assert "Location: Google Meet" in note
    assert "UTM: campaign=acme · source=instantly · medium=email" in note
    assert "Question: Company → Acme Corp" in note


def test_prosp_camelcase_campaign_fields_are_extracted():
    raw = {
        "eventType": "send_connection",
        "eventData": {
            "campaignId": "43795293-6fcf-444b-b246-04671a947fcd",
            "campaignName": "acme_corp | nace",
            "lead": "https://www.linkedin.com/in/ashley-m-rose-mba-07a36913",
            "profileInfo": {
                "linkedinUrl": "https://www.linkedin.com/in/ashley-m-rose-mba-07a36913",
            },
        },
    }
    extracted = extract_relay_fields("prosp", raw)
    event_fields = extracted.get("event", {})
    assert event_fields.get("campaign_id") == "43795293-6fcf-444b-b246-04671a947fcd"
    assert event_fields.get("campaign_name") == "acme_corp | nace"

    ctx = extract_campaign_context("prosp", event_fields, raw)
    assert ctx.campaign_platform_id == "43795293-6fcf-444b-b246-04671a947fcd"
    assert ctx.campaign_name_raw == "acme_corp | nace"
    assert ctx.campaign_name_normalized == "acme_corp | nace"


def test_single_mode_routes_all_to_default():
    _reset_db()
    # Force single mode via config sync (set_workspace_routing blocks multi → single).
    cfg = om.load_config()
    cfg["workspace_routing_mode"] = WORKSPACE_ROUTING_SINGLE
    om.save_config(cfg)
    om.sync_workspace_routing_mode_from_config()
    om.add_campaign_map_cli("smartlead", "default", campaign_platform_id="c1", campaign_name="Alpha")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "alice@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 200,
        "payload": {"campaign_id": "unmapped-99", "to_email": "alice@test.com"},
    }
    lead_id = om.ingest_relay_event(event, quiet=True)
    assert lead_id is not None
    conn = om.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM unmapped_campaign_queue").fetchone()[0] == 0
    conn.close()


def test_multi_mode_no_default_workspace_on_init():
    _reset_db()
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
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_platform_id="c1", campaign_name="Alpha")
    ctx = extract_campaign_context(
        "smartlead",
        {"campaign_id": "missing", "campaign_name": "Ghost Campaign"},
        {"campaign_id": "missing", "campaign_name": "Ghost Campaign"},
    )
    msg = format_unmapped_campaign_message(ctx)
    assert "Ghost Campaign" in msg
    assert "Ask Outreach Magic" in msg
    assert "quarantine" in msg.lower()

    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "ghost@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 201,
        "payload": {"campaign_id": "missing", "campaign_name": "Ghost Campaign", "to_email": "ghost@test.com"},
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
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_platform_id="c1", campaign_name="Alpha")
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
    assert "Ask Outreach Magic" in summary
    assert "replay quarantined events" in summary


def test_ingest_quarantine_and_route():
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("smartlead", "alpha", campaign_platform_id="c1", campaign_name="Alpha")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "bob@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 42,
        "payload": {"campaign_id": "c1", "to_email": "bob@test.com"},
    }
    lead_id = om.ingest_relay_event(event, quiet=True)
    assert lead_id is not None
    conn = om.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM workspace_lead_events").fetchone()[0]
    conn.close()
    assert n >= 1

    bad = dict(event)
    bad["relay_id"] = 43
    bad["payload"] = {"campaign_id": "missing", "to_email": "bob@test.com"}
    assert om.ingest_relay_event(bad, quiet=True) is None
    pending = om.list_quarantine()
    assert len(pending) >= 1


def test_replay_pending_quarantine_applies_prefix_rules():
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("LeadgenPH", slug="leadgenph")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "prefix@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 301,
        "payload": {"campaign_name": "leadgenph scholarship", "to_email": "prefix@test.com"},
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


def test_replay_pending_quarantine_applies_contains_rules():
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Acme", slug="acme")
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "contains@test.com",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 303,
        "payload": {"campaign_name": "Q2 Acme outbound", "to_email": "contains@test.com"},
    }
    assert om.ingest_relay_event(event, quiet=True) is None

    om.add_campaign_map_cli(
        "smartlead",
        "acme",
        campaign_name="acme",
        match_strategy="rule_contains",
    )
    result = om.replay_pending_quarantine(limit=100)
    assert result["replayed"] >= 1

    pending_after = om.list_quarantine()
    assert not any((row.get("campaign_name_raw") or "") == "Q2 Acme outbound" for row in pending_after)


def test_replay_pending_quarantine_applies_unknown_name_mapping():
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Acme Corp", slug="acme_corp")
    event = {
        "platform": "prosp",
        "event_type": "linkedin_message",
        "lead": "https://linkedin.com/in/unknown-campaign",
        "received_at": "2026-05-23T00:00:00Z",
        "relay_id": 302,
        "payload": {},
    }
    assert om.ingest_relay_event(event, quiet=True) is None
    om.add_campaign_map_cli("prosp", "acme_corp", campaign_name="unknown", match_strategy="name_exact")

    result = om.replay_pending_quarantine(limit=100)
    assert result["replayed"] >= 1
    pending_after = om.list_quarantine()
    assert not any(row.get("source_platform") == "prosp" for row in pending_after)


def test_campaign_stats_splits_workspace_and_counts_interested():
    _reset_db()
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
        "UPDATE leads SET stage = 'interested' WHERE id = ?", (int(lead_a),)
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


def test_external_id_namespacing():
    assert normalize_external_id("ABC", "NACE List") == "nace_list:abc"
    assert normalize_external_id("crm:123", "nace") == "crm:123"
    extra = {"external_id": "row-1", "list_source": "nace"}
    idents = build_import_identities({"name": "Jane Doe"}, extra)
    assert idents[0] == ("external_id", "nace:row-1")


def test_import_key_stable_within_batch():
    a = build_import_key_fingerprint(name="Jane", import_batch="batch-a")
    b = build_import_key_fingerprint(name="Jane", import_batch="batch-a")
    c = build_import_key_fingerprint(name="Jane", import_batch="batch-b")
    assert a == b
    assert a != c
    assert a.startswith("om:")


def test_agent_sync_full_payload_roundtrip():
    _reset_db()
    row = {
        "name": "Sync Test",
        "company": "Acme",
        "company_domain": "acme.org",
        "unified_lead_id": "sync-42",
        "list_source": "test",
        "tags": "vip; nace",
        "mailmerge_first_name": "Sync",
        "lead_status": "warm",
        "location_city": "Austin",
    }
    s1 = om.import_profiles([row], workspace="default")
    assert s1["created"] == 1
    lead_id = s1["results"][0]["id"]

    conn = om.get_conn()
    payload = om.build_lead_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert payload.get("external_id", "").startswith("test:")
    assert payload.get("location_city") == "Austin"
    assert "vip" in (payload.get("tags") or [])
    assert payload.get("personalization", {}).get("first_name") == "Sync"

    _reset_db()
    entity_key = f"external_id:{payload['external_id']}"
    replay_conn = om.get_conn()
    om.ingest_agent_entry({
        "platform": "agent",
        "entity_key": entity_key,
        "event_type": "lead_core_update",
        "received_at": "2026-05-27T12:00:00Z",
        "payload": {
            "action": "lead_core_update",
            "client_id": "other-client",
            "timestamp": "2026-05-27T12:00:00Z",
            "data": om.build_lead_core_sync_payload(replay_conn, DEFAULT_ORG_ID, lead_id),
        },
    })
    replay_id = om.ingest_agent_entry({
        "platform": "agent",
        "entity_key": entity_key,
        "event_type": "lead_workspace_update",
        "received_at": "2026-05-27T12:00:00Z",
        "payload": {
            "action": "lead_workspace_update",
            "client_id": "other-client",
            "workspace": "default",
            "timestamp": "2026-05-27T12:00:00Z",
            "data": om.build_lead_workspace_sync_payload(
                replay_conn, DEFAULT_ORG_ID, lead_id, workspace_slug="default",
            ),
        },
    })
    replay_conn.close()
    assert replay_id is not None

    conn = om.get_conn()
    from workspace_routing import find_lead_by_identity

    assert find_lead_by_identity(conn, DEFAULT_ORG_ID, "external_id", payload["external_id"]) == replay_id
    conn.close()


def test_import_profiles_force_lead_id_merges_email_conflict():
    _reset_db()
    s0 = om.import_profiles([{
        "name": "Target Lead",
        "company_domain": "target.com",
    }], workspace="default")
    target_id = s0["results"][0]["id"]
    s1 = om.import_profiles([{
        "name": "Other Lead",
        "email": "shared@acme.com",
        "company_domain": "acme.com",
    }], workspace="default")
    other_id = s1["results"][0]["id"]
    assert other_id != target_id

    s2 = om.import_profiles([{
        "id": target_id,
        "email": "shared@acme.com",
        "tags": "trykitt_attempted",
    }], workspace="default")
    assert s2["matched"] == 1
    conn = om.get_conn()
    row = conn.execute("SELECT email FROM leads WHERE id = ?", (target_id,)).fetchone()
    other = conn.execute("SELECT id FROM leads WHERE id = ?", (other_id,)).fetchone()
    conn.close()
    assert row["email"] == "shared@acme.com"
    assert other is None


def test_import_profiles_force_lead_id_enriches_existing():
    _reset_db()
    s0 = om.import_profiles([{
        "name": "Lucia Stanković",
        "company": "ISAB",
        "company_domain": "isab.berkeley.edu",
        "linkedin": "https://linkedin.com/in/lucia-stankovic",
    }], workspace="default")
    assert s0["created"] == 1
    lead_id = s0["results"][0]["id"]

    s1 = om.import_profiles([{
        "id": lead_id,
        "email": "lucia@berkeley.edu",
        "tags": "trykitt_attempted",
    }], workspace="default")
    assert s1["created"] == 0
    assert s1["matched"] == 1
    assert s1["results"][0]["id"] == lead_id

    conn = om.get_conn()
    row = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["email"] == "lucia@berkeley.edu"


def test_import_profiles_weak_identity_and_entity_key():
    _reset_db()
    row = {
        "name": "Melynie Schiel",
        "company": "ACCJC",
        "company_domain": "accjc.org",
        "unified_lead_id": "ul_test_99",
        "list_source": "nace",
    }
    s1 = om.import_profiles([row], import_batch_id="nace-test")
    assert s1["created"] == 1
    assert s1["processed"] == 1
    lead_id = s1["results"][0]["id"]
    assert s1["results"][0]["match_confidence"] in ("high", "medium", "low")

    s2 = om.import_profiles([row], import_batch_id="nace-test")
    assert s2["matched"] == 1
    assert s2["results"][0]["id"] == lead_id

    conn = om.get_conn()
    ekey = lead_entity_key(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert ekey.startswith("external_id:nace:")

    conn = om.get_conn()
    found = om.find_lead_by_identifier(conn, ekey)
    conn.close()
    assert found == lead_id

    itype, val = parse_entity_key(ekey)
    assert itype == "external_id"
    conn = om.get_conn()
    assert find_lead_by_identity(conn, DEFAULT_ORG_ID, "external_id", val) == lead_id
    conn.close()


def test_import_profiles_persists_row_notes_and_overwrite_behavior():
    _reset_db()
    ws = om.create_workspace("Notes WS", slug="notesws")

    row1 = {
        "email": "notes1@test.com",
        "name": "Notes Lead",
        "company": "Acme",
        "notes": "domain: acme.com",
    }
    s1 = om.import_profiles([row1], workspace="notesws")
    assert s1["created"] == 1
    lead_id = s1["results"][0]["id"]

    conn = om.get_conn()
    n1 = conn.execute("SELECT notes FROM leads WHERE id = ?", (lead_id,)).fetchone()["notes"]
    conn.close()
    assert n1 == "domain: acme.com"

    # Import again with overwrite=False: should not overwrite existing notes.
    row2 = {
        "email": "notes1@test.com",
        "name": "Notes Lead",
        "company": "Acme",
        "notes": "domain: other.com",
    }
    s2 = om.import_profiles([row2], workspace="notesws", overwrite=False)
    assert s2["matched"] == 1

    conn = om.get_conn()
    n2 = conn.execute("SELECT notes FROM leads WHERE id = ?", (lead_id,)).fetchone()["notes"]
    conn.close()
    assert n2 == "domain: acme.com"

    # Import again with overwrite=True: should overwrite notes.
    s3 = om.import_profiles([row2], workspace="notesws", overwrite=True)
    assert s3["matched"] == 1

    conn = om.get_conn()
    n3 = conn.execute("SELECT notes FROM leads WHERE id = ?", (lead_id,)).fetchone()["notes"]
    conn.close()
    assert n3 == "domain: other.com"


def test_import_profiles_accepts_tags_json_list():
    _reset_db()
    ws = om.create_workspace("Tag List WS", slug="tagws")

    row = {
        "email": "taglist@test.com",
        "name": "Tag Lead",
        "company": "Acme",
        "tags": ["vip", "nace"],
    }
    s = om.import_profiles([row], workspace="tagws")
    assert s["created"] == 1
    lead_id = s["results"][0]["id"]

    conn = om.get_conn()
    tags = [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
            (ws["id"], lead_id),
        ).fetchall()
    ]
    conn.close()

    assert "vip" in tags
    assert "nace" in tags
    assert not any("[" in t or "]" in t for t in tags)


def test_parse_tags_value_rejects_bracket_literal_string():
    assert om.parse_tags_value("['nace']") == ["nace"]
    assert om.parse_tags_value('["vip", "nace"]') == ["vip", "nace"]
    assert om.parse_tags_value("nace") == ["nace"]


def test_repair_malformed_tags_fixes_bracket_form():
    _reset_db()
    ws = om.create_workspace("Repair Tag WS", slug="repairtagws")
    lead_id = om.add_lead(name="Repair Lead", email="repairtag@test.com")["id"]
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, ?)""",
        ("wlt_repair_bad", ws["id"], lead_id, "['nace']"),
    )
    conn.commit()
    result = om.repair_malformed_tags(conn)
    after = [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
            (ws["id"], lead_id),
        ).fetchall()
    ]
    conn.close()
    assert result["rows_fixed"] >= 1
    assert after == ["nace"]


def test_campaign_stats_normalizes_linkedin_sent_and_reply_counts():
    _reset_db()
    lead_id = om.add_lead(name="LinkedIn Lead", email="linkedinlead@test.com").get("id")
    assert lead_id

    conn = om.get_conn()
    campaign_id = om.ensure_campaign(conn, "acme_corp | nace", int(lead_id))
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, campaign_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(lead_id), "send_connection", "outbound", "linkedin", "{}", campaign_id),
    )
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, campaign_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(lead_id), "linkedin_message", "outbound", "linkedin", "{}", campaign_id),
    )
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, campaign_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(lead_id), "linkedin_message", "inbound", "linkedin", "{}", campaign_id),
    )
    conn.commit()
    conn.close()

    stats = om.get_campaign_stats()
    row = next((c for c in stats["campaigns"] if c.get("campaign") == "acme_corp | nace"), None)
    assert row is not None
    assert row.get("normalized_event_type_counts", {}).get("linkedin_connection_sent") == 1
    assert row.get("normalized_event_type_counts", {}).get("linkedin_message_sent") == 1
    assert row.get("normalized_event_type_counts", {}).get("linkedin_message_reply") == 1
    assert row.get("linkedin_connections_sent") == 1
    assert row.get("linkedin_messages_sent") == 1
    assert row.get("linkedin_message_replies") == 1
    assert "normalized:" in (row.get("event_summary") or "")


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
    test_campaign_stats_normalizes_linkedin_sent_and_reply_counts()
    test_external_id_namespacing()
    test_import_key_stable_within_batch()
    test_import_profiles_weak_identity_and_entity_key()
    test_import_profiles_persists_row_notes_and_overwrite_behavior()
    test_import_profiles_accepts_tags_json_list()
    test_parse_tags_value_rejects_bracket_literal_string()
    test_repair_malformed_tags_fixes_bracket_form()
    test_agent_sync_full_payload_roundtrip()
    print("ok")
