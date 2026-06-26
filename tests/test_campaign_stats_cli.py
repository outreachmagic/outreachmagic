"""Tests for pipeline.py `sheets campaign-stats` CLI wiring."""

from __future__ import annotations

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


class CampaignStatsCliTests(unittest.TestCase):
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
        conn.execute("INSERT INTO campaigns (name) VALUES ('acme | alpha')")
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'acme | alpha'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO leads (name, email, channel, stage)
               VALUES ('Alice', 'a@test.com', 'email', 'prospecting')"""
        )
        lid = conn.execute("SELECT id FROM leads WHERE email = 'a@test.com'").fetchone()[0]
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, campaign_id, created_at)
               VALUES (?, 'email_sent', 'outbound', 'email', ?, datetime('now', '-1 hours'))""",
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
            "command": "sheets",
            "sheets_command": "campaign-stats",
            "workspace": "acme",
            "since": "7d",
            "dry_run": False,
            "json": False,
            "share_email": None,
            "anyone_with_link": False,
            "public": False,
            "sheet_id": None,
        }
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_dry_run_prints_payload_without_login(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            om._cmd_sheets_campaign_stats(self._args(dry_run=True))
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["template"], "campaign-stats")
        self.assertIn("sheets", payload)
        self.assertGreaterEqual(len(payload["sheets"]), 4)
        overview = payload["sheets"][0]
        self.assertIn("Campaign Overview", overview["title"])
        self.assertGreaterEqual(len(overview["rows"]), 1)

    def test_json_flag_prints_payload(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            om._cmd_sheets_campaign_stats(self._args(json=True))
        payload = json.loads(buf.getvalue())
        self.assertIn("title", payload)
        self.assertIn("acme", payload["title"].lower())

    def test_missing_workspace_exits(self):
        buf = StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit) as ctx:
            om._cmd_sheets_campaign_stats(self._args(workspace=None))
        self.assertEqual(ctx.exception.code, 1)
        data = json.loads(buf.getvalue())
        self.assertIn("error", data)

    def test_upload_requires_login(self):
        buf = StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit) as ctx:
            om._cmd_sheets_campaign_stats(self._args())
        self.assertEqual(ctx.exception.code, 1)
        data = json.loads(buf.getvalue())
        self.assertIn("login required", data["error"])


if __name__ == "__main__":
    unittest.main()
