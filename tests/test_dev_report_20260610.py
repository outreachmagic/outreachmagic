#!/usr/bin/env python3
"""Regression tests for dev report 2026-06-10."""

from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import pipeline_lead_review as plr  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")


def test_backfill_null_campaign_events_quarantine_and_skip():
    conn = om.get_conn()
    conn.execute("INSERT INTO leads (name, email) VALUES ('No Camp', 'nocamp@example.com')")
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_sent', 'outbound', ?, NULL)""",
        (lead_id, '{"platform": "smartlead", "relay_id": 88001}'),
    )
    conn.commit()
    conn.close()

    result = om.backfill_null_campaign_quarantine(quiet=True)
    assert result["found"] == 1
    assert result["quarantined"] == 1
    assert result["skipped"] == 1

    rows = om.list_quarantine(status="skipped", limit=10)
    assert len(rows) == 1
    assert rows[0]["reason"] == "no_campaign_id"
    assert rows[0]["external_event_id"] == "88001"


def test_no_campaign_message_is_natural_language():
    from user_messages import no_campaign_event_message

    msg = no_campaign_event_message(platform="smartlead")
    assert "pipeline.py" not in msg
    assert "event history" in msg.lower()


def test_full_export_columns_include_new_fields():
    ws = om.create_workspace("Full Export", slug="full-export")
    ws_id = f"ws_{ws['slug']}"
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO leads (name, email, company, latest_source, latest_source_detail)
           VALUES ('Pat', 'pat@test.com', 'Acme', 'csv_import', 'june-list')"""
    )
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, source, verified_at)
           VALUES ('lev1', ?, ?, 'pat@test.com', 'valid', 'trykitt', '2026-06-10T12:00:00Z')""",
        (om.DEFAULT_ORG_ID, lead_id),
    )
    conn.commit()

    keys = plr.resolve_field_keys("full", sender_profiles=[])
    assert "lev_source" in keys
    assert "lev_verified_at" in keys
    assert "latest_source" in keys
    assert "be_platform" in keys

    payload = plr.build_export_payload(
        conn,
        workspace="full-export",
        detail="full",
        title="Test",
        enrich_fn=om.enrich_lead_rows,
        limit=5,
    )
    conn.close()
    field_keys = [c["key"] for c in payload["columns"]]
    row = dict(zip(field_keys, payload["rows"][0]))
    assert row.get("latest_source") == "csv_import"
    assert row.get("lev_source") == "trykitt"
    labels = [c["label"] for c in payload["columns"]]
    assert "✏️ Personalized First Name" in labels or any(
        "Personalized First Name" in label for label in labels
    )


def test_linkedin_sender_column_naming():
    meta = plr.build_sender_column_metadata("https://www.linkedin.com/in/dremmanuela")
    assert meta["key"] == "linkedin_sender_dremmanuela"
    assert meta["label"] == "🔒 Dremmanuela LI 1st Degree"
    assert meta["type"] == "string"


def test_dropdown_metadata_on_workspace_fields():
    cols = plr.build_column_metadata(["workspace_stage", "lead_sentiment", "lead_status"])
    by_key = {c["key"]: c for c in cols}
    from constants import PIPELINE_STAGES

    assert by_key["workspace_stage"]["validation"]["values"] == list(PIPELINE_STAGES)
    assert by_key["lead_sentiment"]["validation"]["values"] == list(plr.LEAD_SENTIMENT_VALUES)
    assert "validation" not in by_key["lead_status"]


def test_account_revoked_status_output():
    om.save_config({"account_access_revoked": True})
    buf = io.StringIO()
    with redirect_stdout(buf):
        om.cmd_status()
    out = buf.getvalue()
    assert "Access error" in out
    assert "pipeline.py" not in out


def test_require_agent_key_revoked_blocks():
    om.save_config({"account_access_revoked": True})
    buf = io.StringIO()
    with pytest.raises(SystemExit):
        with redirect_stdout(buf):
            om._require_agent_key()
    assert "account access error" in buf.getvalue().lower()
