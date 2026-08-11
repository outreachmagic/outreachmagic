#!/usr/bin/env python3
"""Import ergonomics: the questions a dry run is asked, answered by the dry run.

Reconstructs the friction from a real 1,551-row AI Ark import where ~12 of 20
minutes went on working around the tool rather than importing:

  * a `company_conflicts` blocker that fired because every not-yet-created
    company resolves to company_id = None and therefore shared one bucket;
  * "did they have a LinkedIn before?" being unanswerable without hand-written
    SQL, because the dry run reported neither `filled` nor a summary;
  * the same person appearing twice under two profile URLs and creating two
    leads, found afterwards with a manual GROUP BY;
  * `enriched: 334` with no breakdown;
  * and 51 camelCase headers hand-mapped with a throwaway script.
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

import import_formats  # noqa: E402
import pipeline as om  # noqa: E402


class ImportErgonomicsTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()

    # -- company_conflicts must not fire on brand-new companies -------------

    def test_many_new_companies_are_not_one_conflict(self):
        """The blocker: 1,000+ distinct new companies in a single bucket.

        Each row names a DIFFERENT company, so there is no conflict at all --
        but every one of them resolved to company_id = None, and grouping by
        that id put them all together.
        """
        rows = [
            {"name": f"Person {i}", "email": f"p{i}@co{i}.test",
             "company": f"Company {i}", "company_domain": f"co{i}.test",
             "personalized_company_name": f"Co {i}"}
            for i in range(50)
        ]
        report = om.validate_import_rows(rows, mode="upsert")
        self.assertEqual(report["company_conflicts"]["count"], 0,
                         report["company_conflicts"]["sample"][:2])
        self.assertEqual(report["blocking"], [])

    def test_two_values_for_one_new_company_is_still_a_conflict(self):
        """The check still has to work -- it just has to bucket correctly."""
        rows = [
            {"name": "A", "email": "a@acme.test", "company": "Acme",
             "company_domain": "acme.test", "personalized_company_name": "Acme"},
            {"name": "B", "email": "b@acme.test", "company": "Acme",
             "company_domain": "acme.test", "personalized_company_name": "Acme Corp"},
        ]
        report = om.validate_import_rows(rows, mode="upsert")
        self.assertEqual(report["company_conflicts"]["count"], 1)
        self.assertEqual(report["blocking"], ["company_conflicts"])

    # -- the dry run answers "what would be filled" -------------------------

    def test_dry_run_names_the_columns_it_would_fill(self):
        om.import_profiles([{"name": "Val Thomas", "email": "val@aps.test",
                             "company": "APS"}])
        summary = om.import_profiles(
            [{"name": "Val Thomas", "email": "val@aps.test", "company": "APS",
              "linkedin": "linkedin.com/in/valerie-thomas-514a248",
              "title": "Director of Facilities"}],
            dry_run=True)
        row = summary["results"][0]
        self.assertEqual(row["status"], "matched")
        self.assertIn("linkedin_url", row["would_fill"])
        self.assertIn("title", row["would_fill"])
        # Already present, so not a fill.
        self.assertNotIn("email", row["would_fill"])

    def test_dry_run_reports_nothing_to_fill_for_a_complete_record(self):
        om.import_profiles([{"name": "Full Record", "email": "f@acme.test",
                             "company": "Acme", "title": "CTO",
                             "linkedin": "linkedin.com/in/full-record"}])
        summary = om.import_profiles(
            [{"name": "Full Record", "email": "f@acme.test", "company": "Acme",
              "title": "CTO", "linkedin": "linkedin.com/in/full-record"}],
            dry_run=True)
        self.assertEqual(summary["results"][0]["would_fill"], [])

    # -- enriched, broken out by field --------------------------------------

    def test_enriched_is_broken_out_by_field(self):
        om.import_profiles([{"name": "Val Thomas", "email": "val@aps.test",
                             "company": "APS"}])
        summary = om.import_profiles(
            [{"name": "Val Thomas", "email": "val@aps.test", "company": "APS",
              "linkedin": "linkedin.com/in/valerie-thomas-514a248",
              "title": "Director of Facilities"}])
        self.assertEqual(summary["enriched"], 1)
        self.assertEqual(summary["enriched_by_field"].get("linkedin_url"), 1)
        self.assertEqual(summary["enriched_by_field"].get("title"), 1)

    # -- same person, two LinkedIn URLs -------------------------------------

    def _same_person_twice(self, **kwargs):
        rows = [
            {"name": "Valerie Thomas", "company": "Atlanta Public Schools",
             "company_domain": "atlantapublicschools.us",
             "title": "Executive Director of Facilities Services",
             "linkedin": "linkedin.com/in/valerie-thomas-514a248"},
            {"name": "Valerie Thomas", "company": "Atlanta Public Schools",
             "company_domain": "atlantapublicschools.us",
             "title": "Executive Director of Facilities Services",
             "linkedin": "linkedin.com/in/valerie-thomas-5891ab3"},
        ]
        return om.import_profiles(rows, **kwargs)

    def test_the_duplicate_pair_is_reported_not_left_to_manual_sql(self):
        summary = self._same_person_twice()
        dupes = summary["duplicate_candidates"]
        self.assertTrue(dupes, "the same person twice should be reported")
        self.assertEqual(dupes[0]["name"], "Valerie Thomas")
        self.assertEqual(dupes[0]["count"], 2)

    def test_review_is_the_default_and_does_not_destroy_a_record(self):
        summary = self._same_person_twice()
        self.assertEqual(summary["created"], 2, "review must not merge")
        if summary["identity_review"]:
            self.assertEqual(summary["identity_review"][0]["resolution"], "review")

    def test_merge_mode_folds_the_second_row_into_the_first(self):
        summary = self._same_person_twice(on_identity_conflict="merge")
        if summary["identity_review"] or any(
                e.get("resolution") == "merged" for e in summary["identity_conflicts"]):
            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["matched"], 1)

    def test_an_unknown_conflict_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            om.import_profiles([], on_identity_conflict="nonsense")

    # -- the AI Ark preset ---------------------------------------------------

    def test_a_raw_ai_ark_export_is_detected_and_mapped(self):
        headers = {"Full Name", "First Name", "Last Name", "Title", "Organization",
                   "Email Business", "LinkedIn", "AI Ark People ID",
                   "Company Website", "Company Employee Count", "Company Industry",
                   "City", "State", "Country", "Company City", "Seniority",
                   "Department", "Business Status", "Company Size"}
        fmt, confidence = import_formats.detect_import_format(headers)
        self.assertEqual(fmt, "ai_ark")
        self.assertEqual(confidence, "high")

        row = import_formats.normalize_import_row({
            "Full Name": "Valerie Thomas",
            "Title": "Executive Director",
            "Organization": "Atlanta Public Schools",
            "Email Business": "vthomas@atlantapublicschools.us",
            "AI Ark People ID": "aa_12345",
            "Company Website": "atlantapublicschools.us",
            "Company Employee Count": "501-1000",
            "Company Size": "309",
            "Company Industry": "Education",
            "City": "Atlanta",
            "Seniority": "director",
            "Department": "master_operations",
        }, import_format="ai_ark")

        self.assertEqual(row["name"], "Valerie Thomas")
        self.assertEqual(row["company"], "Atlanta Public Schools")
        self.assertEqual(row["email"], "vthomas@atlantapublicschools.us")
        # The dedup key: re-pulls of the same source match on identity tier 1.
        self.assertEqual(row["external_id"], "aa_12345")
        self.assertEqual(row["company_domain"], "atlantapublicschools.us")
        # The band, not the numeric column that looks better and is wrong.
        self.assertEqual(row["headcount"], "501-1000")
        self.assertEqual(row["location_city"], "Atlanta")
        self.assertIn("seniority=director", row["notes"])
        self.assertIn("department=master_operations", row["notes"])

    def test_the_ai_ark_aliases_do_not_leak_into_generic_imports(self):
        """FORMAT_HEADER_ALIASES is scoped; HEADER_ALIASES stays global."""
        row = import_formats.normalize_import_row(
            {"Organization": "Acme", "Company Website": "acme.test"})
        self.assertNotIn("company", row)
        self.assertNotIn("company_domain", row)

    def test_re_importing_an_ai_ark_file_matches_on_external_id(self):
        raw = {"Full Name": "Repeat Person", "Email Business": "r@acme.test",
               "Organization": "Acme", "AI Ark People ID": "aa_999",
               "Company Website": "acme.test"}
        first = om.import_profiles([dict(raw)], import_format="ai_ark")
        self.assertEqual(first["created"], 1)
        second = om.import_profiles([dict(raw)], import_format="ai_ark")
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["matched"], 1)


if __name__ == "__main__":
    unittest.main()
