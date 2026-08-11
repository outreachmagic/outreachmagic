#!/usr/bin/env python3
"""A booked meeting belongs to the campaign that earned it, not to the calendar.

Booking tools publish the event TYPE with every scheduled-meeting webhook --
"30 Minute Meeting", "Popcam Rental Onboarding" -- and ingest stores that as the
event's campaign. Three things followed from treating it as one:

  * the calendar slot appeared in the Campaigns table beside real sequences;
  * the same lead was counted twice, once under the sequence that actually
    reached them and once under the slot they picked;
  * and because routing is campaign-name based and nobody writes a routing rule
    per calendar slot, the booking quarantined outright -- so the lead kept the
    sequencer's last label ("interested") instead of advancing to meeting_booked.

`SCHEDULING_PLATFORMS` already existed and was applied to exactly one query.
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

import json  # noqa: E402

import reporting_queries as dq  # noqa: E402
import pipeline as om  # noqa: E402

WS = "popcam"


class SchedulingCampaignTests(unittest.TestCase):
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
            (WS, "default", WS, "Popcam"))
        self.company_id = self.conn.execute(
            "INSERT INTO companies (name, domain) VALUES (?, ?)",
            ("Acme", "acme.test")).lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _lead(self, name):
        lead_id = self.conn.execute(
            "INSERT INTO leads (name, company_id) VALUES (?, ?)",
            (name, self.company_id)).lastrowid
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
        self.conn.execute(
            "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
            (campaign_id, lead_id)) if campaign_id else None
        self.conn.commit()
        return event_id

    def _booked(self, lead_id):
        """The full shape: outbound campaign reached them, then they booked."""
        outbound = self._campaign("popcam | field marketing")
        slot = self._campaign("Popcam Rental Onboarding")
        self._event(lead_id, "email_sent", "outbound", "2026-01-02T09:00:00",
                    outbound, "plusvibe")
        self._event(lead_id, "email_reply", "inbound", "2026-01-04T10:00:00",
                    outbound, "plusvibe")
        self._event(lead_id, "meeting_booked", "inbound", "2026-01-05T12:00:00",
                    slot, "calendly")
        self.conn.execute(
            "UPDATE workspace_leads SET current_status_sentiment = 'positive',"
            " current_sentiment_since = '2026-01-05T12:00:00',"
            " current_status_label = 'meeting booked' WHERE lead_id = ?", (lead_id,))
        self.conn.commit()
        return outbound, slot

    # -- classification -----------------------------------------------------

    def test_a_calendar_slot_is_classified_as_scheduling(self):
        lead = self._lead("Booker")
        _, slot = self._booked(lead)
        self.assertEqual(dq.scheduling_campaign_ids(self.conn), {slot})

    def test_a_campaign_with_any_outbound_event_is_not_scheduling(self):
        """ALL, not ANY: one stray booking must not reclassify a real sequence."""
        lead = self._lead("Mixed")
        camp = self._campaign("popcam | field marketing")
        self._event(lead, "email_sent", "outbound", "2026-01-02T09:00:00", camp, "plusvibe")
        self._event(lead, "meeting_booked", "inbound", "2026-01-05T12:00:00", camp, "calendly")
        self.assertEqual(dq.scheduling_campaign_ids(self.conn), set())

    # -- the campaigns table ------------------------------------------------

    def test_the_calendar_slot_is_not_a_campaign_row(self):
        lead = self._lead("Booker")
        self._booked(lead)
        names = [c["name"] for c in dq.campaign_audit(self.conn, WS, since="all")["campaigns"]]
        self.assertIn("popcam | field marketing", names)
        self.assertNotIn("Popcam Rental Onboarding", names)

    def test_the_meeting_is_credited_to_the_campaign_that_earned_it(self):
        lead = self._lead("Booker")
        self._booked(lead)
        rows = {c["name"]: c for c in dq.campaign_audit(self.conn, WS, since="all")["campaigns"]}
        self.assertEqual(rows["popcam | field marketing"]["meetings"], 1)

    def test_one_lead_is_counted_once_not_once_per_campaign(self):
        """The original complaint: one person, two positives."""
        lead = self._lead("Booker")
        self._booked(lead)
        audit = dq.campaign_audit(self.conn, WS, since="all")
        self.assertEqual(sum(c["positive"] for c in audit["campaigns"]), 1)
        self.assertEqual(sum(c["meetings"] for c in audit["campaigns"]), 1)

    def test_including_scheduling_brings_the_slot_back(self):
        lead = self._lead("Booker")
        self._booked(lead)
        audit = dq.campaign_audit(self.conn, WS, since="all", include_scheduling=True)
        rows = {c["name"]: c for c in audit["campaigns"]}
        self.assertIn("Popcam Rental Onboarding", rows)
        self.assertTrue(rows["Popcam Rental Onboarding"]["is_scheduling"])
        # Even shown, the meeting stays credited to the campaign that earned it.
        self.assertEqual(rows["popcam | field marketing"]["meetings"], 1)

    def test_an_untraceable_meeting_is_surfaced_not_dropped(self):
        """Excluding scheduling must not silently lose a booking."""
        lead = self._lead("Walk-in")
        slot = self._campaign("30 Minute Meeting")
        self._event(lead, "meeting_booked", "inbound", "2026-01-05T12:00:00",
                    slot, "calendly")
        audit = dq.campaign_audit(self.conn, WS, since="all")
        rows = {c["name"]: c for c in audit["campaigns"]}
        self.assertIn(dq.UNATTRIBUTED_SCHEDULING_NAME, rows)
        self.assertEqual(rows[dq.UNATTRIBUTED_SCHEDULING_NAME]["meetings"], 1)

    def test_the_campaigns_table_carries_the_daily_activity_columns(self):
        lead = self._lead("Booker")
        self._booked(lead)
        row = next(c for c in dq.campaign_audit(self.conn, WS, since="all")["campaigns"]
                   if c["name"] == "popcam | field marketing")
        for column in ("email_sent", "email_received", "dm_sent", "dm_received",
                       "connects_sent", "connects_accepted",
                       "positive", "negative", "invalid", "autoreply", "meetings"):
            self.assertIn(column, row, f"campaigns table is missing {column}")
        self.assertEqual(row["email_sent"], 1)
        self.assertEqual(row["email_received"], 1)
        self.assertNotIn("status", row)

    # -- the daily matrix agrees -------------------------------------------

    def test_the_daily_matrix_excludes_scheduling_too(self):
        """Matrix and table must reconcile, or the page contradicts itself."""
        lead = self._lead("Booker")
        self._booked(lead)
        daily = dq.campaign_daily(self.conn, WS, since="all")
        self.assertEqual(sum(d["meetings"] for d in daily["days"]), 0)
        with_sched = dq.campaign_daily(self.conn, WS, since="all", include_scheduling=True)
        self.assertEqual(sum(d["meetings"] for d in with_sched["days"]), 1)


if __name__ == "__main__":
    unittest.main()
