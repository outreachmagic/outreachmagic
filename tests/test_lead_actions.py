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


# ── company-pane record edits ────────────────────────────────────────────────
#
# Record type and contact order are decisions you make while looking at the
# roster of an account. Until now the only way to set either was to round-trip a
# review sheet, which is a long way to go to say "call this one first".


class TestRecordTypeAndContactOrder:
    def _lead_in_workspace(self):
        import dashboard_actions

        lead_id = _add_lead()
        lead_actions.log_event_scoped(lead_id, "email_sent", workspace_slug="alpha")
        return dashboard_actions, lead_id

    def test_record_type_round_trips(self):
        da, lead_id = self._lead_in_workspace()
        assert da.set_record_type(lead_id, "company_placeholder")["record_type"] == "company_placeholder"
        conn = om.get_conn()
        try:
            assert conn.execute(
                "SELECT record_type FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()["record_type"] == "company_placeholder"
        finally:
            conn.close()
        da.set_record_type(lead_id, "contact")

    def test_an_unknown_record_type_is_rejected(self):
        da, lead_id = self._lead_in_workspace()
        with pytest.raises(ValueError, match="record_type must be"):
            da.set_record_type(lead_id, "prospect")

    def test_contact_order_round_trips_and_clears(self):
        da, lead_id = self._lead_in_workspace()
        assert da.set_contact_order(lead_id, 3, workspace_slug="alpha")["contact_order"] == 3
        conn = om.get_conn()
        try:
            read = lambda: conn.execute(  # noqa: E731
                "SELECT contact_priority FROM workspace_leads WHERE lead_id = ?",
                (lead_id,)).fetchone()["contact_priority"]
            assert read() == 3
            # "" is how a <select> spells "no order", and must clear rather than
            # raise or write a zero.
            da.set_contact_order(lead_id, "", workspace_slug="alpha")
            assert read() is None
        finally:
            conn.close()

    def test_contact_order_must_be_a_number_from_one(self):
        da, lead_id = self._lead_in_workspace()
        for bad in ("first", 0, -2):
            with pytest.raises(ValueError):
                da.set_contact_order(lead_id, bad, workspace_slug="alpha")

    def test_contact_order_needs_the_lead_to_be_in_that_workspace(self):
        import dashboard_actions

        lead_id = _add_lead()   # never associated with a workspace
        with pytest.raises(ValueError, match="not in this workspace"):
            dashboard_actions.set_contact_order(lead_id, 1, workspace_slug="alpha")


class TestDeleteLeads:
    def test_delete_keeps_the_relay_tombstone(self):
        """Deleting has to remove the contact from the relay too, or the next
        pull regrows it. The BEFORE DELETE trigger files the tombstone; keeping
        it is what makes this a deletion rather than a local hide."""
        import dashboard_actions

        lead_id = _add_lead()
        lead_actions.log_event_scoped(lead_id, "email_sent", workspace_slug="alpha")
        out = dashboard_actions.delete_leads([lead_id], confirm=True)
        assert out["deleted"] == 1
        conn = om.get_conn()
        try:
            assert conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone() is None
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
            ).fetchone()["n"] >= 1
        finally:
            conn.close()

    def test_delete_refuses_without_confirmation(self):
        import dashboard_actions

        lead_id = _add_lead()
        with pytest.raises(ValueError, match="confirm"):
            dashboard_actions.delete_leads([lead_id])
        conn = om.get_conn()
        try:
            assert conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone() is not None
        finally:
            conn.close()

    def test_delete_refuses_an_empty_list(self):
        import dashboard_actions

        with pytest.raises(ValueError, match="no lead_ids"):
            dashboard_actions.delete_leads([], confirm=True)
