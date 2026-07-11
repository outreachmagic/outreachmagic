#!/usr/bin/env python3
"""Tests for full-pull snapshot ordering and agent event_log replay."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


def _patch_pull_prefetch(monkeypatch):
    monkeypatch.setattr(om, "prefetch_relay_ingested", lambda keys, conn=None: set())
    monkeypatch.setattr(om, "prefetch_ws_idempotency_keys", lambda conn, org_id, keys: set())


def test_relay_pull_phases_order():
    all_kinds = frozenset({"events", "core", "workspace"})
    assert om._relay_pull_phases(True, True, all_kinds) == ("snapshots", "events")
    assert om._relay_pull_phases(False, True, all_kinds) == ("events", "snapshots")
    assert om._relay_pull_phases(True, False, frozenset({"core"})) == ("snapshots",)
    assert om._relay_pull_phases(False, True, frozenset({"events"})) == ("events",)
    assert om._relay_pull_phases(True, True, frozenset({"events"})) == ("events",)


def test_full_pull_fetches_snapshots_before_events(monkeypatch):
    order: list[str] = []

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            order.append(f"snapshot:{kwargs.get('snapshot_kind')}")
            return {"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}
        order.append("events")
        return {"events": [], "max_id": 0, "has_more_events": False}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    stats = {}
    om.sync_from_relay_org(
        "om_agent_test",
        full=True,
        quiet=True,
        skip_routing_sync=True,
        stats=stats,
    )
    assert stats["pull_phases"] == ["snapshots", "events"]
    first_event = next(i for i, x in enumerate(order) if x == "events")
    assert all(order[i].startswith("snapshot:") for i in range(first_event))
    assert first_event > 0


def test_incremental_pull_fetches_events_before_snapshots(monkeypatch):
    order: list[str] = []

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            order.append(f"snapshot:{kwargs.get('snapshot_kind')}")
            return {"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}
        order.append("events")
        return {"events": [], "max_id": 0, "has_more_events": False}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    stats = {}
    om.sync_from_relay_org(
        "om_agent_test",
        full=False,
        quiet=True,
        skip_routing_sync=True,
        stats=stats,
    )
    assert stats["pull_phases"] == ["events", "snapshots"]
    assert order[0] == "events"
    assert any(x.startswith("snapshot:") for x in order[order.index("events") + 1 :])


def test_agent_sync_payload_from_entity_key_email():
    payload = om._agent_sync_payload_from_entity_key("user@example.com", {})
    assert payload["email"] == "user@example.com"


def test_event_log_bootstraps_lead_from_entity_key():
    om.init_db()
    om.set_workspace_routing("multi")
    om.create_workspace("AcmeCo", "acme", sync=False)
    conn = om.get_conn()
    config = om.get_org_routing_config(conn, DEFAULT_ORG_ID)
    ws_map = om._pull_workspace_slug_map(conn, DEFAULT_ORG_ID)
    conn.close()

    event = {
        "platform": "agent",
        "entity_key": "replay-bootstrap@example.com",
        "event_type": "event_log",
        "received_at": "2026-06-01T12:00:00Z",
        "payload": {
            "action": "event_log",
            "client_id": "remote-replay-client",
            "workspace": "acme",
            "timestamp": "2026-06-01T12:00:00Z",
            "data": {
                "event_type": "email_sent",
                "direction": "outbound",
                "channel": "email",
                "campaign": "acme | headshot lounge",
                "body_preview": "Hello",
            },
        },
    }
    lead_id = om.ingest_agent_entry(
        event,
        routing_config=config,
        ws_slug_map=ws_map,
        quiet=True,
    )
    assert lead_id is not None
    conn = om.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    lead = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert int(row["n"]) == 1
    assert lead["email"] == "replay-bootstrap@example.com"


def test_full_pull_replays_event_log_after_core_snapshot(monkeypatch):
    """Regression: event_log must not run before lead_core_update on full pull."""
    om.init_db()
    om.set_workspace_routing("single")
    entity_key = "full-replay@example.com"
    core_event = {
        "platform": "agent",
        "relay_id": 50_001,
        "entity_key": entity_key,
        "event_type": "lead_core_update",
        "received_at": "2026-06-01T10:00:00Z",
        "payload": {
            "action": "lead_core_update",
            "client_id": "upstream-client",
            "timestamp": "2026-06-01T10:00:00Z",
            "data": {
                "email": entity_key,
                "name": "Full Replay",
                "company": "Acme",
            },
        },
    }
    log_event = {
        "platform": "agent",
        "relay_id": 50_002,
        "entity_key": entity_key,
        "event_type": "event_log",
        "received_at": "2026-06-01T11:00:00Z",
        "workspace": "default",
        "payload": {
            "action": "event_log",
            "client_id": "upstream-client",
            "workspace": "default",
            "timestamp": "2026-06-01T11:00:00Z",
            "data": {
                "event_type": "email_sent",
                "direction": "outbound",
                "channel": "email",
                "campaign": "acme | headshot lounge",
                "body_preview": "Hi",
            },
        },
    }

    snapshot_pages = {
        "core": [
            {
                "events": [core_event],
                "max_snapshot_id": 1,
                "has_more_snapshots": False,
            }
        ],
        "workspace": [{"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}],
    }
    event_pages = [
        {"events": [log_event], "max_id": 50_002, "has_more_events": False},
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs["snapshot_kind"]
            pages = snapshot_pages.get(kind) or [{"events": []}]
            page = pages.pop(0) if pages else {"events": []}
            return {
                "events": page.get("events") or [],
                "max_snapshot_id": page.get("max_snapshot_id", 0),
                "has_more_snapshots": page.get("has_more_snapshots", False),
            }
        page = event_pages.pop(0) if event_pages else {"events": []}
        return {
            "events": page.get("events") or [],
            "max_id": page.get("max_id", 0),
            "has_more_events": page.get("has_more_events", False),
        }

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "_snapshot_pending_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)
    _patch_pull_prefetch(monkeypatch)

    imported, _skipped = om.sync_from_relay_org(
        "om_agent_test",
        full=True,
        quiet=True,
        skip_routing_sync=True,
    )
    assert imported >= 1
    conn = om.get_conn()
    count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    meta = conn.execute(
        """SELECT metadata_json FROM events
           WHERE json_extract(metadata_json, '$.source') = 'agent_sync'"""
    ).fetchone()
    ts_row = conn.execute(
        """SELECT created_at FROM events
           WHERE json_extract(metadata_json, '$.source') = 'agent_sync'"""
    ).fetchone()
    wle = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_lead_events WHERE lower(event_type) = 'email_sent'"
    ).fetchone()
    conn.close()
    assert int(count) >= 1
    assert meta is not None
    assert json.loads(meta["metadata_json"]).get("campaign") == "acme | headshot lounge"
    assert ts_row is not None
    assert str(ts_row["created_at"]).startswith("2026-06-01")
    assert int(wle["n"]) >= 1


def test_full_pull_linkedin_keyed_event_resolves_to_core_snapshot_lead(monkeypatch):
    """Regression: apply_agent_lead_core_payload() used to never register the
    snapshot's LinkedIn identity when replaying onto an ALREADY-EXISTING lead
    (find_lead_by_identifier hits, so the bootstrap-only resolve_lead_from_
    agent_sync path -- which does handle payload["linkedin"] -- is skipped).
    So a later entry in the same pull keyed by that LinkedIn URL (not email)
    couldn't find the lead -- it either bootstrapped a duplicate or attached
    to some unrelated lead that already owned that URL. Pre-seed the lead so
    the core snapshot replay exercises the existing-lead path, then prove
    the LinkedIn-keyed event resolves to it end-to-end via the real
    pull-phase ordering (core snapshots before events)."""
    om.init_db()
    om.set_workspace_routing("single")
    entity_key = "li-replay@example.com"
    linkedin_url = "https://www.linkedin.com/in/li-replay-test"
    existing_lead_id = om.resolve_lead(email=entity_key, name="LI Replay")["id"]
    core_event = {
        "platform": "agent",
        "relay_id": 51_001,
        "entity_key": entity_key,
        "event_type": "lead_core_update",
        "received_at": "2026-06-01T10:00:00Z",
        "payload": {
            "action": "lead_core_update",
            "client_id": "upstream-client",
            "timestamp": "2026-06-01T10:00:00Z",
            "data": {
                "email": entity_key,
                "name": "LI Replay",
                "linkedin": linkedin_url,
            },
        },
    }
    log_event = {
        "platform": "agent",
        "relay_id": 51_002,
        "entity_key": linkedin_url,
        "event_type": "event_log",
        "received_at": "2026-06-01T11:00:00Z",
        "workspace": "default",
        "payload": {
            "action": "event_log",
            "client_id": "upstream-client",
            "workspace": "default",
            "timestamp": "2026-06-01T11:00:00Z",
            "data": {
                "event_type": "linkedin_message",
                "direction": "outbound",
                "channel": "linkedin",
                "campaign": "acme | li outreach",
            },
        },
    }

    snapshot_pages = {
        "core": [
            {
                "events": [core_event],
                "max_snapshot_id": 1,
                "has_more_snapshots": False,
            }
        ],
        "workspace": [{"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}],
    }
    event_pages = [
        {"events": [log_event], "max_id": 51_002, "has_more_events": False},
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs["snapshot_kind"]
            pages = snapshot_pages.get(kind) or [{"events": []}]
            page = pages.pop(0) if pages else {"events": []}
            return {
                "events": page.get("events") or [],
                "max_snapshot_id": page.get("max_snapshot_id", 0),
                "has_more_snapshots": page.get("has_more_snapshots", False),
            }
        page = event_pages.pop(0) if event_pages else {"events": []}
        return {
            "events": page.get("events") or [],
            "max_id": page.get("max_id", 0),
            "has_more_events": page.get("has_more_events", False),
        }

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "_snapshot_pending_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)
    _patch_pull_prefetch(monkeypatch)

    imported, _skipped = om.sync_from_relay_org(
        "om_agent_test",
        full=True,
        quiet=True,
        skip_routing_sync=True,
    )
    assert imported >= 1

    conn = om.get_conn()
    lead_count = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    lead_row = conn.execute(
        "SELECT id, linkedin_url FROM leads WHERE id = ?", (existing_lead_id,),
    ).fetchone()
    li_event = conn.execute(
        "SELECT lead_id FROM events WHERE event_type = 'linkedin_message'"
    ).fetchone()
    conn.close()

    assert int(lead_count) == 1, "LinkedIn-keyed event must not bootstrap a duplicate lead"
    assert lead_row is not None
    assert lead_row["linkedin_url"], "core snapshot replay must register linkedin on the existing lead"
    assert li_event is not None
    assert li_event["lead_id"] == existing_lead_id


def test_event_log_bootstraps_even_when_events_run_before_snapshots(monkeypatch):
    """Email entity_keys can still replay when events precede snapshots (bootstrap path)."""
    om.init_db()
    om.set_workspace_routing("single")
    log_event = {
        "platform": "agent",
        "relay_id": 60_001,
        "entity_key": "orphan@example.com",
        "event_type": "event_log",
        "received_at": "2026-06-01T11:00:00Z",
        "payload": {
            "action": "event_log",
            "client_id": "upstream-client",
            "workspace": "default",
            "timestamp": "2026-06-01T11:00:00Z",
            "data": {
                "event_type": "email_sent",
                "direction": "outbound",
                "channel": "email",
                "campaign": "acme | career services",
            },
        },
    }

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}
        return {"events": [log_event], "max_id": 60_001, "has_more_events": False}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "_relay_pull_phases", lambda full, do_events, kinds: ("events", "snapshots"))
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)
    _patch_pull_prefetch(monkeypatch)

    imported, _skipped = om.sync_from_relay_org(
        "om_agent_test",
        full=True,
        quiet=True,
        skip_routing_sync=True,
    )
    conn = om.get_conn()
    count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    conn.close()
    assert int(count) == 1
    assert imported >= 1


def test_many_event_logs_after_snapshots(monkeypatch):
    """Stress-ish: 50 event_logs replay after one core snapshot."""
    om.init_db()
    om.set_workspace_routing("single")
    emails = [f"bulk{i}@example.com" for i in range(50)]
    core_events = [
        {
            "platform": "agent",
            "relay_id": 70_000 + i,
            "entity_key": email,
            "event_type": "lead_core_update",
            "received_at": f"2026-06-01T10:{i:02d}:00Z",
            "payload": {
                "action": "lead_core_update",
                "client_id": "bulk-client",
                "timestamp": f"2026-06-01T10:{i:02d}:00Z",
                "data": {"email": email, "name": f"Lead {i}"},
            },
        }
        for i, email in enumerate(emails)
    ]
    log_events = [
        {
            "platform": "agent",
            "relay_id": 80_000 + i,
            "entity_key": email,
            "event_type": "event_log",
            "received_at": f"2026-06-01T11:{i:02d}:00Z",
            "payload": {
                "action": "event_log",
                "client_id": "bulk-client",
                "workspace": "default",
                "timestamp": f"2026-06-01T11:{i:02d}:00Z",
                "data": {
                    "event_type": "email_sent",
                    "direction": "outbound",
                    "channel": "email",
                    "campaign": "acme | marketing",
                },
            },
        }
        for i, email in enumerate(emails)
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs["snapshot_kind"]
            if kind == "core":
                return {
                    "events": core_events,
                    "max_snapshot_id": len(core_events),
                    "has_more_snapshots": False,
                }
            return {"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}
        return {
            "events": log_events,
            "max_id": 80_000 + len(log_events),
            "has_more_events": False,
        }

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "_snapshot_pending_count", lambda *_a, **_k: len(emails))
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)
    _patch_pull_prefetch(monkeypatch)

    imported, _skipped = om.sync_from_relay_org(
        "om_agent_test",
        full=True,
        quiet=True,
        skip_routing_sync=True,
    )
    conn = om.get_conn()
    event_count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    lead_count = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    conn.close()
    assert int(lead_count) == len(emails)
    assert int(event_count) == len(emails)
    assert imported >= len(emails)


def test_self_pushed_snapshot_still_applies_on_fresh_pull(monkeypatch):
    """Regression: a hardcoded 'don't replay your own pushes back to
    yourself' check in ingest_agent_entry() fired unconditionally, including
    on a wiped DB where the relay is the only remaining source of
    crm_entity_map/tags/LinkedIn status. Snapshot actions must still apply
    even when client_id matches the local client."""
    om.init_db()
    om.set_workspace_routing("single")
    monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-client")

    lead_id = om.resolve_lead(email="self-push@example.com", name="Self Push")["id"]

    event = {
        "platform": "agent",
        "relay_id": 700_001,
        "entity_key": "self-push@example.com",
        "event_type": "lead_workspace_update",
        "received_at": "2026-06-01T10:00:00Z",
        "workspace": "default",
        "payload": {
            "action": "lead_workspace_update",
            "client_id": "local-client",
            "workspace": "default",
            "timestamp": "2026-06-01T10:00:00Z",
            "data": {
                "tags": ["nace"],
                "crm_entity_map": [
                    {"platform": "ghl", "crm_contact_id": "ghl-contact-1"},
                ],
            },
        },
    }
    result_lead_id = om.ingest_agent_entry(event, quiet=True)
    assert result_lead_id == lead_id

    conn = om.get_conn()
    entity_row = conn.execute(
        "SELECT crm_contact_id FROM crm_entity_map WHERE lead_id = ? AND platform = 'ghl'",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert entity_row is not None
    assert entity_row["crm_contact_id"] == "ghl-contact-1"


def test_self_pushed_stage_change_still_skipped(monkeypatch):
    """stage_change/event_log actions remain skipped for self-pushed entries
    -- they're redundant with the primary webhook ingest path; only
    snapshot actions need the exception."""
    om.init_db()
    om.set_workspace_routing("single")
    monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-client")

    lead_id = om.resolve_lead(email="self-stage@example.com", name="Self Stage")["id"]

    event = {
        "platform": "agent",
        "relay_id": 700_002,
        "entity_key": "self-stage@example.com",
        "event_type": "stage_change",
        "received_at": "2026-06-01T10:00:00Z",
        "payload": {
            "action": "stage_change",
            "client_id": "local-client",
            "timestamp": "2026-06-01T10:00:00Z",
            "data": {"stage": "interested"},
        },
    }
    result = om.ingest_agent_entry(event, quiet=True)
    assert result is None

    conn = om.get_conn()
    row = conn.execute("SELECT stage FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["stage"] != "interested"
