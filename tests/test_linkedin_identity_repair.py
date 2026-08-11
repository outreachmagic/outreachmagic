#!/usr/bin/env python3
"""A LinkedIn URL on the row, with no identity row behind it.

Two Mark Goodmans existed with the same profile URL. It was not a normalization
bug — `normalize_linkedin()` is correct and the canonical bare form
`linkedin.com/in/<slug>` was already the overwhelming majority (104,468 rows
against 1,664 carrying `https://www.`).

The cause: one of the two leads had `leads.linkedin_url` set but **no
`linkedin_url` row in `lead_identities`**. Tier-3 matching compares identities,
so that lead was unmatchable by LinkedIn forever, and the next import of the
same person created a second record. 193 leads were in that state, 166 of them
from Prosp.

The evidence that this is the *only* duplicate mechanism: no two leads share a
LinkedIn identity value — the unique constraint holds — so a duplicate can only
exist where one side never registered.
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

import db_health  # noqa: E402
import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    normalize_linkedin,
    repair_linkedin_identities,
    set_lead_linkedin_url,
)

CANONICAL = "linkedin.com/in/markzgoodman"


class LinkedInNormalizationTests(unittest.TestCase):
    def test_every_shape_normalizes_to_the_bare_canonical_form(self):
        for raw in (
            "https://www.linkedin.com/in/markzgoodman",
            "http://www.linkedin.com/in/markzgoodman",
            "https://linkedin.com/in/markzgoodman",
            "www.linkedin.com/in/markzgoodman",
            "linkedin.com/in/markzgoodman",
            "LinkedIn.com/in/MarkZGoodman",
            "https://www.linkedin.com/in/markzgoodman/",
        ):
            self.assertEqual(normalize_linkedin(raw), CANONICAL, raw)


class RepairTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        self.conn = om.get_conn()
        om.ensure_organization(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _raw_lead(self, name, url, *, with_identity=False):
        """A lead written the way the broken path wrote them."""
        lead_id = self.conn.execute(
            "INSERT INTO leads (name, linkedin_url, uid) VALUES (?, ?, ?)",
            (name, url, f"uid_{name.lower().replace(' ', '')}_{abs(hash(url)) % 10**8}")).lastrowid
        if with_identity:
            self.conn.execute(
                """INSERT INTO lead_identities
                       (org_id, lead_id, identity_type, identity_value_normalized)
                   VALUES (?, ?, 'linkedin_url', ?)""",
                (DEFAULT_ORG_ID, lead_id, normalize_linkedin(url)))
        self.conn.commit()
        return lead_id

    # -- the write path can no longer produce the state -----------------------

    def test_resolve_lead_registers_the_identity_even_when_identities_are_passed(self):
        """The Prosp shape: caller supplies its own identity list.

        The column was written from `linkedin_url` while the identity came only
        from the caller's list, so passing email + sales-nav id set the column
        and registered neither.
        """
        result = om.resolve_lead(
            name="Mark Goodman",
            email="markzgoodman@gmail.com",
            linkedin_url="https://www.linkedin.com/in/markzgoodman",
            identities=[("email", "markzgoodman@gmail.com")],
        )
        found = self.conn.execute(
            """SELECT identity_value_normalized FROM lead_identities
                WHERE lead_id = ? AND identity_type = 'linkedin_url'""",
            (result["id"],)).fetchone()
        self.assertIsNotNone(found, "the column was written without its identity")
        self.assertEqual(found["identity_value_normalized"], CANONICAL)

    def test_the_second_import_matches_instead_of_duplicating(self):
        """The end-to-end regression: this is what made two Mark Goodmans."""
        first = om.resolve_lead(
            name="Mark Goodman", linkedin_url="https://www.linkedin.com/in/markzgoodman",
            identities=[("email", "mark@fivestar.test")], email="mark@fivestar.test")
        second = om.resolve_lead(
            name="Mark Goodman", linkedin_url="linkedin.com/in/markzgoodman")
        self.assertEqual(second["id"], first["id"], "same profile URL, two leads")

    def test_set_lead_linkedin_url_stores_the_canonical_form(self):
        lead_id = self._raw_lead("Plain Person", "linkedin.com/in/plain", with_identity=True)
        set_lead_linkedin_url(
            self.conn, lead_id, "https://www.linkedin.com/in/plainperson",
            overwrite=True)
        row = self.conn.execute(
            "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(row["linkedin_url"], "linkedin.com/in/plainperson")

    # -- the repair -----------------------------------------------------------

    def test_repair_reports_before_it_writes(self):
        self._raw_lead("Www Person", "https://www.linkedin.com/in/wwwperson")
        report = repair_linkedin_identities(self.conn)
        self.assertFalse(report["applied"])
        self.assertEqual(report["normalized"], 1)
        self.assertEqual(report["identities_backfilled"], 1)
        row = self.conn.execute(
            "SELECT linkedin_url FROM leads").fetchone()
        self.assertEqual(row["linkedin_url"], "https://www.linkedin.com/in/wwwperson",
                         "reporting must not write")

    def test_repair_normalizes_and_backfills(self):
        lead_id = self._raw_lead("Www Person", "https://www.linkedin.com/in/wwwperson")
        repair_linkedin_identities(self.conn, apply=True)
        row = self.conn.execute(
            "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(row["linkedin_url"], "linkedin.com/in/wwwperson")
        ident = self.conn.execute(
            """SELECT identity_value_normalized FROM lead_identities
                WHERE lead_id = ? AND identity_type = 'linkedin_url'""",
            (lead_id,)).fetchone()
        self.assertEqual(ident["identity_value_normalized"], "linkedin.com/in/wwwperson")

    def test_repair_surfaces_the_duplicates_it_reveals(self):
        """Backfilling exposes pairs; it must not merge them silently."""
        keeper = self._raw_lead("Mark Goodman", CANONICAL, with_identity=True)
        orphan = self._raw_lead(
            "Mark Goodman", "https://www.linkedin.com/in/markzgoodman")
        report = repair_linkedin_identities(self.conn, apply=True)
        self.assertEqual(report["duplicates_found"], 1)
        self.assertEqual(report["duplicates"][0]["lead_ids"], sorted([keeper, orphan]))
        # Both records still exist — merging is the operator's call.
        n = self.conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_repair_is_idempotent(self):
        self._raw_lead("Www Person", "https://www.linkedin.com/in/wwwperson")
        repair_linkedin_identities(self.conn, apply=True)
        again = repair_linkedin_identities(self.conn, apply=True)
        self.assertEqual(again["normalized"], 0)
        self.assertEqual(again["identities_backfilled"], 0)

    # -- the guard ------------------------------------------------------------

    def test_db_health_flags_the_gap(self):
        for i in range(12):
            self._raw_lead(f"Orphan {i}", f"linkedin.com/in/orphan{i}")
        payload = db_health.collect_db_health(self.conn, org_id=DEFAULT_ORG_ID, fast=True)
        rules = {r["id"]: r for r in payload["rulesTriggered"]}
        self.assertIn("linkedin_identity_gap", rules)
        self.assertEqual(rules["linkedin_identity_gap"]["status"], "warn")

    def test_db_health_is_quiet_when_there_is_no_gap(self):
        self._raw_lead("Fine Person", CANONICAL, with_identity=True)
        payload = db_health.collect_db_health(self.conn, org_id=DEFAULT_ORG_ID, fast=True)
        rules = {r["id"] for r in payload["rulesTriggered"]}
        self.assertNotIn("linkedin_identity_gap", rules)


class DuplicateClassificationTests(RepairTests):
    """Sharing a profile URL means three different things, and only one is a merge.

    On the live database 193 pairs shared a URL: 129 were one person twice, 41
    were two DIFFERENT people (Andrew Howk / Andrew Jensen, Rick Fette / Joe
    Fette) where one record had someone else's profile attached, and 23 were a
    Google-Maps company placeholder carrying a person's URL. Merging the last
    two categories would have fused unrelated records.
    """

    def _pair(self, name_a, name_b, *, type_b="contact"):
        a = self._raw_lead(name_a, CANONICAL, with_identity=True)
        b = self.conn.execute(
            "INSERT INTO leads (name, linkedin_url, record_type, uid) VALUES (?,?,?,?)",
            (name_b, "https://www.linkedin.com/in/markzgoodman", type_b,
             f"uid_{name_b.lower().replace(' ', '')}")).lastrowid
        self.conn.commit()
        return a, b

    def test_same_name_is_classified_as_one_person(self):
        self._pair("Mark Goodman", "Mark Goodman")
        report = repair_linkedin_identities(self.conn)
        self.assertEqual(report["duplicates"][0]["kind"], "same_person")
        self.assertEqual(report["duplicates_by_kind"]["same_person"], 1)

    def test_different_names_are_flagged_as_a_wrong_url_not_a_duplicate(self):
        self._pair("Andrew Howk", "Andrew Jensen")
        report = repair_linkedin_identities(self.conn)
        self.assertEqual(report["duplicates"][0]["kind"], "name_mismatch")
        self.assertEqual(report["duplicates_by_kind"]["same_person"], 0)

    def test_a_company_placeholder_holding_a_person_url_is_its_own_case(self):
        self._pair("Keith Kramer", "Chevrolet at Harry Brown",
                   type_b="company_placeholder")
        report = repair_linkedin_identities(self.conn)
        self.assertEqual(report["duplicates"][0]["kind"], "placeholder_url")

    def test_no_pair_is_ever_merged_by_the_repair(self):
        """The repair reports; merging is always a separate, deliberate act."""
        self._pair("Andrew Howk", "Andrew Jensen")
        repair_linkedin_identities(self.conn, apply=True)
        n = self.conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
