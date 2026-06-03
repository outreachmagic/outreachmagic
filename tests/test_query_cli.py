"""Tests for pipeline.py query subcommand (in-process)."""

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402
import query_cli  # noqa: E402


class QueryCliInProcessTests(unittest.TestCase):
    def setUp(self):
        self._prev_data = om_paths._DATA_ROOT_OVERRIDE
        self._prev_project = om_paths._PROJECT_ROOT_OVERRIDE
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        om_paths.set_data_root_override(root)
        om_paths.set_project_root_override(root / "project")
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        om.init_db()
        conn = om.get_conn()
        conn.execute("INSERT INTO campaigns (name) VALUES ('pop | camp')")
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'pop | camp'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO leads (name, email, channel, stage)
               VALUES ('P', 'p@test.com', 'email', 'prospecting')"""
        )
        lid = conn.execute("SELECT id FROM leads WHERE email = 'p@test.com'").fetchone()[0]
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, campaign_id, created_at)
               VALUES (?, 'email_reply', 'inbound', 'email', ?, datetime('now', '-1 hours'))""",
            (lid, cid),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        om_paths.set_data_root_override(self._prev_data)
        om_paths.set_project_root_override(self._prev_project)
        self._tmp.cleanup()

    def _args(self, **kwargs):
        defaults = {
            "preset": None,
            "workspace": None,
            "campaign_prefix": None,
            "since": None,
            "direction": "inbound",
            "event_types": None,
            "sql": None,
            "params": None,
            "file": None,
            "limit": 500,
            "json": True,
            "command": "query",
        }
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_engagement_preset_json(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_query(self._args(preset="engagement", workspace="pop", since="48h"))
        data = json.loads(buf.getvalue())
        self.assertEqual(data["preset"], "engagement")
        self.assertGreaterEqual(data["row_count"], 1)

    def test_sql_rejects_mutation(self):
        with self.assertRaises(SystemExit):
            query_cli.cmd_query(self._args(sql="DELETE FROM events"))

    def test_sql_select_ok(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_query(self._args(sql="SELECT COUNT(*) AS n FROM events"))
        data = json.loads(buf.getvalue())
        self.assertEqual(data["rows"][0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
