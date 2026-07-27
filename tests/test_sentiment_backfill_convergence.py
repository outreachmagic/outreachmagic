#!/usr/bin/env python3
"""_backfill_current_sentiment_since must converge in one pass.

The guard ("any row still missing an anchor?") and the UPDATE (which can only
supply an anchor from a matching sentiment *event*) used to disagree. A lead
whose sentiment was set directly -- a manual stage change rather than a webhook
-- has no such event, so the UPDATE wrote NULL over NULL and the row still
qualified next time. Forever.

SQLite fires AFTER UPDATE even when nothing changes, so each pass re-stamped
those rows' outbox entries (dirty_at = now, attempts = 0). On the live database
276 rows were stuck like this: the outbox never reached zero, and every sync
rebuilt ~276 workspace + ~241 core payloads only to discard them as echoes.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import pipeline_migration as pm  # noqa: E402


class SentimentBackfillConvergenceTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _seed(self, *, with_event: bool):
        """One workspace lead carrying a sentiment and no anchor.

        with_event=True gives it a sentiment event the backfill can anchor on;
        False is the stuck case (sentiment set by hand, no event).
        """
        # add_lead opens its own connection; finish with it before taking ours.
        lead_id = om.add_lead(name="Ann", email="ann@acme.com", company="Acme")["id"]
        conn = om.get_conn()
        org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        ws_id = "ws-test"
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?, ?, ?, ?)",
            (ws_id, org_id, "testws", "Test WS"),
        )
        conn.execute(
            "INSERT INTO workspace_leads "
            "  (id, org_id, workspace_id, lead_id, status, "
            "   current_status_sentiment, current_sentiment_since) "
            "VALUES (?, ?, ?, ?, 'replied', 'positive', NULL)",
            (f"wl-{lead_id}", org_id, ws_id, lead_id),
        )
        if with_event:
            cur = conn.execute(
                "INSERT INTO events (lead_id, event_type, direction, channel, metadata_json) "
                "VALUES (?, 'reply', 'inbound', 'email', "
                "  json_object('lead_status_sentiment', 'positive'))",
                (lead_id,),
            )
            conn.execute(
                "INSERT INTO workspace_lead_events "
                "  (org_id, workspace_id, lead_id, event_id, event_type, event_at, idempotency_key) "
                "VALUES (?, ?, ?, ?, 'reply', '2026-05-01T10:00:00Z', ?)",
                (org_id, ws_id, lead_id, cur.lastrowid, f"test-{lead_id}"),
            )
        conn.commit()
        conn.close()
        return lead_id, ws_id

    def _since(self, lead_id, ws_id):
        conn = om.get_conn()
        try:
            row = conn.execute(
                "SELECT current_sentiment_since FROM workspace_leads "
                "WHERE lead_id = ? AND workspace_id = ?",
                (lead_id, ws_id),
            ).fetchone()
            return row["current_sentiment_since"] if row else None
        finally:
            conn.close()

    def _changes_from_backfill(self):
        conn = om.get_conn()
        try:
            before = conn.total_changes
            pm._backfill_current_sentiment_since(conn)
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()

    def test_anchorable_row_is_still_backfilled(self):
        """The fix must not break what the migration is for."""
        lead_id, ws_id = self._seed(with_event=True)
        self.assertIsNone(self._since(lead_id, ws_id))
        self._changes_from_backfill()
        self.assertEqual(self._since(lead_id, ws_id), "2026-05-01T10:00:00Z")

    def test_second_pass_over_anchorable_row_changes_nothing(self):
        lead_id, ws_id = self._seed(with_event=True)
        self._changes_from_backfill()
        self.assertEqual(self._changes_from_backfill(), 0)

    def test_unanchorable_row_is_not_rewritten_every_pass(self):
        """The actual defect: no event to anchor on, so no write should happen
        at all -- writing NULL over NULL still fires the outbox trigger."""
        lead_id, ws_id = self._seed(with_event=False)
        self.assertEqual(
            self._changes_from_backfill(), 0,
            "an unanchorable row must not be updated -- NULL over NULL still "
            "fires AFTER UPDATE and re-dirties the outbox",
        )
        self.assertIsNone(self._since(lead_id, ws_id))
        self.assertEqual(self._changes_from_backfill(), 0)

    def test_unanchorable_row_does_not_redirty_the_outbox(self):
        """End-to-end statement of the symptom: repeated migrations must leave
        the outbox timestamp alone."""
        lead_id, ws_id = self._seed(with_event=False)
        conn = om.get_conn()
        conn.execute("DELETE FROM outbox")
        conn.commit()
        conn.close()

        self._changes_from_backfill()

        conn = om.get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) n FROM outbox WHERE entity_type = 'lead_workspace'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0, "migration must not enqueue an unchanged row for sync")


if __name__ == "__main__":
    unittest.main()
