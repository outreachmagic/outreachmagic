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

import dashboard_queries as dq  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
