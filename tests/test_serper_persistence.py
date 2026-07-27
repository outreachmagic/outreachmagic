#!/usr/bin/env python3
"""A Serper research run must leave a trace.

It used to leave none. The formatted research came back in the job summary and
nothing else: no provider attempt, no personalization row, no event. Three
consequences, all of which read as "I ran it and nothing happened":

  * the contact showed nothing,
  * the provider-runs panel showed nothing, and
  * has_attempted() was therefore never true, so the "skip already-run" guard
    never fired and every re-run bought the same Serper credits again.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import dashboard_actions  # noqa: E402
import pipeline as om  # noqa: E402
from pipeline_provider_attempts import (  # noqa: E402
    get_provider_attempts_for_lead, has_attempted,
)

FAKE_RESULT = {"organic": [{"title": "Jane Doe - GM at Dealer Co", "link": "https://x.test"}]}


class SerperPersistenceTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        self.lead_id = om.add_lead(
            name="Jane Doe", email="jane@dealer.com", company="Dealer Co")["id"]

    def _run(self, **kwargs):
        with mock.patch.object(dashboard_actions, "sync_manager", dashboard_actions.SyncManager()):
            pass
        mgr = dashboard_actions.SyncManager()
        with mock.patch("enrich.load_config", return_value={}), \
             mock.patch("enrich.serper_search", return_value=FAKE_RESULT) as search:
            summary = mgr._run_serper("", [self.lead_id], **kwargs)
        return summary, search

    def _personalization(self):
        conn = om.get_conn()
        try:
            return {
                r["field_name"]: r["field_value"]
                for r in conn.execute(
                    "SELECT field_name, field_value FROM lead_personalization WHERE lead_id = ?",
                    (self.lead_id,))
            }
        finally:
            conn.close()

    def _events(self):
        conn = om.get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT event_type, subject FROM events WHERE lead_id = ?", (self.lead_id,))]
        finally:
            conn.close()

    # -- the three missing traces -------------------------------------------

    def test_run_records_a_provider_attempt(self):
        summary, _ = self._run()
        self.assertEqual(summary["searched"], 1)
        conn = om.get_conn()
        try:
            attempts = get_provider_attempts_for_lead(conn, self.lead_id)
        finally:
            conn.close()
        serper = [a for a in attempts if a.get("provider") == "serper"]
        self.assertEqual(len(serper), 1, "the run must appear in the provider-run log")
        # Not a free-text status: ATTEMPT_STATUSES coerces anything outside its
        # vocabulary to "unknown", which is exactly how a run reads as "nothing
        # happened" even once it is being recorded.
        self.assertEqual(serper[0]["status"], "found")

    def test_run_persists_the_research_text(self):
        self._run()
        pers = self._personalization()
        self.assertIn("serper_research", pers)
        self.assertTrue(pers["serper_research"].strip(), "research must not be empty")

    def test_run_logs_a_history_event(self):
        self._run()
        types = [e["event_type"] for e in self._events()]
        self.assertIn("research_completed", types)

    def test_history_event_survives_a_caller_with_no_workspace_slug(self):
        """log_event demands an explicit workspace in multi-workspace mode. A
        lead in exactly one workspace already answers that, and dropping the
        event because the caller had no slug to hand is a silent loss."""
        conn = om.get_conn()
        try:
            org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
                ("ws-a", org_id, "wsa", "WS A"))
            conn.execute(
                "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id) "
                "VALUES (?,?,?,?)", (f"wl-{self.lead_id}", org_id, "ws-a", self.lead_id))
            conn.commit()
            self.assertEqual(
                dashboard_actions.SyncManager._sole_workspace_slug(conn, self.lead_id), "wsa")
        finally:
            conn.close()
        self._run(force=True)
        self.assertIn("research_completed", [e["event_type"] for e in self._events()])

    # -- the credit leak ----------------------------------------------------

    def test_second_run_is_skipped_without_force(self):
        _, first = self._run()
        self.assertGreater(first.call_count, 0)
        summary, second = self._run()
        self.assertEqual(summary["skipped_already_ran"], 1)
        self.assertEqual(
            second.call_count, 0,
            "a lead already researched must not buy Serper credits again",
        )

    def test_force_reruns(self):
        self._run()
        summary, search = self._run(force=True)
        self.assertEqual(summary["searched"], 1)
        self.assertGreater(search.call_count, 0)

    def test_has_attempted_becomes_true(self):
        conn = om.get_conn()
        try:
            self.assertFalse(has_attempted(conn, self.lead_id, "serper"))
        finally:
            conn.close()
        self._run()
        conn = om.get_conn()
        try:
            self.assertTrue(has_attempted(conn, self.lead_id, "serper"))
        finally:
            conn.close()

    # -- failures are visible too -------------------------------------------

    def test_a_failing_run_is_still_recorded(self):
        mgr = dashboard_actions.SyncManager()
        with mock.patch("enrich.load_config", return_value={}), \
             mock.patch("enrich.serper_search", side_effect=RuntimeError("api down")):
            summary = mgr._run_serper("", [self.lead_id])
        self.assertEqual(summary["errors"], 1)
        conn = om.get_conn()
        try:
            attempts = [a for a in get_provider_attempts_for_lead(conn, self.lead_id)
                        if a.get("provider") == "serper"]
        finally:
            conn.close()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
