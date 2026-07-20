"""Regression test for the real production bug in the trykitt debug report:
`pipeline.py sync`'s "Syncing to relay..." announcement used to print() to
stdout with no `file=`, landing ahead of sync_all()'s final JSON in the CLI
handler. Any caller doing `json.loads(subprocess_output)` on the whole
invocation (shared.py's `_run_subprocess_json`, used by `run_sync()`) would
get a JSONDecodeError even though the sync itself succeeded. Fixed by routing
those announcements through the existing `_relay_log()` helper (stderr,
timestamped, flushed) instead of a bare print(). This test drives sync_all()
itself (not the CLI wrapper) with the network push functions stubbed out, and
asserts stdout captured no "Syncing to relay" text."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_workspace  # noqa: E402
import routing_cloud  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _noop_push(*args, **kwargs):
    return {}


def test_sync_all_syncing_announcement_never_lands_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(om, "get_agent_key", lambda: "fake-agent-key")
    monkeypatch.setattr(routing_cloud, "cloud_routing_enabled", lambda *a, **k: True)
    monkeypatch.setattr(om, "get_sync_status", lambda org_id=None: {
        "can_sync": True,
        "synced": False,
        "leads_pending": 3,
        "workspace_leads_pending": 0,
        "pending_workspaces": [],
        "pending_rules": [],
        "local_agent_events": 0,
        "recommended_mode": "push",
    })
    for fn_name in (
        "_push_agent_events_to_relay",
        "_push_pending_company_updates",
        "_push_pending_lead_snapshots",
        "_push_pending_merge_deletes",
        "_push_pending_company_merge_deletes",
        "_push_pending_quarantine_resolutions",
        "_push_pending_sender_account_updates",
        "_push_pending_sender_domain_updates",
    ):
        monkeypatch.setattr(om, fn_name, _noop_push)

    result = pipeline_workspace.sync_all(no_health_report=True)
    assert result["status"] == "ok"

    captured = capsys.readouterr()
    assert "Syncing to relay" not in captured.out, (
        "sync progress text leaked onto stdout -- this is exactly what broke "
        "shared.py's _run_subprocess_json() parsing pipeline.py sync's output "
        "as JSON in production (trykitt debug report, section 3)"
    )
    assert "Syncing to relay" in captured.err
