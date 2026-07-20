"""Company merge relay tombstones: build_company_merge_delete_sync_payload(),
_push_pending_company_merge_deletes(), and inspect_sync_company_merge_delete()
-- mirrors the existing lead merge-delete tombstone mechanism
(build_merge_delete_sync_payload/_push_pending_merge_deletes), extended to
companies so merge_companies() no longer leaves a stale company_core
snapshot on the relay after a merge. Requires wbhk-worker to recognize the
"company_core_delete" action (added alongside this)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_workspace  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def test_build_company_merge_delete_sync_payload_shape():
    payload = pipeline_workspace.build_company_merge_delete_sync_payload(
        "uid:abc123", timestamp="2026-07-19T00:00:00Z",
    )
    assert payload == {
        "action": "company_core_delete",
        "entity_key": "uid:abc123",
        "timestamp": "2026-07-19T00:00:00Z",
        "payload": {"reason": "merge"},
    }


def test_merge_companies_writes_a_pending_tombstone_row():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="University of Maryland", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp Mail", domain="terpmail.umd.edu")
    conn.commit()
    merged_uid = conn.execute("SELECT uid FROM companies WHERE id = ?", (merge_id,)).fetchone()["uid"]

    result = om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()
    assert result["status"] == "merged"

    row = conn.execute(
        "SELECT merge_entity_key, relay_delete_pushed FROM company_merges WHERE keep_id = ? AND merge_id = ?",
        (keep_id, merge_id),
    ).fetchone()
    assert row is not None
    assert row["merge_entity_key"] == f"uid:{merged_uid}"
    assert row["relay_delete_pushed"] == 0
    conn.close()


def test_push_pending_company_merge_deletes_marks_pushed_on_success(monkeypatch):
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="UMD", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp", domain="terpmail.umd.edu")
    conn.commit()
    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()
    conn.close()

    captured_entries = []

    def fake_push_batches(agent_key, entries, client_id, **kwargs):
        captured_entries.extend(entries)
        on_mark_cleared = kwargs.get("on_mark_cleared")
        mark_ids = kwargs.get("mark_ids") or []
        if on_mark_cleared:
            on_mark_cleared(mark_ids)
        return {"pushed": len(entries), "error": None, "throttled": False}

    monkeypatch.setattr(om, "_relay_push_batches", fake_push_batches)

    result = pipeline_workspace._push_pending_company_merge_deletes("fake-key")
    assert result["pushed"] == 1
    assert result["total_pending"] == 1
    assert captured_entries[0]["action"] == "company_core_delete"

    conn = om.get_conn()
    row = conn.execute("SELECT relay_delete_pushed FROM company_merges").fetchone()
    assert row["relay_delete_pushed"] == 1
    conn.close()


def test_push_pending_company_merge_deletes_skips_already_pushed():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="UMD", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp", domain="terpmail.umd.edu")
    conn.commit()
    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.execute("UPDATE company_merges SET relay_delete_pushed = 1")
    conn.commit()
    conn.close()

    result = pipeline_workspace._push_pending_company_merge_deletes("fake-key")
    assert result == {"pushed": 0, "error": None, "total_pending": 0, "sample_entries": []}


def test_push_pending_company_merge_deletes_dry_run_does_not_mark_pushed():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="UMD", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp", domain="terpmail.umd.edu")
    conn.commit()
    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()
    conn.close()

    result = pipeline_workspace._push_pending_company_merge_deletes("fake-key", dry_run=True)
    assert result["pushed"] == 0
    assert result["total_pending"] == 1
    assert len(result["sample_entries"]) == 1
    assert result["sample_entries"][0]["action"] == "company_core_delete"

    conn = om.get_conn()
    row = conn.execute("SELECT relay_delete_pushed FROM company_merges").fetchone()
    assert row["relay_delete_pushed"] == 0, "dry_run must never mark anything as pushed"
    conn.close()


def test_no_pending_tombstones_returns_empty_result():
    result = pipeline_workspace._push_pending_company_merge_deletes("fake-key")
    assert result == {"pushed": 0, "error": None, "total_pending": 0, "sample_entries": []}


def test_inspect_sync_company_merge_delete_by_id():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="UMD", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp", domain="terpmail.umd.edu")
    conn.commit()
    om.merge_companies(keep_id, merge_id, reason="test_reason", conn=conn)
    conn.commit()
    merge_row_id = conn.execute("SELECT id FROM company_merges").fetchone()["id"]

    result = om.inspect_sync_company_merge_delete(conn, str(merge_row_id))
    assert result["keep_company_id"] == keep_id
    assert result["merged_company_id"] == merge_id
    assert result["reason"] == "test_reason"
    assert result["relay_delete_pushed"] is False
    assert result["full_sync_payload"]["action"] == "company_core_delete"
    conn.close()


def test_inspect_sync_company_merge_delete_by_entity_key():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="UMD", domain="umd.edu")
    merge_id = om.ensure_company(conn, name="UMD Terp", domain="terpmail.umd.edu")
    conn.commit()
    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()
    entity_key = conn.execute("SELECT merge_entity_key FROM company_merges").fetchone()["merge_entity_key"]

    result = om.inspect_sync_company_merge_delete(conn, entity_key)
    assert result["keep_company_id"] == keep_id
    conn.close()


def test_inspect_sync_company_merge_delete_not_found_returns_empty():
    conn = om.get_conn()
    result = om.inspect_sync_company_merge_delete(conn, "nonexistent-key")
    assert result == {}
    conn.close()


def test_sync_all_calls_company_merge_delete_push(monkeypatch, capsys):
    """sync_all() must invoke the new push function, distinct from the lead
    version, not silently rely only on the lead tombstone path."""
    import routing_cloud

    monkeypatch.setattr(om, "get_agent_key", lambda: "fake-agent-key")
    monkeypatch.setattr(routing_cloud, "cloud_routing_enabled", lambda *a, **k: True)
    monkeypatch.setattr(om, "get_sync_status", lambda org_id=None: {
        "can_sync": True,
        "synced": False,
        "leads_pending": 0,
        "workspace_leads_pending": 0,
        "pending_workspaces": [],
        "pending_rules": [],
        "local_agent_events": 0,
        "recommended_mode": "push",
    })

    calls = []

    def _noop_push(*args, **kwargs):
        return {}

    def _tracking_company_merge_push(*args, **kwargs):
        calls.append((args, kwargs))
        return {"pushed": 0}

    # sync_all() does a call-time `from pipeline import (...)` re-import of
    # these push functions inside its own body, so patching the `om`
    # (pipeline) module's attribute -- not pipeline_workspace's -- is what
    # actually takes effect, matching test_sync_stdout_is_json_only.py's
    # convention for the sibling lead push functions.
    for fn_name in (
        "_push_agent_events_to_relay",
        "_push_pending_company_updates",
        "_push_pending_lead_snapshots",
        "_push_pending_merge_deletes",
        "_push_pending_quarantine_resolutions",
        "_push_pending_sender_account_updates",
        "_push_pending_sender_domain_updates",
    ):
        monkeypatch.setattr(om, fn_name, _noop_push)
    monkeypatch.setattr(om, "_push_pending_company_merge_deletes", _tracking_company_merge_push)

    result = pipeline_workspace.sync_all(no_health_report=True)
    assert result["status"] == "ok"
    assert len(calls) == 1, "sync_all() must call _push_pending_company_merge_deletes exactly once"
    assert "company_merge_deletes_pushed" in result
