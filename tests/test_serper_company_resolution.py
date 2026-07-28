#!/usr/bin/env python3
"""Serper must never search for a company it cannot name.

A lead with no company text produced the query `"" official website`. The cause:
is_non_company_name("") is False -- it answers "is this a known non-company
word", and the empty string is not one -- so a blank company passed the guard
and got interpolated straight into the query. Google returns whatever it likes
for a bare phrase (usa.gov, state.gov, ...), the credit is spent, and the saved
research reads as though the search succeeded.

Two fixes, tested here:
  * presence is checked separately from junk-value-ness, and
  * the company is resolved from the linked companies row (and failing that, the
    lead's professional email domain) before the pack is built -- leads.company
    is free text and is blank on exactly the leads that get sent here.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import dashboard_actions  # noqa: E402
import enrich  # noqa: E402
import pipeline as om  # noqa: E402

FAKE_RESULT = {"organic": [{"title": "x", "link": "https://x.test"}]}


def _labels(pack):
    return [q["label"] for q in pack]


class BuildQueriesTests(unittest.TestCase):
    """The pack itself, with no database in the way."""

    def test_blank_company_builds_no_company_query(self):
        pack = enrich.build_serper_queries(
            {"full_name": "Sam Rivera", "company_name": ""})
        self.assertEqual(_labels(pack), ["linkedin_profile"])
        # The regression, stated directly.
        self.assertFalse(any('""' in q["query"] for q in pack))

    def test_blank_company_still_looks_up_the_person(self):
        # Finding the person is independently useful, and is often how the
        # company gets identified. It must not be collateral damage.
        pack = enrich.build_serper_queries(
            {"full_name": "Sam Rivera", "company_name": ""})
        self.assertIn("site:linkedin.com/in Sam Rivera", pack[0]["query"])

    def test_domain_stands_in_for_a_missing_company_name(self):
        pack = enrich.build_serper_queries({
            "full_name": "Sam Rivera", "company_name": "",
            "company_domain": "northfield.test",
        })
        self.assertIn("company_discovery_strict", _labels(pack))
        strict = next(q for q in pack if q["label"] == "company_discovery_strict")
        self.assertEqual(strict["query"], '"northfield.test" official website')

    def test_real_company_name_still_wins_over_the_domain(self):
        pack = enrich.build_serper_queries({
            "full_name": "Jane Doe", "company_name": "Dealer Co",
            "company_domain": "dealer.test",
        })
        strict = next(q for q in pack if q["label"] == "company_discovery_strict")
        self.assertEqual(strict["query"], '"Dealer Co" official website')

    def test_junk_company_name_falls_back_to_the_domain(self):
        # "Self-Employed" is a real string but not a real company. Previously it
        # suppressed the company query outright; now a usable domain rescues it.
        pack = enrich.build_serper_queries({
            "full_name": "Jane Doe", "company_name": "Self-Employed",
            "company_domain": "janedoe.test",
        })
        strict = next(q for q in pack if q["label"] == "company_discovery_strict")
        self.assertEqual(strict["query"], '"janedoe.test" official website')

    def test_junk_company_and_no_domain_builds_no_company_query(self):
        pack = enrich.build_serper_queries(
            {"full_name": "Jane Doe", "company_name": "Freelance"})
        self.assertEqual(_labels(pack), ["linkedin_profile"])


class SharedDomainTests(unittest.TestCase):
    def test_shared_mailbox_domains_are_not_company_identifiers(self):
        # gmail.com belongs to a mail provider, not to the lead's employer.
        self.assertEqual(
            dashboard_actions.SyncManager._company_search_domain("gmail.com"), "")
        self.assertEqual(
            dashboard_actions.SyncManager._company_search_domain("  "), "")
        self.assertEqual(
            dashboard_actions.SyncManager._company_search_domain("Northfield.TEST"),
            "northfield.test")


class ResolveFromDatabaseTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _queries_for(self, lead_id):
        mgr = dashboard_actions.SyncManager()
        with mock.patch("enrich.load_config", return_value={}), \
             mock.patch("enrich.serper_search", return_value=FAKE_RESULT) as search:
            mgr._run_serper("", [lead_id])
        return [c.args[0] for c in search.call_args_list]

    def test_no_company_text_uses_the_professional_email_domain(self):
        lead_id = om.add_lead(name="Sam Rivera", email="srivera@northfield.test")["id"]
        queries = self._queries_for(lead_id)
        self.assertFalse(any('""' in q for q in queries), queries)
        self.assertTrue(
            any("northfield.test" in q and "official website" in q for q in queries),
            queries)

    def test_linked_company_name_beats_blank_free_text(self):
        lead_id = om.add_lead(name="Jane Doe", email="jane@acme.test")["id"]
        conn = om.get_conn()
        try:
            # add_lead already derived the company from the email domain; name it.
            conn.execute("UPDATE companies SET name = ? WHERE domain = ?",
                         ("Acme Widgets", "acme.test"))
            conn.execute("UPDATE leads SET company = NULL WHERE id = ?", (lead_id,))
            conn.commit()
        finally:
            conn.close()
        queries = self._queries_for(lead_id)
        self.assertTrue(
            any('"Acme Widgets" official website' == q for q in queries), queries)

    def test_missing_identifier_is_recorded_on_the_attempt(self):
        # "no company results" must be distinguishable from "we had nothing to
        # search for" when someone reads the provider-runs panel later.
        lead_id = om.add_lead(name="Nomad Person", email="nobody@gmail.com")["id"]
        self._queries_for(lead_id)
        conn = om.get_conn()
        try:
            row = conn.execute(
                "SELECT metadata_json FROM lead_provider_attempts "
                "WHERE lead_id = ? AND provider = 'serper'", (lead_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIn("no_company_identifier", row["metadata_json"])


if __name__ == "__main__":
    unittest.main()
