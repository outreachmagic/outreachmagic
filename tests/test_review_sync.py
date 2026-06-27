"""Tests for lead-review sheet sync (personalization headers, LinkedIn noise)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_lead_review as review  # noqa: E402


def _reset_db() -> tuple[str, int]:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    ws = om.create_workspace("Review Sync Test", slug="review-sync-test")
    ws_id = f"ws_{ws['slug']}"
    r = om.resolve_lead(
        name="Alex Sync",
        company="Sync Co",
        email="alex.sync@test.com",
    )
    lead_id = int(r["id"])
    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    conn.execute(
        """INSERT INTO lead_personalization (lead_id, field_name, field_value)
           VALUES (?, 'first_name', 'Alexander')""",
        (lead_id,),
    )
    conn.execute(
        """INSERT INTO workspace_lead_linkedin_status
           (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending, updated_at)
           VALUES (?, ?, ?, ?, 0, 0, datetime('now'))""",
        (f"lis_{ws_id}_{lead_id}_sender", ws_id, lead_id, "sender one"),
    )
    conn.commit()
    conn.close()
    return ws_id, lead_id


def test_personalization_emoji_header_detected():
    ws_id, lead_id = _reset_db()
    conn = om.get_conn()
    summary = review.apply_lead_review_sync(
        conn,
        ws_id,
        [{"🔑 lead_id": str(lead_id), "✏️ Personalized First Name": "Alex"}],
        upsert_workspace_lead_fn=om.upsert_workspace_lead,
        org_id=om.DEFAULT_ORG_ID,
        dry_run=True,
    )
    conn.close()
    assert summary["updated"] == 1
    change = summary["changes"][0]
    assert change.get("personalized_first_name") == "Alex"


def test_linkedin_columns_unchanged_skipped():
    ws_id, lead_id = _reset_db()
    conn = om.get_conn()
    summary = review.apply_lead_review_sync(
        conn,
        ws_id,
        [{
            "lead_id": str(lead_id),
            "🔒 LinkedIn (sender one)": "not_requested",
        }],
        upsert_workspace_lead_fn=om.upsert_workspace_lead,
        org_id=om.DEFAULT_ORG_ID,
        dry_run=True,
    )
    conn.close()
    assert summary["updated"] == 0
    assert summary["skipped"] >= 1


def test_resolve_personalization_key():
    assert review._resolve_personalization_key("first_name") == "personalized_first_name"
    assert review._resolve_personalization_key("personalized_first_name") == "personalized_first_name"
    assert review._resolve_personalization_key("name") is None


if __name__ == "__main__":
    test_resolve_personalization_key()
    test_personalization_emoji_header_detected()
    test_linkedin_columns_unchanged_skipped()
    print("OK")
