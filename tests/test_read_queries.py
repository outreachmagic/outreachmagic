"""Tests for read_queries analytics presets."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402
import read_queries  # noqa: E402


class ReadQueriesTests(unittest.TestCase):
    def setUp(self):
        self._prev_data = om_paths._DATA_ROOT_OVERRIDE
        self._prev_project = om_paths._PROJECT_ROOT_OVERRIDE
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        om_paths.set_data_root_override(root)
        om_paths.set_project_root_override(root / "project")
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        om.init_db()

    def tearDown(self):
        om_paths.set_data_root_override(self._prev_data)
        om_paths.set_project_root_override(self._prev_project)
        self._tmp.cleanup()

    def _seed_engagement_data(self):
        conn = om.get_conn()
        conn.execute(
            "INSERT INTO campaigns (name) VALUES ('acme | alpha'), ('acme | beta')"
        )
        c_alpha = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'acme | alpha'"
        ).fetchone()[0]
        c_beta = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'acme | beta'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO leads (name, email, channel, stage)
               VALUES ('Lead One', 'one@test.com', 'email', 'prospecting')"""
        )
        lead_id = conn.execute(
            "SELECT id FROM leads WHERE email = 'one@test.com'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, campaign_id, created_at)
               VALUES (?, 'email_reply', 'inbound', 'email', ?, datetime('now', '-1 hours')),
                      (?, 'email_reply', 'inbound', 'email', ?, datetime('now', '-2 hours')),
                      (?, 'email_sent', 'outbound', 'email', ?, datetime('now', '-1 hours'))""",
            (lead_id, c_alpha, lead_id, c_alpha, lead_id, c_beta),
        )
        conn.commit()
        conn.close()

    def test_normalize_since_hours(self):
        self.assertIn("48 hours", read_queries.normalize_since("48h"))

    def test_engagement_by_campaign(self):
        self._seed_engagement_data()
        result = read_queries.engagement_by_campaign(workspace="acme", since="48h")
        self.assertEqual(result["preset"], "engagement")
        rows = result["rows"]
        self.assertGreaterEqual(len(rows), 1)
        alpha_rows = [r for r in rows if "alpha" in (r.get("campaign") or "")]
        self.assertTrue(alpha_rows)
        self.assertEqual(alpha_rows[0]["event_type"], "email_reply")
        self.assertGreaterEqual(int(alpha_rows[0]["count"]), 2)

    def test_validate_readonly_sql_rejects_delete(self):
        with self.assertRaises(ValueError):
            read_queries.validate_readonly_sql("DELETE FROM events")

    def test_run_readonly_sql_limit(self):
        self._seed_engagement_data()
        result = read_queries.run_readonly_sql(
            "SELECT id FROM events ORDER BY id",
            limit=1,
        )
        self.assertEqual(result["row_count"], 1)
        self.assertTrue(result["truncated"])

    def test_view_exists_after_migrate(self):
        conn = om.get_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_inbound_events_by_campaign'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
