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
    all_kinds = frozenset({"events", "core", "workspace", "company"})
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
    om.create_workspace("PopCam", "popcam", sync=False)
    conn = om.get_conn()
    config = om.get_org_routing_config(conn, DEFAULT_ORG_ID)
    ws_map = om._pull_workspace_slug_map(conn, DEFAULT_ORG_ID)
    conn.close()

    event = {
        "platform": "agent",
        "client_id": "remote-replay-client",
        "action": "event_log",
        "entity_key": "replay-bootstrap@example.com",
        "timestamp": "2026-06-01T12:00:00Z",
        "workspace": "popcam",
        "payload": {
            "event_type": "email_sent",
            "direction": "outbound",
            "channel": "email",
            "campaign": "popcam | headshot lounge",
            "body_preview": "Hello",
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
        "action": "lead_core_update",
        "client_id": "upstream-client",
        "entity_key": entity_key,
        "timestamp": "2026-06-01T10:00:00Z",
        "payload": {
            "email": entity_key,
            "name": "Full Replay",
            "company": "Acme",
        },
    }
    log_event = {
        "platform": "agent",
        "relay_id": 50_002,
        "action": "event_log",
        "client_id": "upstream-client",
        "entity_key": entity_key,
        "timestamp": "2026-06-01T11:00:00Z",
        "workspace": "default",
        "payload": {
            "event_type": "email_sent",
            "direction": "outbound",
            "channel": "email",
            "campaign": "popcam | headshot lounge",
            "body_preview": "Hi",
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
        "company": [{"events": [], "max_snapshot_id": 0, "has_more_snapshots": False}],
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
    assert json.loads(meta["metadata_json"]).get("campaign") == "popcam | headshot lounge"
    assert ts_row is not None
    assert str(ts_row["created_at"]).startswith("2026-06-01")
    assert int(wle["n"]) >= 1


def test_event_log_bootstraps_even_when_events_run_before_snapshots(monkeypatch):
    """Email entity_keys can still replay when events precede snapshots (bootstrap path)."""
    om.init_db()
    om.set_workspace_routing("single")
    log_event = {
        "platform": "agent",
        "relay_id": 60_001,
        "action": "event_log",
        "client_id": "upstream-client",
        "entity_key": "orphan@example.com",
        "timestamp": "2026-06-01T11:00:00Z",
        "workspace": "default",
        "payload": {
            "event_type": "email_sent",
            "direction": "outbound",
            "channel": "email",
            "campaign": "popcam | career services",
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
            "action": "lead_core_update",
            "client_id": "bulk-client",
            "entity_key": email,
            "timestamp": f"2026-06-01T10:{i:02d}:00Z",
            "payload": {"email": email, "name": f"Lead {i}"},
        }
        for i, email in enumerate(emails)
    ]
    log_events = [
        {
            "platform": "agent",
            "relay_id": 80_000 + i,
            "action": "event_log",
            "client_id": "bulk-client",
            "entity_key": email,
            "timestamp": f"2026-06-01T11:{i:02d}:00Z",
            "workspace": "default",
            "payload": {
                "event_type": "email_sent",
                "direction": "outbound",
                "channel": "email",
                "campaign": "popcam | marketing",
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
