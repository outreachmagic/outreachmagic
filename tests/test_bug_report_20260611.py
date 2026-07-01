#!/usr/bin/env python3
"""Regression tests for agency bug report 2026-06-11."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
EF_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(EF_SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import pipeline_lead_review as plr  # noqa: E402
from batch_runner import build_import_profile, should_tag_provider_attempt  # noqa: E402
from lead_sync import (  # noqa: E402
    apply_agent_lead_core_payload,
    build_lead_core_sync_payload,
    resolve_lead_from_agent_sync,
)
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_icypeas_attempted_not_stamped_when_skipped():
    profile = build_import_profile(
        full_name="Jane Doe",
        company="Acme",
        domain="acme.com",
        linkedin="",
        find_result={
            "status": "not_found",
            "provider": "trykitt",
            "provider_attempts": [
                {"provider": "trykitt", "status": "not_found", "attempted": True},
                {"provider": "icypeas", "status": "no_key", "attempted": False},
            ],
        },
        normalize_linkedin_fn=lambda x: x,
    )
    assert profile["tags"] == ["trykitt_attempted"]


def test_should_tag_provider_attempt_respects_attempted_flag():
    assert should_tag_provider_attempt({"provider": "icypeas", "status": "no_key", "attempted": False}) is False
    assert should_tag_provider_attempt({"provider": "trykitt", "status": "not_found", "attempted": True}) is True


def test_email_verification_source_survives_sync_roundtrip():
    result = om.resolve_lead(
        email="lev@example.com",
        name="Lev Lead",
        company="Acme",
        source="trykitt",
        source_platform="agent",
    )
    lead_id = result["id"]
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, source, verified_at)
           VALUES ('lev1', ?, ?, 'lev@example.com', 'valid', 'trykitt', '2026-06-10T12:00:00Z')""",
        (DEFAULT_ORG_ID, lead_id),
    )
    conn.execute(
        "UPDATE leads SET email_verification_status = 'valid' WHERE id = ?",
        (lead_id,),
    )
    conn.commit()
    payload = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert payload.get("email_verification_source") == "trykitt"
    assert payload.get("latest_email_verification_source") == "trykitt"

    # Simulate pull on another machine
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    pulled = resolve_lead_from_agent_sync("lev@example.com", payload)
    apply_agent_lead_core_payload(pulled["id"], payload, org_id=DEFAULT_ORG_ID, entity_key="lev@example.com")

    conn = om.get_conn()
    lev_rows = conn.execute(
        "SELECT source FROM lead_email_verification WHERE lead_id = ?",
        (pulled["id"],),
    ).fetchall()
    supplements = plr._load_lead_supplements(conn, [pulled["id"]])
    conn.close()
    sources = {r["source"] for r in lev_rows}
    assert "agent_sync" not in sources
    assert supplements[pulled["id"]]["email_verification_source"] == "trykitt"
    assert supplements[pulled["id"]]["latest_email_verification_source"] == "trykitt"


def test_export_includes_original_and_latest_email_verification_source():
    ws = om.create_workspace("Lev Export", slug="lev-export")
    ws_id = f"ws_{ws['slug']}"
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO leads (name, email, company, email_verification_status)
           VALUES ('Pat', 'pat@test.com', 'Acme', 'valid')"""
    )
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    om.upsert_workspace_lead(conn, DEFAULT_ORG_ID, ws_id, lead_id)
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, source, verified_at)
           VALUES ('lev_old', ?, ?, 'pat@test.com', 'valid', 'trykitt', '2026-06-01T10:00:00Z')""",
        (DEFAULT_ORG_ID, lead_id),
    )
    conn.execute(
        """INSERT INTO lead_email_verification
           (id, org_id, lead_id, email, status, source, verified_at)
           VALUES ('lev_new', ?, ?, 'pat@test.com', 'valid', 'millionverifier', '2026-06-10T12:00:00Z')""",
        (DEFAULT_ORG_ID, lead_id),
    )
    conn.commit()

    keys = plr.resolve_field_keys("full", sender_profiles=[])
    assert "original_email_verification_source" in keys
    assert "latest_email_verification_source" in keys

    payload = plr.build_export_payload(
        conn,
        workspace="lev-export",
        detail="full",
        title="Test",
        enrich_fn=om.enrich_lead_rows,
        limit=5,
    )
    conn.close()
    field_keys = [c["key"] for c in payload["columns"]]
    row = dict(zip(field_keys, payload["rows"][0]))
    assert row.get("original_email_verification_source") == "trykitt"
    assert row.get("latest_email_verification_source") == "millionverifier"
    assert row.get("email_verification_source") == "millionverifier"
