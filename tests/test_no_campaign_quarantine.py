#!/usr/bin/env python3
"""Regression tests for no-campaign event quarantine (bug 9)."""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.add_campaign_map_cli("*", "alpha", campaign_name="alpha", match_strategy="rule_contains")


def test_no_campaign_event_quarantined():
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "nocamp@example.com",
        "received_at": "2026-06-09T12:00:00Z",
        "relay_id": 99001,
        "payload": {"to_email": "nocamp@example.com"},
    }
    assert om.ingest_relay_event(event, quiet=True) is None
    pending = om.list_quarantine(status="pending", limit=10)
    assert len(pending) == 1
    assert pending[0]["reason"] == "no_campaign_id"
    assert pending[0]["external_event_id"] == "99001"


def test_quarantine_dedup_on_repull():
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "dup@example.com",
        "received_at": "2026-06-09T12:00:00Z",
        "relay_id": 99002,
        "payload": {"to_email": "dup@example.com"},
    }
    om.ingest_relay_event(event, quiet=True)
    om.ingest_relay_event(event, quiet=True)
    pending = om.list_quarantine(status="pending", limit=10)
    relay_ids = [p["external_event_id"] for p in pending if p["external_event_id"] == "99002"]
    assert len(relay_ids) == 1


def test_skip_by_reason_no_campaign_id():
    event = {
        "platform": "smartlead",
        "event_type": "email_reply",
        "lead": "skip@example.com",
        "received_at": "2026-06-09T12:00:00Z",
        "relay_id": 99003,
        "payload": {"to_email": "skip@example.com"},
    }
    om.ingest_relay_event(event, quiet=True)
    result = om.skip_quarantine_bulk(reason="no_campaign_id")
    assert result["status"] == "ok"
    assert result["skipped"] == 1
    assert result["reason"] == "no_campaign_id"


def test_cloud_skip_prevents_requarantine():
    event = {
        "platform": "smartlead",
        "event_type": "email_sent",
        "lead": "cloud@example.com",
        "received_at": "2026-06-09T12:00:00Z",
        "relay_id": 99004,
        "payload": {"to_email": "cloud@example.com"},
    }
    om.ingest_relay_event(event, quiet=True)
    resolution_map = {99004: {"status": "skipped"}}
    batch = om._ingest_relay_page([event], resolution_map=resolution_map, quiet=True)
    assert batch["skipped_resolved"] == 1
    assert batch["imported"] == 0
