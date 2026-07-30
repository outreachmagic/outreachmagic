#!/usr/bin/env python3
"""Serper candidates: extract, offer, choose, and survive the round trip.

The research run deliberately refuses to guess which of nine same-named people
is the right one. What it lacked was any way to record the answer once a human
had one -- so the title and linkedin_url stayed empty even when the research
plainly contained them.

These tests pin the three properties that make the picker trustworthy:
  * ordering is ordering, never a selection (nothing comes back pre-chosen),
  * "none of these" is a recorded answer, not the absence of one, and
  * an applied value survives a push/pull round trip.
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

import pipeline as om  # noqa: E402
import serper_candidates as sc  # noqa: E402
import serper_review  # noqa: E402

# The reported result shape, sanitized. Nine near-identical people, one of whom
# is the lead -- this is the case the picker exists for.
LINKEDIN_SECTION = {
    "label": "linkedin_profile",
    "query": "site:linkedin.com/in Sam Rivera",
    "data": {"organic": [
        {"title": "Sam Rivera - Northfield College",
         "link": "https://www.linkedin.com/in/samrivera",
         "snippet": "Certified Coach, Marketing & Communications — Northfield College"},
        {"title": "Sam Rivera - Speech Language Pathologist",
         "link": "https://www.linkedin.com/in/sam-rivera-4bab731b8",
         "snippet": "Speech Language Pathologist at Lakeside Home Health"},
        {"title": "Sam Rivera - Debt collector",
         "link": "https://www.linkedin.com/in/sam-rivera-10049353",
         "snippet": "Debt collector at CCS"},
        {"title": "Not a profile", "link": "https://example.test/about",
         "snippet": "no linkedin here"},
    ]},
}

COMPANY_SECTION = {
    "label": "company_discovery_strict",
    "query": '"Northfield College" official website',
    "data": {"organic": [
        {"title": "Northfield College", "link": "https://www.northfield.test/",
         "snippet": "Contact us at info@northfield.test or admissions@northfield.test"},
        {"title": "Northfield College - LinkedIn",
         "link": "https://www.linkedin.com/company/northfield",
         "snippet": "social"},
    ]},
}

SECTIONS = [COMPANY_SECTION, LINKEDIN_SECTION]


class ExtractionTests(unittest.TestCase):
    def test_only_profile_urls_become_linkedin_candidates(self):
        out = sc.extract_linkedin_candidates(SECTIONS, name="Sam Rivera")
        self.assertTrue(all("/in/" in c["url"] for c in out))
        self.assertEqual(len(out), 3)

    def test_company_match_outranks_google_ordering(self):
        out = sc.extract_linkedin_candidates(
            SECTIONS, name="Sam Rivera", company="Northfield College")
        self.assertEqual(out[0]["url"], "https://www.linkedin.com/in/samrivera")
        # Ordering only -- nothing in the payload says "this one".
        self.assertNotIn("chosen", out[0])
        self.assertNotIn("selected", out[0])

    def test_every_candidate_carries_its_evidence(self):
        # The operator chooses from what Serper returned, not from a rank.
        for c in sc.extract_linkedin_candidates(SECTIONS, name="Sam Rivera"):
            self.assertIn("snippet", c)
            self.assertIn("title", c)

    def test_social_hosts_are_not_company_candidates(self):
        out = sc.extract_company_candidates(SECTIONS, company="Northfield College")
        self.assertIn("northfield.test", [c["domain"] for c in out])
        self.assertNotIn("linkedin.com", [c["domain"] for c in out])

    def test_suggested_title_drops_a_bare_company_name(self):
        # "Sam Rivera - Northfield College" has the employer after the dash, not
        # a role. Offering it as the title teaches the operator to stop reading.
        out = sc.extract_candidates(
            SECTIONS, name="Sam Rivera", company="Northfield College")
        top = out["linkedin"][0]
        self.assertEqual(top["suggested_title"], "")
        second = next(c for c in out["linkedin"] if "4bab731b8" in c["url"])
        self.assertEqual(second["suggested_title"], "Speech Language Pathologist")

    def test_generic_and_personal_addresses_are_classified_apart(self):
        out = sc.extract_email_candidates(SECTIONS, company_domains=["northfield.test"])
        by_email = {c["email"]: c for c in out}
        self.assertEqual(by_email["info@northfield.test"]["kind"], "public")
        # admissions@ is not in the generic list; it must not be treated as a
        # shareable mailbox just because it looks departmental.
        self.assertEqual(by_email["admissions@northfield.test"]["kind"], "personal")
        self.assertTrue(by_email["info@northfield.test"]["matches_company_domain"])

    def test_shared_mailbox_domains_are_never_company_addresses(self):
        section = {"label": "x", "data": {"organic": [
            {"title": "t", "snippet": "reach me at info@gmail.com", "link": "https://x.test"}]}}
        self.assertEqual(sc.extract_email_candidates([section]), [])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        self.lead_id = om.add_lead(name="Sam Rivera", email="sam@northfield.test")["id"]
        conn = om.get_conn()
        try:
            serper_review.store_candidates(
                conn, self.lead_id,
                sc.extract_candidates(SECTIONS, name="Sam Rivera",
                                      company="Northfield College"))
            conn.commit()
        finally:
            conn.close()

    def _queue(self, field="linkedin"):
        conn = om.get_conn()
        try:
            return serper_review.review_queue(conn, None, field=field)
        finally:
            conn.close()

    def _lead(self):
        conn = om.get_conn()
        try:
            return dict(conn.execute(
                "SELECT name, title, linkedin_url FROM leads WHERE id = ?",
                (self.lead_id,)).fetchone())
        finally:
            conn.close()

    def test_queue_offers_the_lead_with_nothing_preselected(self):
        q = self._queue()
        self.assertEqual(q["total"], 1)
        lead = q["leads"][0]
        self.assertEqual(lead["lead_id"], self.lead_id)
        self.assertTrue(lead["candidates"])
        for c in lead["candidates"]:
            self.assertNotIn("chosen", c)

    def test_applying_writes_the_field_and_leaves_the_queue(self):
        url = "https://www.linkedin.com/in/samrivera"
        result = serper_review.apply_decision(self.lead_id, "linkedin", value=url)
        self.assertEqual(result["status"], "applied")
        self.assertIn("samrivera", self._lead()["linkedin_url"] or "")
        self.assertEqual(self._queue()["total"], 0)

    def test_none_of_these_is_recorded_and_writes_nothing(self):
        result = serper_review.apply_decision(self.lead_id, "linkedin", dismissed=True)
        self.assertEqual(result["status"], "dismissed")
        self.assertIsNone(self._lead()["linkedin_url"])
        # The judgement sticks: the lead does not come back round for another ask.
        self.assertEqual(self._queue()["total"], 0)

    def test_rejected_candidates_are_kept(self):
        url = "https://www.linkedin.com/in/samrivera"
        serper_review.apply_decision(self.lead_id, "linkedin", value=url)
        conn = om.get_conn()
        try:
            blob = serper_review.load_candidates(conn, self.lead_id)
        finally:
            conn.close()
        rejected = blob["decisions"]["linkedin"]["rejected"]
        self.assertIn("https://www.linkedin.com/in/sam-rivera-4bab731b8", rejected)
        self.assertNotIn(url, rejected)

    def test_dry_run_reports_without_writing(self):
        url = "https://www.linkedin.com/in/samrivera"
        result = serper_review.apply_decision(
            self.lead_id, "linkedin", value=url, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertIsNone(self._lead()["linkedin_url"])
        self.assertEqual(self._queue()["total"], 1)

    def test_a_re_run_does_not_erase_an_existing_decision(self):
        serper_review.apply_decision(self.lead_id, "linkedin", dismissed=True)
        conn = om.get_conn()
        try:
            serper_review.store_candidates(
                conn, self.lead_id,
                sc.extract_candidates(SECTIONS, name="Sam Rivera"))
            conn.commit()
            blob = serper_review.load_candidates(conn, self.lead_id)
        finally:
            conn.close()
        self.assertTrue(blob["decisions"]["linkedin"]["dismissed"])

    def test_applying_a_company_domain_links_the_lead_to_a_company(self):
        """The whole company_domain path ran through an import that did not
        exist (`from lead_sync import ensure_company`; it lives in pipeline).
        The ImportError escaped the dispatcher, the socket closed, and the
        browser said "Failed to fetch" — so this end-to-end assertion, not a
        mock, is what pins it."""
        serper_review.apply_decision(
            self.lead_id, "company_domain", value="northfield.test")
        conn = om.get_conn()
        try:
            row = conn.execute(
                """SELECT co.domain FROM leads l JOIN companies co ON co.id = l.company_id
                   WHERE l.id = ?""", (self.lead_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "lead was not linked to a company")
        self.assertEqual(row["domain"], "northfield.test")

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(serper_review.SerperReviewError):
            serper_review.apply_decision(self.lead_id, "favourite_colour", value="blue")

    def test_a_value_or_a_dismissal_is_required(self):
        with self.assertRaises(serper_review.SerperReviewError):
            serper_review.apply_decision(self.lead_id, "linkedin")

    # -- batch --------------------------------------------------------------

    def test_batch_applies_every_decision(self):
        result = serper_review.apply_batch([
            {"lead_id": self.lead_id, "field": "linkedin",
             "value": "https://www.linkedin.com/in/samrivera"},
            {"lead_id": self.lead_id, "field": "title", "value": "Career Coach"},
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["applied"], 2)
        lead = self._lead()
        self.assertIn("samrivera", lead["linkedin_url"] or "")
        self.assertEqual(lead["title"], "Career Coach")

    def test_one_bad_row_rolls_the_whole_batch_back(self):
        # Half-applying twenty-five judgements leaves the operator with no way
        # to know which half landed.
        result = serper_review.apply_batch([
            {"lead_id": self.lead_id, "field": "title", "value": "Career Coach"},
            {"lead_id": self.lead_id, "field": "nonsense", "value": "x"},
        ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["applied"], 0)
        self.assertIsNone(self._lead()["title"])


class RoundTripTests(unittest.TestCase):
    """An applied value that does not survive a relay round trip is not applied.

    This is where a "it worked when I clicked it" feature quietly dies: the
    value is written locally, the next full pull replays a snapshot without it,
    and the field is empty again with nothing in the logs to say why.
    """

    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        self.lead_id = om.add_lead(name="Sam Rivera", email="sam@northfield.test")["id"]

    def test_applied_fields_are_on_the_sync_contract(self):
        import sync_contract

        synced = sync_contract.SYNCED_COLUMNS["leads"]
        self.assertIn("title", synced)
        self.assertIn("linkedin_url", synced)

    def test_applied_values_reach_the_push_payload(self):
        import lead_sync
        from workspace_routing import DEFAULT_ORG_ID

        serper_review.apply_decision(self.lead_id, "title", value="Career Coach")
        serper_review.apply_decision(
            self.lead_id, "linkedin", value="https://www.linkedin.com/in/samrivera")

        conn = om.get_conn()
        try:
            payload = lead_sync.build_lead_sync_payload(
                conn, DEFAULT_ORG_ID, self.lead_id)
        finally:
            conn.close()
        self.assertEqual(payload.get("title"), "Career Coach")
        # The wire field is `linkedin`, and the stored value is normalized
        # (scheme stripped) by the identity path -- assert on the identifier,
        # not on the exact string the operator pasted.
        self.assertIn("samrivera", payload.get("linkedin") or "")

    def test_a_later_snapshot_without_them_does_not_clear_them(self):
        """The apply side is truthy-guarded; prove it, don't assume it."""
        import lead_sync

        serper_review.apply_decision(self.lead_id, "title", value="Career Coach")
        conn = om.get_conn()
        try:
            # A sequencer snapshot that knows the name and nothing else.
            lead_sync.apply_agent_lead_core_payload(
                self.lead_id, {"name": "Sam Rivera"}, conn=conn)
            conn.commit()
            title = conn.execute(
                "SELECT title FROM leads WHERE id = ?", (self.lead_id,)).fetchone()["title"]
        finally:
            conn.close()
        self.assertEqual(title, "Career Coach")

    def test_candidates_blob_rides_synced_personalization(self):
        import sync_contract

        self.assertIn("field_value", sync_contract.SYNCED_COLUMNS["lead_personalization"])
        conn = om.get_conn()
        try:
            serper_review.store_candidates(
                conn, self.lead_id, sc.extract_candidates(SECTIONS, name="Sam Rivera"))
            conn.commit()
            row = conn.execute(
                "SELECT field_value FROM lead_personalization "
                "WHERE lead_id = ? AND field_name = ?",
                (self.lead_id, serper_review.CANDIDATES_FIELD)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        json.loads(row["field_value"])   # round-trips as JSON


class FreeTierQueryPatternTests(unittest.TestCase):
    """Serper's free plan refuses search-operator syntax outright:

        HTTP 400 {"message": "Query pattern not allowed for free accounts."}

    The pack's LinkedIn lookup is `site:linkedin.com/in <name>`, so on a free
    key every lead died on that query and the run reported "0 Serper calls" —
    which reads as "nothing ran" rather than "the shape was refused".
    """

    def setUp(self):
        import enrich

        self.enrich = enrich
        self.calls = []
        self.config = {"serper_endpoint": "https://example.test/search"}

    def _patch(self, responder):
        original = self.enrich._serper_post

        def fake(api_key, query, config):
            self.calls.append(query)
            return responder(query)

        self.enrich._serper_post = fake
        self.addCleanup(setattr, self.enrich, "_serper_post", original)

    @staticmethod
    def _refused():
        return ValueError(
            'Serper HTTP 400 for \'q\': {"message": "Query pattern not allowed '
            'for free accounts.","statusCode":400}')

    def test_plain_query_drops_operators_and_quotes(self):
        self.assertEqual(
            self.enrich.plain_query("site:linkedin.com/in Jane Doe VP Acme"),
            "linkedin.com/in Jane Doe VP Acme")
        self.assertEqual(
            self.enrich.plain_query('"Acme Events" official website'),
            "Acme Events official website")

    def test_a_refused_pattern_is_retried_without_operators(self):
        self._patch(lambda q: (_ for _ in ()).throw(self._refused())
                    if q.startswith("site:") else {"organic": [{"link": "ok"}]})
        got = self.enrich._serper_search_with_key(
            "k", "site:linkedin.com/in Jane Doe", self.config)
        self.assertEqual(got, {"organic": [{"link": "ok"}]})
        self.assertEqual(
            self.calls, ["site:linkedin.com/in Jane Doe", "linkedin.com/in Jane Doe"])

    def test_the_retry_happens_at_most_once(self):
        self._patch(lambda q: (_ for _ in ()).throw(self._refused()))
        with self.assertRaises(ValueError):
            self.enrich._serper_search_with_key(
                "k", "site:linkedin.com/in Jane Doe", self.config)
        self.assertEqual(len(self.calls), 2)

    def test_other_failures_are_not_retried(self):
        # A retry is a second billed call. Only the pattern refusal earns one —
        # not quota, not auth, not a 500.
        self._patch(lambda q: (_ for _ in ()).throw(
            ValueError('Serper HTTP 403 for \'q\': {"message":"Not enough credits"}')))
        with self.assertRaises(ValueError):
            self.enrich._serper_search_with_key(
                "k", "site:linkedin.com/in Jane Doe", self.config)
        self.assertEqual(len(self.calls), 1)

    def test_an_operator_free_query_is_not_retried(self):
        # Nothing to strip means the retry would be the identical call.
        self._patch(lambda q: (_ for _ in ()).throw(self._refused()))
        with self.assertRaises(ValueError):
            self.enrich._serper_search_with_key("k", "acme official website", self.config)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
