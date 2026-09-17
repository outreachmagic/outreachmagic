#!/usr/bin/env python3
"""A lead's current status is its LATEST status fact, by event time.

Regression: a pull applied the event stream (lead turned positive), then
replayed a workspace snapshot pushed the day before (still autoreply) -- and
the snapshot won, because nothing compared times. set_lead_status is now the
one writer and ignores anything older than current_status_at; untimed
(legacy) writes only fill blanks; repair-lead-status rebuilds the projection
from the event log.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import lead_actions  # noqa: E402
import pipeline as om  # noqa: E402
from lead_status_repair import event_label, repair_lead_status  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID, WORKSPACE_ROUTING_MULTI, set_lead_status,
)


def _ts(days_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")


@pytest.fixture
def lead():
    lead_id = om.add_lead("Pat Example", company="Example Co", email="pat@example.com")["id"]
    conn = om.get_conn()
    ws_id = om.resolve_workspace_identity(conn, "alpha")["id"]
    wl_id = om.upsert_workspace_lead(conn, DEFAULT_ORG_ID, ws_id, lead_id, status="contacted")
    conn.commit()
    yield {"lead_id": lead_id, "ws_id": ws_id, "wl_id": wl_id, "conn": conn}
    conn.close()


def _row(conn, wl_id):
    return dict(conn.execute("SELECT * FROM workspace_leads WHERE id = ?", (wl_id,)).fetchone())


def _log_status(conn, lead, at, meta):
    cur = conn.execute(
        "INSERT INTO events (lead_id, event_type, direction, channel, metadata_json, created_at) "
        "VALUES (?, 'lead_status_updated', 'inbound', 'email', ?, ?)",
        (lead["lead_id"], json.dumps(meta), at),
    )
    om.append_workspace_event(
        conn, DEFAULT_ORG_ID, lead["ws_id"], lead["lead_id"], event_id=cur.lastrowid,
        event_type="lead_status_updated", event_at=at,
        idempotency_key=f"t-{cur.lastrowid}",
    )


def _relay_meta(label, sentiment):
    return {"lead_status_raw": label, "lead_status_sentiment": sentiment,
            "platform": "plusvibe", "webhook_event": "LEAD_MARKED_AS_" + label.upper()}


def test_stale_snapshot_does_not_roll_back_newer_event(lead):
    conn = lead["conn"]
    set_lead_status(conn, lead["wl_id"], label="out_of_office", sentiment="autoreply",
                    at="2026-09-15 19:18:31")
    set_lead_status(conn, lead["wl_id"], label="qc_interested", sentiment="positive",
                    at="2026-09-17 01:36:31")
    om.apply_agent_lead_workspace_payload(
        lead["lead_id"],
        {"lead_status": "out_of_office", "lead_sentiment": "autoreply",
         "sentiment_since": "2026-09-15 19:18:31", "status_at": "2026-09-15 19:18:31",
         "stage": "prospecting"},
        workspace_id=lead["ws_id"], conn=conn,
    )
    row = _row(conn, lead["wl_id"])
    assert row["current_status_label"] == "qc_interested"
    assert row["current_status_sentiment"] == "positive"
    assert row["current_sentiment_since"] == "2026-09-17 01:36:31"
    assert row["current_status_at"] == "2026-09-17 01:36:31"


def test_legacy_snapshot_without_time_only_fills_blanks(lead):
    conn = lead["conn"]
    legacy = {"lead_status": "out of office", "lead_sentiment": "autoreply",
              "sentiment_since": "2026-09-15 19:18:31"}
    om.apply_agent_lead_workspace_payload(
        lead["lead_id"], legacy, workspace_id=lead["ws_id"], conn=conn)
    row = _row(conn, lead["wl_id"])
    assert (row["current_status_label"], row["current_status_sentiment"]) == (
        "out_of_office", "autoreply")
    assert row["current_sentiment_since"] == "2026-09-15 19:18:31"

    set_lead_status(conn, lead["wl_id"], label="interested", sentiment="positive",
                    at="2026-09-17 01:36:31")
    om.apply_agent_lead_workspace_payload(
        lead["lead_id"], legacy, workspace_id=lead["ws_id"], conn=conn)
    row = _row(conn, lead["wl_id"])
    assert (row["current_status_label"], row["current_status_sentiment"]) == (
        "interested", "positive")


def test_out_of_order_events_keep_the_newest(lead):
    conn = lead["conn"]
    assert set_lead_status(conn, lead["wl_id"], label="interested", sentiment="positive",
                           at="2026-09-17 01:36:31")
    assert not set_lead_status(conn, lead["wl_id"], label="out_of_office",
                               sentiment="autoreply", at="2026-09-16 14:14:47")
    row = _row(conn, lead["wl_id"])
    assert row["current_status_sentiment"] == "positive"
    assert row["first_positive_at"] == "2026-09-17 01:36:31"


def test_same_status_in_another_spelling_is_a_no_op(lead):
    conn = lead["conn"]
    at = "2026-09-17 01:36:31"
    assert set_lead_status(conn, lead["wl_id"], label="Out of office", sentiment="autoreply", at=at)
    conn.execute("UPDATE workspace_leads SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
                 (lead["wl_id"],))
    assert not set_lead_status(conn, lead["wl_id"], label="out_of_office",
                               sentiment="AutoReply", at=at)
    assert _row(conn, lead["wl_id"])["updated_at"] == "2000-01-01 00:00:00"


def test_manual_label_is_replayable(lead):
    lead["conn"].close()
    lead_actions.change_stage_scoped(
        lead["lead_id"], "interested", workspace_slug="alpha",
        label="qc interested", sentiment="positive")
    conn = lead["conn"] = om.get_conn()
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM events WHERE lead_id = ? AND event_type = 'lead_status_updated'",
        (lead["lead_id"],)).fetchone()[0])
    assert meta["status_label"] == "qc_interested"
    assert meta["stage"] == "interested"
    assert event_label(meta, "lead_status_updated") == "qc_interested"

    conn.execute("UPDATE workspace_leads SET current_status_label = 'wrong' WHERE id = ?",
                 (lead["wl_id"],))
    conn.commit()
    result = repair_lead_status(conn, dry_run=False)
    assert result["leads_changed"] == 1
    assert _row(conn, lead["wl_id"])["current_status_label"] == "qc_interested"


def test_event_label_rules():
    # Manual change that set only a stage: no label.
    assert event_label({"lead_status_raw": "contacted", "stage": "contacted"},
                       "lead_status_updated") is None
    # Legacy manual change (pre-status_label): label survived only in display.
    assert event_label({"lead_status_raw": "interested",
                        "lead_status_display": "qc_interested"},
                       "lead_status_updated") == "qc_interested"
    assert event_label({"lead_status_raw": "contacted",
                        "lead_status_display": "contacted"},
                       "lead_status_updated") is None
    # Webhook event: raw is the label.
    assert event_label(_relay_meta("out_of_office", "autoreply"),
                       "lead_status_updated") == "out_of_office"


def test_repair_rebuilds_from_latest_event_and_converges(lead):
    conn = lead["conn"]
    _log_status(conn, lead, _ts(3), _relay_meta("out_of_office", "autoreply"))
    _log_status(conn, lead, _ts(2), _relay_meta("out_of_office", "autoreply"))
    turned_positive = _ts(1)
    _log_status(conn, lead, turned_positive, _relay_meta("qc_interested", "positive"))
    # A manual re-confirmation later keeps the positive run's anchor.
    _log_status(conn, lead, _ts(0.5), {"lead_status_raw": "interested",
                                       "stage": "interested",
                                       "lead_status_sentiment": "positive"})
    # The corrupted projection a stale snapshot left behind.
    conn.execute(
        "UPDATE workspace_leads SET current_status_label = 'out of office', "
        "current_status_sentiment = 'autoreply', current_sentiment_since = ? WHERE id = ?",
        (_ts(3), lead["wl_id"]))
    conn.commit()

    dry = repair_lead_status(conn, since="90d", dry_run=True)
    assert dry["leads_changed"] == 1 and dry["sentiment_changed"] == 1
    assert _row(conn, lead["wl_id"])["current_status_sentiment"] == "autoreply"

    applied = repair_lead_status(conn, since="90d", dry_run=False)
    assert applied["status"] == "applied"
    row = _row(conn, lead["wl_id"])
    assert row["current_status_label"] == "qc_interested"
    assert row["current_status_sentiment"] == "positive"
    assert row["current_sentiment_since"] == turned_positive
    assert row["current_status_at"] > turned_positive
    assert row["first_positive_at"] == turned_positive

    assert repair_lead_status(conn, since="90d", dry_run=True)["leads_changed"] == 0


def test_push_payload_carries_status_at(lead):
    conn = lead["conn"]
    set_lead_status(conn, lead["wl_id"], label="interested", sentiment="positive",
                    at="2026-09-17 01:36:31")
    conn.commit()
    payload = om.build_lead_workspace_sync_payload(
        conn, DEFAULT_ORG_ID, lead["lead_id"], workspace_slug="alpha")
    assert payload["status_at"] == "2026-09-17 01:36:31"
