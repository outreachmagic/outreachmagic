#!/usr/bin/env python3
"""Tests for lead_actions: the shared workspace-scoped write path.

Locks the behavior extracted from pipeline_cli's update-stage/log-event
blocks, including the CLI wrappers themselves (parity tests).
"""

import json
import sys
import tempfile
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
import pipeline_cli  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")


def _add_lead(name="Jane Tester", company="Acme Corp", email="jane@acme-example.com"):
    result = om.add_lead(name, company=company, email=email)
    assert result["id"]
    return result["id"]


def _query(sql, params=()):
    conn = om.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


class TestChangeStageScoped:
    def test_updates_lead_and_workspace_rows(self):
        lead_id = _add_lead()
        result = lead_actions.change_stage_scoped(
            lead_id, "interested", workspace_slug="alpha",
            label="meeting requested", sentiment="positive")

        assert result == {
            "status": "updated", "id": lead_id, "stage": "interested",
            "workspace": "alpha",
        }
        lead = _query("SELECT stage FROM leads WHERE id = ?", (lead_id,))[0]
        assert lead["stage"] == "interested"
        wl = _query(
            "SELECT status, stage_entered_at, current_status_label, current_status_sentiment"
            " FROM workspace_leads WHERE lead_id = ?", (lead_id,))[0]
        assert wl["status"] == "interested"
        assert wl["stage_entered_at"]
        assert wl["current_status_label"] == "meeting requested"
        assert wl["current_status_sentiment"] == "positive"

    def test_logs_status_event_with_metadata(self):
        lead_id = _add_lead()
        lead_actions.change_stage_scoped(
            lead_id, "not_interested", workspace_slug="alpha", sentiment="negative")

        events = _query(
            "SELECT event_type, direction, metadata_json FROM events"
            " WHERE lead_id = ? AND event_type = 'lead_status_updated'", (lead_id,))
        assert len(events) == 1
        assert events[0]["direction"] == "inbound"
        meta = json.loads(events[0]["metadata_json"])
        assert meta["lead_status_raw"] == "not_interested"
        assert meta["lead_status_display"] == "not interested"
        assert meta["lead_status_sentiment"] == "negative"

    def test_label_overrides_display_metadata(self):
        lead_id = _add_lead()
        lead_actions.change_stage_scoped(
            lead_id, "replied", workspace_slug="alpha", label="asked for pricing")
        events = _query(
            "SELECT metadata_json FROM events"
            " WHERE lead_id = ? AND event_type = 'lead_status_updated'", (lead_id,))
        meta = json.loads(events[0]["metadata_json"])
        assert meta["lead_status_display"] == "asked for pricing"
        assert "lead_status_sentiment" not in meta

    def test_queues_outbox_rows_for_relay_push(self):
        lead_id = _add_lead()
        before = {(r["entity_type"], r["entity_id"]) for r in _query(
            "SELECT entity_type, entity_id FROM outbox")}
        lead_actions.change_stage_scoped(lead_id, "contacted", workspace_slug="alpha")
        after = _query("SELECT entity_type, entity_id FROM outbox")
        entity_types = {r["entity_type"] for r in after}
        assert "lead_core" in entity_types
        assert "lead_workspace" in entity_types
        assert len(after) >= len(before)

    def test_multi_mode_requires_workspace(self):
        lead_id = _add_lead()
        with pytest.raises(lead_actions.WorkspaceResolutionError) as exc:
            lead_actions.change_stage_scoped(lead_id, "contacted")
        assert "--workspace is required for update-stage" in str(exc.value)

    def test_unknown_workspace_rejected(self):
        lead_id = _add_lead()
        with pytest.raises(lead_actions.WorkspaceResolutionError) as exc:
            lead_actions.change_stage_scoped(lead_id, "contacted", workspace_slug="nope")
        assert "workspace not found: nope" in str(exc.value)

    def test_invalid_stage_raises_plain_valueerror(self):
        lead_id = _add_lead()
        with pytest.raises(ValueError) as exc:
            lead_actions.change_stage_scoped(lead_id, "bogus", workspace_slug="alpha")
        assert not isinstance(exc.value, lead_actions.WorkspaceResolutionError)


class TestLogEventScoped:
    def test_writes_event_and_workspace_index(self):
        lead_id = _add_lead()
        result = lead_actions.log_event_scoped(
            lead_id, "email_sent", subject="Quick question",
            workspace_slug="alpha", idempotency_prefix="dashboard")

        assert result == {"status": "logged", "lead_id": lead_id, "workspace": "alpha"}
        events = _query(
            "SELECT id, subject, direction, channel FROM events"
            " WHERE lead_id = ? AND event_type = 'email_sent'", (lead_id,))
        assert len(events) == 1
        assert events[0]["subject"] == "Quick question"
        wse = _query(
            "SELECT event_id, event_type, idempotency_key FROM workspace_lead_events"
            " WHERE lead_id = ?", (lead_id,))
        assert len(wse) == 1
        assert wse[0]["event_id"] == events[0]["id"]
        assert wse[0]["idempotency_key"].startswith(f"dashboard_{lead_id}_email_sent_")

    def test_default_idempotency_prefix_matches_cli(self):
        lead_id = _add_lead()
        lead_actions.log_event_scoped(lead_id, "email_sent", workspace_slug="alpha")
        wse = _query(
            "SELECT idempotency_key FROM workspace_lead_events WHERE lead_id = ?",
            (lead_id,))
        assert wse[0]["idempotency_key"].startswith(f"agent_cli_{lead_id}_")

    def test_event_status_default_applied_to_new_workspace_lead(self):
        lead_id = _add_lead()
        conn = om.get_conn()
        conn.execute("DELETE FROM workspace_leads WHERE lead_id = ?", (lead_id,))
        conn.commit()
        conn.close()
        lead_actions.log_event_scoped(lead_id, "email_reply", direction="inbound",
                                      workspace_slug="alpha")
        wl = _query("SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,))[0]
        assert wl["status"] == "replied"

    def test_multi_mode_requires_workspace(self):
        lead_id = _add_lead()
        with pytest.raises(lead_actions.WorkspaceResolutionError) as exc:
            lead_actions.log_event_scoped(lead_id, "email_sent")
        assert "--workspace is required for log-event" in str(exc.value)


class TestCliParity:
    """The refactored CLI wrappers must behave exactly like the old inline code."""

    def _run_cli(self, argv, capsys):
        old_argv = sys.argv
        sys.argv = ["pipeline.py"] + argv
        try:
            pipeline_cli.main()
        finally:
            sys.argv = old_argv
        return capsys.readouterr().out.strip()

    def test_update_stage_output_and_rows(self, capsys):
        lead_id = _add_lead()
        capsys.readouterr()
        out = self._run_cli(
            ["update-stage", "--id", str(lead_id), "--stage", "interested",
             "--workspace", "alpha", "--sentiment", "positive"], capsys)
        assert json.loads(out.splitlines()[-1]) == {
            "status": "updated", "id": lead_id, "stage": "interested",
            "workspace": "alpha",
        }
        wl = _query("SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,))[0]
        assert wl["status"] == "interested"

    def test_update_stage_missing_workspace_errors(self, capsys):
        lead_id = _add_lead()
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            self._run_cli(["update-stage", "--id", str(lead_id), "--stage", "contacted"], capsys)
        assert exc.value.code == 1
        out = capsys.readouterr().out.strip()
        assert json.loads(out.splitlines()[-1]) == {
            "error": "Multi-workspace mode: --workspace is required for update-stage"
        }

    def test_log_event_output_and_rows(self, capsys):
        lead_id = _add_lead()
        capsys.readouterr()
        out = self._run_cli(
            ["log-event", "--lead-id", str(lead_id), "--type", "email_sent",
             "--subject", "Hello", "--workspace", "alpha"], capsys)
        assert json.loads(out.splitlines()[-1]) == {
            "status": "logged", "lead_id": lead_id, "workspace": "alpha",
        }
        wse = _query(
            "SELECT idempotency_key FROM workspace_lead_events WHERE lead_id = ?",
            (lead_id,))
        assert len(wse) == 1
        assert wse[0]["idempotency_key"].startswith("agent_cli_")

    def test_log_event_unknown_workspace_errors(self, capsys):
        lead_id = _add_lead()
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            self._run_cli(
                ["log-event", "--lead-id", str(lead_id), "--type", "email_sent",
                 "--workspace", "ghost"], capsys)
        assert exc.value.code == 1
        out = capsys.readouterr().out.strip()
        assert json.loads(out.splitlines()[-1]) == {"error": "workspace not found: ghost"}
