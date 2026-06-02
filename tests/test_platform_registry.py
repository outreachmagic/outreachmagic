#!/usr/bin/env python3
"""Tests for platform_registry.py and platform-map CLI."""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import platform_registry as pr  # noqa: E402


class TestResolveEvent(unittest.TestCase):
    def test_prosp_has_msg_replied(self):
        resolved = pr.resolve_event("prosp", "has_msg_replied", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "inbound")
        self.assertEqual(resolved.target_stage, "replied")
        self.assertEqual(resolved.reporting_bucket, "linkedin_message_reply")

    def test_prosp_has_msg_reply_alias(self):
        resolved = pr.resolve_event("prosp", "has_msg_reply", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "inbound")

    def test_prosp_send_connection(self):
        resolved = pr.resolve_event("prosp", "send_connection", {})
        self.assertEqual(resolved.local_type, "linkedin_connect")
        self.assertEqual(resolved.target_stage, "contacted")

    def test_smartlead_email_reply(self):
        resolved = pr.resolve_event("smartlead", "email_reply", {})
        self.assertEqual(resolved.local_type, "email_reply")
        self.assertEqual(resolved.direction, "inbound")
        self.assertEqual(resolved.target_stage, "replied")


class TestReplyHelpers(unittest.TestCase):
    def test_is_reply_event_prosp_legacy(self):
        self.assertTrue(pr.is_reply_event("has_msg_replied", "outbound", "linkedin"))

    def test_is_reply_event_normalized(self):
        self.assertTrue(pr.is_reply_event("linkedin_message", "inbound", "linkedin"))

    def test_reply_event_sql_contains_prosp_types(self):
        sql = pr.reply_event_sql_condition()
        self.assertIn("has_msg_replied", sql)
        self.assertIn("email_reply", sql)


class TestPlatformMapJson(unittest.TestCase):
    def test_shape(self):
        data = pr.platform_map_json()
        self.assertIn("platforms", data)
        self.assertGreater(len(data["platforms"]), 0)
        prosp = next(p for p in data["platforms"] if p["id"] == "prosp")
        vendor_types = {m["vendor_type"] for m in prosp["event_mappings"]}
        self.assertIn("has_msg_replied", vendor_types)

    def test_filter_unknown(self):
        data = pr.platform_map_json("not-a-platform")
        self.assertIn("error", data)


class TestExtractReplyBody(unittest.TestCase):
    def test_prosp_strips_prefix(self):
        raw = {
            "eventType": "has_msg_replied",
            "eventData": {"body": "A lead has replied\nRe:Thanks for reaching out"},
        }
        body = pr.extract_reply_body(
            "prosp", "linkedin_message", raw, {}, "A lead has replied"
        )
        self.assertIn("Thanks for reaching out", body)
        self.assertNotIn("A lead has replied", body)


class TestPlatformMapCli(unittest.TestCase):
    def test_cmd_platform_map(self):
        om_spec = importlib.util.spec_from_file_location("pipeline", SCRIPTS / "pipeline.py")
        om = importlib.util.module_from_spec(om_spec)
        sys.modules["pipeline"] = om
        assert om_spec.loader is not None
        om_spec.loader.exec_module(om)
        buf = io.StringIO()
        with redirect_stdout(buf):
            om.cmd_platform_map("prosp")
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data["platforms"]), 1)
        self.assertEqual(data["platforms"][0]["id"], "prosp")


if __name__ == "__main__":
    unittest.main()
