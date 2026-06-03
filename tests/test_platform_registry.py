#!/usr/bin/env python3
"""Tests for platform_registry.py and platform-map CLI."""

import importlib.util
import io
import json
import sys
import tempfile
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

    def test_prosp_send_msg(self):
        resolved = pr.resolve_event("prosp", "send_msg", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "outbound")
        self.assertEqual(resolved.target_stage, "contacted")
        self.assertEqual(resolved.reporting_bucket, "linkedin_message_sent")

    def test_prosp_send_msg_legacy_reporting(self):
        bucket = pr.normalize_reporting_bucket("send_msg", "outbound", "linkedin", "prosp")
        self.assertEqual(bucket, "linkedin_message_sent")
        flags = pr.classify_activity_flags("send_msg", "outbound", "linkedin")
        self.assertTrue(flags["linkedin_sent"])

    def test_prosp_accept_invite(self):
        resolved = pr.resolve_event("prosp", "accept_invite", {})
        self.assertEqual(resolved.local_type, "linkedin_connection_accepted")
        self.assertEqual(resolved.direction, "inbound")
        self.assertEqual(resolved.reporting_bucket, "linkedin_connection_accepted")

    def test_prosp_accept_invite_legacy_reporting_without_platform(self):
        bucket = pr.normalize_reporting_bucket("accept_invite", "outbound", "linkedin")
        self.assertEqual(bucket, "linkedin_connection_accepted")

    def test_heyreach_connection_request_sent(self):
        resolved = pr.resolve_event("heyreach", "connection_request_sent", {})
        self.assertEqual(resolved.local_type, "linkedin_connect")
        self.assertEqual(resolved.direction, "outbound")
        self.assertEqual(resolved.reporting_bucket, "linkedin_connection_sent")

    def test_heyreach_connection_request_accepted(self):
        resolved = pr.resolve_event("heyreach", "connection_request_accepted", {})
        self.assertEqual(resolved.local_type, "linkedin_connection_accepted")
        self.assertEqual(resolved.direction, "inbound")

    def test_heyreach_message_sent(self):
        resolved = pr.resolve_event("heyreach", "message_sent", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "outbound")
        self.assertEqual(resolved.target_stage, "contacted")
        self.assertEqual(resolved.reporting_bucket, "linkedin_message_sent")

    def test_heyreach_message_reply_received(self):
        resolved = pr.resolve_event("heyreach", "message_reply_received", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "inbound")
        self.assertEqual(resolved.target_stage, "replied")
        self.assertEqual(resolved.reporting_bucket, "linkedin_message_reply")

    def test_heyreach_every_message_reply_received(self):
        resolved = pr.resolve_event("heyreach", "every_message_reply_received", {})
        self.assertEqual(resolved.local_type, "linkedin_message")
        self.assertEqual(resolved.direction, "inbound")

    def test_heyreach_inmail_sent_and_reply(self):
        sent = pr.resolve_event("heyreach", "inmail_sent", {})
        self.assertEqual(sent.local_type, "linkedin_message")
        self.assertEqual(sent.direction, "outbound")
        reply = pr.resolve_event("heyreach", "inmail_reply_received", {})
        self.assertEqual(reply.local_type, "linkedin_message")
        self.assertEqual(reply.direction, "inbound")

    def test_smartlead_email_reply(self):
        resolved = pr.resolve_event("smartlead", "email_reply", {})
        self.assertEqual(resolved.local_type, "email_reply")
        self.assertEqual(resolved.direction, "inbound")
        self.assertEqual(resolved.target_stage, "replied")


class TestReplyHelpers(unittest.TestCase):
    def test_is_reply_event_prosp_legacy(self):
        self.assertTrue(pr.is_reply_event("has_msg_replied", "outbound", "linkedin"))

    def test_is_reply_event_heyreach_wrong_direction_still_reply(self):
        self.assertTrue(pr.is_reply_event("message_reply_received", "outbound", "linkedin"))

    def test_is_reply_event_normalized(self):
        self.assertTrue(pr.is_reply_event("linkedin_message", "inbound", "linkedin"))

    def test_reply_event_sql_contains_prosp_and_heyreach_types(self):
        sql = pr.reply_event_sql_condition()
        self.assertIn("has_msg_replied", sql)
        self.assertIn("message_reply_received", sql)
        self.assertIn("email_reply", sql)


class TestPlatformMapJson(unittest.TestCase):
    def test_shape(self):
        data = pr.platform_map_json()
        self.assertIn("platforms", data)
        self.assertGreater(len(data["platforms"]), 0)
        prosp = next(p for p in data["platforms"] if p["id"] == "prosp")
        vendor_types = {m["vendor_type"] for m in prosp["event_mappings"]}
        self.assertIn("has_msg_replied", vendor_types)
        self.assertIn("accept_invite", vendor_types)
        heyreach = next(p for p in data["platforms"] if p["id"] == "heyreach")
        hr_types = {m["vendor_type"] for m in heyreach["event_mappings"]}
        self.assertIn("message_sent", hr_types)
        self.assertIn("message_reply_received", hr_types)

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


class TestHtmlBodyNormalization(unittest.TestCase):
    def test_looks_like_html(self):
        self.assertTrue(pr.looks_like_html("<p>Hi</p>"))
        self.assertFalse(pr.looks_like_html("plain text reply"))
        self.assertFalse(pr.looks_like_html("use x < y and z > 0"))

    def test_strip_html_reply_entities(self):
        html = "<p>Hello <b>world</b> &amp; team</p>"
        self.assertEqual(pr.strip_html_reply(html, max_len=0), "Hello world & team")

    def test_normalize_skips_plain_text(self):
        plain, was_html = pr.normalize_event_body_for_storage("Thanks for reaching out!")
        self.assertEqual(plain, "Thanks for reaching out!")
        self.assertFalse(was_html)

    def test_normalize_strips_html(self):
        html = "<html><body><div>Yes, let's talk.</div></body></html>"
        plain, was_html = pr.normalize_event_body_for_storage(html)
        self.assertEqual(plain, "Yes, let's talk.")
        self.assertTrue(was_html)

    def test_strip_html_ignores_style_blocks(self):
        html = "<p>Hi</p><style>.x{color:red}</style>"
        self.assertEqual(pr.strip_html_reply(html, max_len=0), "Hi")


class TestLogEventHtmlBody(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        from om_paths import set_data_root_override  # noqa: E402

        set_data_root_override(Path(cls._tmpdir))
        om_spec = importlib.util.spec_from_file_location("pipeline", SCRIPTS / "pipeline.py")
        cls.om = importlib.util.module_from_spec(om_spec)
        sys.modules["pipeline"] = cls.om
        assert om_spec.loader is not None
        om_spec.loader.exec_module(cls.om)

    def setUp(self):
        self.om.init_db()
        conn = self.om.get_conn()
        conn.execute(
            "INSERT INTO leads (name, email, stage) VALUES (?, ?, ?)",
            ("Jane", "jane@acme.com", "prospecting"),
        )
        conn.commit()
        self.lead_id = conn.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
        conn.close()

    def test_log_event_strips_html_before_save(self):
        html = (
            "<html><body><p>Interested in learning more.</p>"
            "<style>.x{color:red}</style></body></html>"
        )
        event_id = self.om.log_event(
            self.lead_id,
            "email_reply",
            direction="inbound",
            channel="email",
            body_preview=html[:200],
            metadata={"body": html},
        )
        conn = self.om.get_conn()
        row = conn.execute(
            "SELECT body_preview, metadata_json FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        conn.close()
        meta = json.loads(row["metadata_json"])
        self.assertTrue(meta.get("body_was_html"))
        self.assertGreater(meta.get("body_original_length", 0), len(meta["body"]))
        self.assertEqual(meta["body"], "Interested in learning more.")
        self.assertEqual(row["body_preview"], "Interested in learning more.")
        self.assertNotIn("<", meta["body"])


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
