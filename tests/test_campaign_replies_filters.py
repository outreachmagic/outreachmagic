#!/usr/bin/env python3
"""Replies list: sentiment / lead-status filters, and facets that stay complete.

The facets are computed BEFORE the two filters apply. If they weren't, picking
"positive" would strip the status-label dropdown down to the labels that happen
to be positive, and you could never get back to the others without clearing the
filter first — the dropdown has to keep offering every option available for the
current campaign and date selection.
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

import reporting_queries as dq  # noqa: E402
import pipeline as om  # noqa: E402


class CampaignRepliesFilterTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        self.org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
            ("ws-a", self.org_id, "wsa", "WS A"))
        conn.commit()
        conn.close()
        self.ws = "ws-a"

    def _replier(self, name, sentiment, label, *, since="2026-07-01 10:00:00"):
        lead_id = om.add_lead(name=name, email=f"{name.lower()}@acme.com", company="Acme")["id"]
        conn = om.get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO workspace_leads
                       (id, org_id, workspace_id, lead_id, current_status_sentiment,
                        current_status_label, current_sentiment_since)
                   VALUES (?,?,?,?,?,?,?)""",
                (f"wl-{lead_id}", self.org_id, self.ws, lead_id, sentiment, label, since))
            conn.commit()
        finally:
            conn.close()
        return lead_id

    def _replies(self, **kw):
        conn = om.get_conn()
        try:
            return dq.campaign_replies(conn, self.ws, **kw)
        finally:
            conn.close()

    def _seed(self):
        self._replier("Alice", "positive", "interested")
        self._replier("Bob", "positive", "meeting_booked")
        self._replier("Cara", "negative", "not_interested")

    def test_unfiltered_returns_everyone_with_a_sentiment(self):
        self._seed()
        self.assertEqual(len(self._replies()["replies"]), 3)

    def test_sentiment_filter_narrows(self):
        self._seed()
        names = {r["lead_name"] for r in self._replies(sentiment="positive")["replies"]}
        self.assertEqual(names, {"Alice", "Bob"})

    def test_status_label_filter_narrows(self):
        self._seed()
        names = {r["lead_name"] for r in self._replies(status_label="interested")["replies"]}
        self.assertEqual(names, {"Alice"})

    def test_both_filters_compose(self):
        self._seed()
        d = self._replies(sentiment="positive", status_label="meeting_booked")
        self.assertEqual([r["lead_name"] for r in d["replies"]], ["Bob"])

    def test_filters_are_case_insensitive(self):
        self._seed()
        self.assertEqual(len(self._replies(sentiment="POSITIVE")["replies"]), 2)

    # -- the facets ----------------------------------------------------------

    def test_facets_list_every_value_with_counts(self):
        self._seed()
        f = self._replies()["facets"]
        self.assertEqual({r["value"]: r["n"] for r in f["sentiment"]},
                         {"positive": 2, "negative": 1})
        self.assertEqual({r["value"] for r in f["status_label"]},
                         {"interested", "meeting_booked", "not_interested"})

    def test_facets_ignore_the_filters_they_populate(self):
        """The whole point: filtering to positive must not empty the status
        dropdown of every label that isn't positive."""
        self._seed()
        f = self._replies(sentiment="positive")["facets"]
        self.assertEqual({r["value"] for r in f["status_label"]},
                         {"interested", "meeting_booked", "not_interested"})
        self.assertEqual({r["value"] for r in f["sentiment"]}, {"positive", "negative"})

    def test_facets_do_respect_campaign_and_range(self):
        """They are scoped to the current selection — just not to themselves."""
        self._replier("Alice", "positive", "interested", since="2026-07-01 10:00:00")
        self._replier("Cara", "negative", "not_interested", since="2026-01-01 10:00:00")
        f = self._replies(since="2026-06-01")["facets"]
        self.assertEqual({r["value"] for r in f["sentiment"]}, {"positive"})

    def test_a_filter_matching_nothing_returns_an_empty_list_not_an_error(self):
        self._seed()
        d = self._replies(sentiment="positive", status_label="not_interested")
        self.assertEqual(d["replies"], [])
        self.assertTrue(d["facets"]["sentiment"], "facets survive an empty result")


    # -- the row carries enough to work the lead without opening it ---------
    #
    # The table used to be Lead / Sentiment / Status / Subject / Campaign /
    # Since, which meant opening every row to find out who the person actually
    # was. These are the columns the pane was being opened for.

    def test_a_reply_row_carries_the_contact_columns(self):
        lead_id = self._replier("Alice", "positive", "interested")
        conn = om.get_conn()
        try:
            conn.execute("UPDATE leads SET title = ? WHERE id = ?", ("VP Ops", lead_id))
            conn.execute(
                "INSERT OR IGNORE INTO workspace_lead_tags (workspace_id, lead_id, tag) "
                "VALUES (?,?,?)", (self.ws, lead_id, "priority"))
            conn.execute(
                "UPDATE workspace_leads SET last_activity_at = ? WHERE lead_id = ?",
                ("2026-07-04 12:00:00", lead_id))
            conn.commit()
        finally:
            conn.close()
        row = self._replies()["replies"][0]
        self.assertEqual(row["company"], "Acme")
        self.assertEqual(row["title"], "VP Ops")
        self.assertEqual(row["email"], "alice@acme.com")
        self.assertEqual(row["tags"], "priority")
        self.assertEqual(row["last_activity_at"], "2026-07-04 12:00:00")
        # Distinct from the sentiment anchor, which is what the range filters on.
        self.assertEqual(row["event_at"], "2026-07-01 10:00:00")

    def test_a_lead_with_no_tags_reports_none_rather_than_dropping_out(self):
        self._replier("Alice", "positive", "interested")
        row = self._replies()["replies"][0]
        self.assertIsNone(row["tags"])


class UnclassifiedReplyTests(CampaignRepliesFilterTests):
    """A reply with no sentiment is still a reply.

    The Replies card was one row per lead keyed on `current_status_sentiment` /
    `current_sentiment_since`, so a lead with no sentiment could not appear in
    it whatever they actually did. Email sequencers ship a status label with
    every reply and that is what sets sentiment; Prosp ships none -- all 715
    inbound LinkedIn events carried `lead_status_raw` NULL. The whole channel
    was therefore missing from the card that claims to list replies: 92 leads
    had a LinkedIn DM reply and only 12 showed up.
    """

    def _dm_replier(self, name, *, at="2026-07-02 12:00:00"):
        """A LinkedIn DM reply, exactly as Prosp delivers it: no status label."""
        lead_id = om.add_lead(name=name, linkedin_url=f"linkedin.com/in/{name.lower()}")["id"]
        conn = om.get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO workspace_leads
                       (id, org_id, workspace_id, lead_id)
                   VALUES (?,?,?,?)""",
                (f"wl-{lead_id}", self.org_id, self.ws, lead_id))
            event_id = conn.execute(
                """INSERT INTO events
                       (lead_id, event_type, direction, channel, subject,
                        metadata_json, created_at)
                   VALUES (?, 'linkedin_message', 'inbound', 'linkedin', ?, '{}', ?)""",
                (lead_id, f"{name} replied on LinkedIn", at)).lastrowid
            conn.execute(
                """INSERT INTO workspace_lead_events
                       (workspace_id, org_id, lead_id, event_id, event_type,
                        event_at, idempotency_key)
                   VALUES (?,?,?,?,'linkedin_message',?,?)""",
                (self.ws, self.org_id, lead_id, event_id, at, f"k-{event_id}"))
            conn.commit()
        finally:
            conn.close()
        return lead_id

    def test_a_linkedin_dm_reply_appears_in_the_list(self):
        self._dm_replier("Markzgoodman")
        names = [r["lead_name"] for r in self._replies()["replies"]]
        self.assertIn("Markzgoodman", names)

    def test_it_is_classified_as_a_human_reply_with_no_sentiment(self):
        """Honest, not invented. Defaulting DMs to 'positive' would corrupt
        every positive count on the Campaigns page."""
        self._dm_replier("Markzgoodman")
        row = next(r for r in self._replies()["replies"]
                   if r["lead_name"] == "Markzgoodman")
        self.assertEqual(row["reply_kind"], "human")
        self.assertIsNone(row["sentiment"])

    def test_it_is_anchored_on_the_reply_time(self):
        self._dm_replier("Markzgoodman", at="2026-07-02 12:00:00")
        row = next(r for r in self._replies()["replies"]
                   if r["lead_name"] == "Markzgoodman")
        self.assertEqual(str(row["event_at"])[:10], "2026-07-02")

    def test_the_date_range_still_applies_to_it(self):
        self._dm_replier("Recent", at="2026-07-20 12:00:00")
        self._dm_replier("Ancient", at="2020-01-01 12:00:00")
        names = {r["lead_name"] for r in self._replies(since="2026-07-01")["replies"]}
        self.assertIn("Recent", names)
        self.assertNotIn("Ancient", names)

    def test_sentiment_bearing_leads_are_unaffected(self):
        """Sentiment still wins where it exists, so the daily columns still
        reconcile with this list."""
        self._seed()
        self._dm_replier("Markzgoodman")
        d = self._replies()
        self.assertEqual(len(d["replies"]), 4)
        self.assertEqual(
            {r["lead_name"] for r in self._replies(sentiment="positive")["replies"]},
            {"Alice", "Bob"})

    def test_a_lead_with_no_reply_and_no_sentiment_stays_out(self):
        om.add_lead(name="Silent", email="silent@acme.com", company="Acme")
        self._seed()
        self.assertEqual(len(self._replies()["replies"]), 3)


if __name__ == "__main__":
    unittest.main()
