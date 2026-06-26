#!/usr/bin/env python3
"""Tests for platform bounce extraction, dedupe, and bounce_events storage."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402
from relay_extractors import extract_bounce_fields  # noqa: E402


# Real PlusVibe payloads sampled from relay storage (2026-05-31).
PLUSVIBE_BOUNCE_SAMPLES = [
    {
        "lead_email": "yhannahlacsa33@gmail.com",
        "sender_email": "ava@joinleadgenph.com",
        "msg": (
            "The email account that you tried to reach does not exist. Please try 550-5.1.1 "
            "double-checking the recipient's email address for typos or 550-5.1.1 unnecessary "
            "spaces. For more information, go to 550 5.1.1 https://support.google.com/mail/?p=NoSuchUser "
            "41be03b00d2f7-c8585ef54cesi6023482a12.308 - gsmtp"
        ),
        "lead_mx": "GOOGLE_WORKSPACE",
        "sender_mx": "GOOGLE_WORKSPACE",
        "webhook_event": "bounced_email",
    },
    {
        "lead_email": "t.harner@psu.edu",
        "sender_email": "sebastian@rentpopcam.com",
        "msg": "<t.harner@psu.edu>... User unknown",
        "lead_mx": "MICROSOFT365",
        "webhook_event": "bounced_email",
    },
    {
        "lead_email": "johnmichaelguevarra13@gmail.com",
        "sender_email": "ada@joinleadgenph.com",
        "msg": (
            "5.4.300 Message expired -> 452 4.2.2 The recipient's inbox is out of storage space."
        ),
        "lead_mx": "GOOGLE_WORKSPACE",
        "webhook_event": "bounced_email",
    },
]


class BounceExtractionTests(unittest.TestCase):
    def test_plusvibe_uses_msg_not_bounce_reason(self):
        raw = PLUSVIBE_BOUNCE_SAMPLES[0]
        fields = extract_bounce_fields("plusvibe", raw)
        self.assertIn("does not exist", fields["message"])
        self.assertEqual(fields["recipient_mx"], "GOOGLE_WORKSPACE")

    def test_extract_bounce_payload_empty_without_msg(self):
        raw = {"bounce_reason": "Mailbox does not exist"}
        payload = om._extract_bounce_payload(raw, "smartlead")
        self.assertEqual(payload["bounce_type"], "hard")
        self.assertEqual(payload["bounce_message"], "Mailbox does not exist")

    def test_plusvibe_payload_classifies_hard_and_soft(self):
        hard = om._extract_bounce_payload(PLUSVIBE_BOUNCE_SAMPLES[0], "plusvibe")
        self.assertEqual(hard["bounce_type"], "hard")
        self.assertEqual(hard["smtp_code"], "5.1.1")

        soft = om._extract_bounce_payload(PLUSVIBE_BOUNCE_SAMPLES[2], "plusvibe")
        self.assertEqual(soft["bounce_type"], "soft")

    def test_bounce_event_type_normalization(self):
        for et in ("email_bounced", "email.bounced", "bounced_email", "EMAIL_BOUNCED"):
            self.assertTrue(om.is_bounce_event_type(et))
            self.assertEqual(om.normalize_bounce_event_type(et), "email_bounce")

    def test_emailbison_bounce_extraction_nested_paths(self):
        raw = {
            "event": {"type": "EMAIL_BOUNCED"},
            "data": {
                "bounce": {
                    "type": "hard",
                    "reason": "550 5.1.1 The email account that you tried to reach does not exist.",
                    "message": "Mailbox does not exist",
                },
                "lead": {"email": "bounce@example.com", "mx_provider": "GOOGLE_WORKSPACE"},
            },
        }
        fields = extract_bounce_fields("emailbison", raw)
        self.assertIn("does not exist", fields["message"])
        self.assertEqual(fields["bounce_type"], "hard")
        self.assertEqual(fields["recipient_mx"], "GOOGLE_WORKSPACE")

    def test_emailbison_bounce_without_nested_bounce(self):
        raw = {
            "bounce_reason": "Mailbox full",
            "bounce_type": "soft",
        }
        fields = extract_bounce_fields("emailbison", raw)
        self.assertEqual(fields["message"], "Mailbox full")
        self.assertEqual(fields["bounce_type"], "soft")


class BounceEventsTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        om_paths.set_data_root_override(Path(self._tmp))
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        self.lead = om.resolve_lead(
            email="bounce-test@example.com",
            name="Bounce Test",
            company="Acme",
            source="relay_sync",
            source_platform="plusvibe",
        )

    def test_dedupe_one_row_per_lead_sender(self):
        conn = om.get_conn()
        lead_id = self.lead["id"]
        payload = om._extract_bounce_payload(PLUSVIBE_BOUNCE_SAMPLES[0], "plusvibe")
        sender = "ava@joinleadgenph.com"
        first = om._record_bounce_event(
            conn,
            lead_id=lead_id,
            event_id=None,
            platform="plusvibe",
            sender_email=sender,
            lead_email="bounce-test@example.com",
            payload=payload,
            event_at="2026-05-31T03:49:21Z",
        )
        second = om._record_bounce_event(
            conn,
            lead_id=lead_id,
            event_id=None,
            platform="plusvibe",
            sender_email=sender,
            lead_email="bounce-test@example.com",
            payload=payload,
            event_at="2026-05-31T15:00:00Z",
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM bounce_events WHERE lead_id = ?", (lead_id,)
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT occurrence_count, bounce_message FROM bounce_events WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(count, 1)
        self.assertEqual(row["occurrence_count"], 2)
        self.assertIn("does not exist", row["bounce_message"])

    def test_different_senders_create_separate_rows(self):
        conn = om.get_conn()
        lead_id = self.lead["id"]
        payload = om._extract_bounce_payload(PLUSVIBE_BOUNCE_SAMPLES[0], "plusvibe")
        for sender in ("sender-a@example.com", "sender-b@example.com"):
            om._record_bounce_event(
                conn,
                lead_id=lead_id,
                event_id=None,
                platform="plusvibe",
                sender_email=sender,
                lead_email="bounce-test@example.com",
                payload=payload,
                event_at="2026-05-31T03:49:21Z",
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM bounce_events WHERE lead_id = ?", (lead_id,)
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(count, 2)


class IngestBounceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        om_paths.set_data_root_override(Path(self._tmp))
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        ws = conn.execute("SELECT id, slug FROM workspaces LIMIT 1").fetchone()
        self.workspace_id = ws["id"]
        self.workspace_slug = ws["slug"]
        conn.close()
        om.add_campaign_map_cli(
            "plusvibe",
            self.workspace_slug,
            campaign_id="camp-1",
            campaign_name="Bounce Campaign",
        )

    def test_ingest_plusvibe_bounce_stores_message(self):
        raw = dict(PLUSVIBE_BOUNCE_SAMPLES[0])
        raw["campaign_name"] = "Bounce Campaign"
        event = {
            "platform": "plusvibe",
            "event_type": "bounced_email",
            "lead": raw["lead_email"],
            "sender": raw["sender_email"],
            "received_at": "2026-05-31T03:49:21Z",
            "raw": raw,
            "relay_id": 70015,
        }
        lead_id = om.ingest_relay_event(event, quiet=True)
        self.assertIsNotNone(lead_id)

        conn = om.get_conn()
        bounce = conn.execute(
            """SELECT bounce_message, bounce_type, recipient_mx, occurrence_count
               FROM bounce_events WHERE lead_id = ?""",
            (lead_id,),
        ).fetchone()
        verification = conn.execute(
            """SELECT bounce_message FROM lead_email_verification
               WHERE lead_id = ? AND source = 'platform_bounce'""",
            (lead_id,),
        ).fetchone()
        timeline = conn.execute(
            """SELECT body_preview, metadata_json FROM events
               WHERE lead_id = ? AND event_type = 'email_bounce'""",
            (lead_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(bounce)
        self.assertIn("does not exist", bounce["bounce_message"])
        self.assertEqual(bounce["bounce_type"], "hard")
        self.assertEqual(bounce["recipient_mx"], "GOOGLE_WORKSPACE")
        self.assertEqual(bounce["occurrence_count"], 1)
        self.assertIn("does not exist", verification["bounce_message"])
        meta = json.loads(timeline["metadata_json"])
        self.assertEqual(meta.get("lead_status_sentiment"), "invalid")
        self.assertIn("does not exist", meta.get("bounce_message", ""))


if __name__ == "__main__":
    unittest.main()
