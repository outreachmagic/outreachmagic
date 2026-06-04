"""Tests for data_freshness helpers."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import data_freshness as df  # noqa: E402
import pipeline as om  # noqa: E402


class DataFreshnessTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(df.parse_duration("5m"), 300)
        self.assertEqual(df.parse_duration("1h"), 3600)
        self.assertEqual(df.parse_duration("2d"), 172800)
        self.assertIsNone(df.parse_duration("bad"))

    def test_is_pull_fresh_enough(self):
        recent = datetime.now(timezone.utc).isoformat()
        self.assertTrue(df.is_pull_fresh_enough(recent, 300))
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.assertFalse(df.is_pull_fresh_enough(old, 300))

    def test_attach_freshness_dict(self):
        out = df.attach_freshness({"preset": "engagement"}, last_pull=None)
        self.assertEqual(out["preset"], "engagement")
        self.assertEqual(out["freshness"], "never")

    def test_pull_if_stale_skip_result(self):
        om.set_last_pull(datetime.now(timezone.utc).isoformat())
        skip = om.pull_if_stale_skip_result("5m")
        self.assertIsNotNone(skip)
        self.assertTrue(skip["skipped"])
        self.assertEqual(skip["reason"], "fresh")
        self.assertIsNone(om.pull_if_stale_skip_result("5m", force=True))


class QueryHelpTests(unittest.TestCase):
    def test_query_help_exits_zero(self):
        import subprocess

        script = SCRIPTS / "pipeline.py"
        proc = subprocess.run(
            [sys.executable, str(script), "query", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("engagement", proc.stdout)


if __name__ == "__main__":
    unittest.main()
