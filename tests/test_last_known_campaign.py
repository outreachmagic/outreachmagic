#!/usr/bin/env python3
"""Attributing a lead to a campaign when the obvious link is missing.

The replies table read the campaign off the lead's latest *reply* event. That
is null more often than it looks: a bounce, an unsubscribe and an auto-reply
all carry no campaign_id, so a lead who plainly came out of a campaign showed
"—". And a lead nobody ever emailed -- a colleague replies, or a second contact
at the same company books the meeting -- had no campaign at all.

The rule, lowest rank wins:

    0  this lead replied in it        2  a colleague replied in it
    1  this lead was sent it          3  a colleague was sent it

Rank 1 is what covers the bounce: walking back to the most recent event that
*does* carry a campaign lands on the send that bounced, without naming bounces
anywhere. Ranks 2-3 are the company fallback, and the caller is told it got one.
"""

import json
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

import dashboard_queries as dq  # noqa: E402
import pipeline as om  # noqa: E402

WS = "test-ws"


class LastKnownCampaignTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        self.conn = om.get_conn()
        om.ensure_organization(self.conn)
        self.conn.execute(
            "INSERT INTO workspaces (id, org_id, slug, name) VALUES (?, ?, ?, ?)",
            (WS, "default", WS, "Test"))
        self.company_id = self.conn.execute(
            "INSERT INTO companies (name, domain) VALUES (?, ?)",
            ("Acme", "acme.test")).lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    # -- fixture helpers ----------------------------------------------------

    def _lead(self, name, *, company=True):
        lead_id = self.conn.execute(
            "INSERT INTO leads (name, company_id) VALUES (?, ?)",
            (name, self.company_id if company else None)).lastrowid
        self.conn.execute(
            "INSERT INTO workspace_leads (workspace_id, org_id, lead_id, status) "
            "VALUES (?, ?, ?, ?)", (WS, "default", lead_id, "contacted"))
        self.conn.commit()
        return lead_id

    def _campaign(self, name):
        return self.conn.execute(
            "INSERT INTO campaigns (name) VALUES (?)", (name,)).lastrowid

    def _event(self, lead_id, event_type, direction, at, campaign_id=None,
               platform=None):
        event_id = self.conn.execute(
            "INSERT INTO events (lead_id, event_type, direction, campaign_id, "
            "metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (lead_id, event_type, direction, campaign_id,
             json.dumps({"platform": platform}) if platform else "{}", at)).lastrowid
        self.conn.execute(
            "INSERT INTO workspace_lead_events "
            "(workspace_id, org_id, lead_id, event_id, event_type, event_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (WS, "default", lead_id, event_id, event_type, at, f"{WS}:{event_id}"))
        self.conn.commit()
        return event_id

    def _resolve(self, lead_id):
        rows = dq.lead_campaign(self.conn, lead_id, WS)["workspaces"]
        return rows[0] if rows else {}

    # -- the rule -----------------------------------------------------------

    def test_bounce_falls_back_to_the_send_that_bounced(self):
        lead = self._lead("Bouncer")
        camp = self._campaign("Q3 Outbound")
        self._event(lead, "email_sent", "outbound", "2026-07-01T10:00:00", camp)
        # The bounce is later and carries no campaign — reading the latest event
        # showed "—" for a lead that obviously came from Q3 Outbound.
        self._event(lead, "email_bounce", "inbound", "2026-07-01T10:05:00", None)
        got = self._resolve(lead)
        self.assertEqual(got["campaign_name"], "Q3 Outbound")
        self.assertEqual(got["campaign_source"], "self_send")
        self.assertIsNone(got["campaign_via_lead_id"])

    def test_a_reply_outranks_a_later_send(self):
        lead = self._lead("Replier")
        replied = self._campaign("Replied Campaign")
        later = self._campaign("Later Campaign")
        self._event(lead, "email_sent", "outbound", "2026-07-01T09:00:00", replied)
        self._event(lead, "email_reply", "inbound", "2026-07-01T09:30:00", replied)
        # More recent, but only a send: the campaign that produced a conversation
        # is the more useful answer.
        self._event(lead, "email_sent", "outbound", "2026-07-05T09:00:00", later)
        got = self._resolve(lead)
        self.assertEqual(got["campaign_name"], "Replied Campaign")
        self.assertEqual(got["campaign_source"], "self_reply")

    def test_a_colleagues_campaign_is_used_and_attributed(self):
        mary = self._lead("Mary Chen")
        jack = self._lead("Jack Doe")      # never emailed; booked a meeting
        camp = self._campaign("Acme Q3")
        self._event(mary, "email_sent", "outbound", "2026-07-01T09:00:00", camp)
        self._event(jack, "meeting_booked", "inbound", "2026-07-02T09:00:00", None)
        got = self._resolve(jack)
        self.assertEqual(got["campaign_name"], "Acme Q3")
        self.assertEqual(got["campaign_source"], "company_send")
        # It must be obvious this came from someone else, and from whom.
        self.assertEqual(got["campaign_via_lead_id"], mary)
        self.assertEqual(got["campaign_via_lead_name"], "Mary Chen")

    def test_a_colleague_who_replied_beats_a_colleague_who_was_only_sent(self):
        target = self._lead("Target")
        quiet = self._lead("Quiet Colleague")
        talker = self._lead("Talkative Colleague")
        send_only = self._campaign("Send Only")
        replied = self._campaign("Got A Reply")
        # The send-only campaign is more recent, and still loses.
        self._event(talker, "email_reply", "inbound", "2026-07-01T09:00:00", replied)
        self._event(quiet, "email_sent", "outbound", "2026-07-09T09:00:00", send_only)
        got = self._resolve(target)
        self.assertEqual(got["campaign_name"], "Got A Reply")
        self.assertEqual(got["campaign_source"], "company_reply")
        self.assertEqual(got["campaign_via_lead_id"], talker)

    def test_the_leads_own_send_beats_a_colleagues_reply(self):
        target = self._lead("Target")
        colleague = self._lead("Colleague")
        own = self._campaign("Own Campaign")
        theirs = self._campaign("Their Campaign")
        self._event(colleague, "email_reply", "inbound", "2026-07-09T09:00:00", theirs)
        self._event(target, "email_sent", "outbound", "2026-07-01T09:00:00", own)
        got = self._resolve(target)
        self.assertEqual(got["campaign_name"], "Own Campaign")
        self.assertEqual(got["campaign_source"], "self_send")

    def test_no_company_means_no_company_fallback(self):
        # Otherwise every unlinked lead would inherit from every other unlinked
        # lead -- company_id IS NULL is not a company they share.
        loner = self._lead("Loner", company=False)
        other = self._lead("Other", company=False)
        camp = self._campaign("Somebody Elses")
        self._event(other, "email_sent", "outbound", "2026-07-01T09:00:00", camp)
        got = self._resolve(loner)
        self.assertIsNone(got["campaign_name"])

    def test_nothing_anywhere_returns_null_not_an_error(self):
        lead = self._lead("Untouched")
        got = self._resolve(lead)
        self.assertIsNone(got["campaign_name"])
        self.assertIsNone(got["campaign_via_lead_id"])

    # -- scheduling platforms are not campaigns -----------------------------
    #
    # Calendly sends the booked event *type* with every webhook ("30 Minute
    # Meeting"), and ingest files it as a campaign. It names a slot on your
    # calendar, not the outbound that produced the meeting, so it must never be
    # the answer to "which campaign did this lead come from".

    def test_a_calendly_event_type_is_not_a_campaign(self):
        lead = self._lead("Booker")
        camp = self._campaign("30 Minute Meeting")
        self._event(lead, "meeting_booked", "inbound", "2026-07-02T09:00:00",
                    camp, platform="calendly")
        self.assertIsNone(self._resolve(lead)["campaign_name"])

    def test_a_real_send_wins_over_a_later_calendly_booking(self):
        lead = self._lead("Booker")
        real = self._campaign("Q3 Outbound")
        booking = self._campaign("60 Minute Meeting")
        self._event(lead, "email_sent", "outbound", "2026-07-01T09:00:00", real)
        # Later, inbound, and would otherwise outrank the send on both keys.
        self._event(lead, "meeting_booked", "inbound", "2026-07-02T09:00:00",
                    booking, platform="calendly")
        got = self._resolve(lead)
        self.assertEqual(got["campaign_name"], "Q3 Outbound")
        self.assertEqual(got["campaign_source"], "self_send")

    def test_a_colleagues_booking_does_not_attribute_either(self):
        target = self._lead("Target")
        colleague = self._lead("Colleague")
        booking = self._campaign("Discovery Call")
        self._event(colleague, "meeting_booked", "inbound", "2026-07-02T09:00:00",
                    booking, platform="calendly")
        self.assertIsNone(self._resolve(target)["campaign_name"])

    def test_a_sequencer_platform_is_still_a_campaign(self):
        # The exclusion is by platform, not by event type — a meeting booked
        # through the sequencer itself still attributes.
        lead = self._lead("Booker")
        camp = self._campaign("Q3 Outbound")
        self._event(lead, "meeting_booked", "inbound", "2026-07-02T09:00:00",
                    camp, platform="plusvibe")
        self.assertEqual(self._resolve(lead)["campaign_name"], "Q3 Outbound")

    def test_attribution_does_not_cross_workspaces(self):
        other_ws = "other-ws"
        self.conn.execute(
            "INSERT INTO workspaces (id, org_id, slug, name) VALUES (?, ?, ?, ?)",
            (other_ws, "default", other_ws, "Other"))
        lead = self._lead("Target")
        colleague = self._lead("Colleague")
        camp = self._campaign("Other Workspace Campaign")
        # The colleague's event belongs to a different workspace.
        event_id = self.conn.execute(
            "INSERT INTO events (lead_id, event_type, direction, campaign_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (colleague, "email_sent", "outbound", camp, "2026-07-01T09:00:00")).lastrowid
        self.conn.execute(
            "INSERT INTO workspace_lead_events "
            "(workspace_id, org_id, lead_id, event_id, event_type, event_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (other_ws, "default", colleague, event_id, "email_sent",
             "2026-07-01T09:00:00", f"{other_ws}:{event_id}"))
        self.conn.commit()
        self.assertIsNone(self._resolve(lead)["campaign_name"])


if __name__ == "__main__":
    unittest.main()
