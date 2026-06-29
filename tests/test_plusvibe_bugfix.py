#!/usr/bin/env python3
"""Regression tests for PlusVibe dedup, mappings, and stage advancement."""

from __future__ import annotations

import json
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
import relay_ingest as ri  # noqa: E402
from platform_registry import resolve_event  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    ws = om.create_workspace("PlusVibe Test", slug="pv-test")
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO campaign_workspace_map
           (id, org_id, source_platform, campaign_id, campaign_name_normalized, workspace_id)
           VALUES ('map1', ?, 'plusvibe', 'camp-1', 'test campaign', ?)""",
        (om.DEFAULT_ORG_ID, f"ws_{ws['slug']}"),
    )
    conn.commit()
    conn.close()
    yield


def _reply_event(*, event_type: str, relay_id: int, body: str = "Yes, let's talk") -> dict:
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "lead": "lead@example.com",
        "received_at": "2026-06-10T12:00:01Z",
        "raw": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "text_body": body,
            "body": body,
        },
    }


def _status_event(event_type: str, relay_id: int) -> dict:
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "lead": "lead@example.com",
        "received_at": "2026-06-10T12:00:05Z",
        "raw": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
        },
    }


def test_all_positive_replies_skipped_after_all_email_replies():
    first = _reply_event(event_type="all_email_replies", relay_id=101)
    dup = _reply_event(event_type="all_positive_replies", relay_id=102)
    assert ri.ingest_relay_event(first, force_workspace_id="ws_pv-test") == 1
    assert ri.ingest_relay_event(dup, force_workspace_id="ws_pv-test") is None
    conn = om.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'email_reply'",
    ).fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.parametrize(
    "event_type,expected_type,expected_stage",
    [
        ("lead_marked_as_meeting_booked", "meeting_booked", "interested"),
        ("lead_marked_as_meeting_completed", "meeting_completed", "interested"),
        ("lead_marked_as_qc_interested", "lead_status_updated", "interested"),
        ("lead_marked_as_qc_crm_only", "lead_disposition", "interested"),
        ("lead_marked_as_wrong_person", "lead_status_updated", "not_interested"),
        ("lead_marked_as_closed", "lead_status_updated", "not_interested"),
    ],
)
def test_plusvibe_status_events_map_and_advance_stage(event_type, expected_type, expected_stage):
    resolved = resolve_event("plusvibe", event_type, {})
    assert resolved.local_type == expected_type
    assert resolved.target_stage == expected_stage

    event = _status_event(event_type, relay_id=200)
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    row = conn.execute(
        """SELECT wl.status FROM workspace_leads wl
           JOIN leads l ON l.id = wl.lead_id
           WHERE l.email = ?""",
        ("lead@example.com",),
    ).fetchone()
    evt = conn.execute(
        "SELECT event_type FROM events WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == expected_stage
    assert evt["event_type"] == expected_type


def test_migrate_db_backfill_uses_shared_connection():
    conn = om.get_conn()
    conn.execute("INSERT INTO leads (name, email) VALUES ('No Camp', 'nocamp@example.com')")
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_sent', 'outbound', ?, NULL)""",
        (lead_id, json.dumps({"platform": "smartlead", "relay_id": 88001})),
    )
    conn.commit()
    cfg = om.load_config()
    cfg.pop("null_campaign_backfill_at", None)
    om.save_config(cfg)
    om.migrate_db(conn)
    conn.close()
