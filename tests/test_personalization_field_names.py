#!/usr/bin/env python3
"""Personalization field-name scoping and validation.

Two separate concerns used to be conflated in one predicate:

  * ROUTING an ambiguously-scoped field during CSV import (a guess), and
  * VALIDATING an explicit write (not a guess -- the caller named the scope).

Using the routing heuristic as a validator made company_personalize_set reject
any field name without a `company_` prefix, so ordinary company facts sourced
from Google Maps (`phone_google_maps`, `gm_rating`, `hours`) were unstorable.
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

import pipeline as om  # noqa: E402
import pipeline_personalize as pp  # noqa: E402


class PersonalizationFieldNameTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _company(self):
        lead = om.add_lead(name="Jane", email="j@acme.com", company="Acme Corp")
        conn = om.get_conn()
        cid = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (lead["id"],)
        ).fetchone()["company_id"]
        conn.close()
        return lead["id"], cid

    # -- the reported bug ---------------------------------------------------

    def test_company_field_without_company_prefix_is_accepted(self):
        _, cid = self._company()
        for field, value in (
            ("phone_google_maps", "+1 612 555 0147"),
            ("gm_rating", "4.6"),
            ("hours", "Mon-Fri 9-6"),
        ):
            result = pp.company_personalize_set(field, value, company_id=cid)
            self.assertEqual(result["status"], "ok", f"{field}: {result.get('error')}")

        stored = om.company_personalize_get(company_id=cid)
        self.assertEqual(stored["phone_google_maps"], "+1 612 555 0147")
        self.assertEqual(stored["gm_rating"], "4.6")
        self.assertEqual(stored["hours"], "Mon-Fri 9-6")

    def test_company_prefixed_fields_still_work(self):
        _, cid = self._company()
        self.assertEqual(
            pp.company_personalize_set("company_icebreaker", "hi", company_id=cid)["status"],
            "ok",
        )

    def test_pending_reports_non_prefixed_company_fields(self):
        _, cid = self._company()
        pending = pp.company_personalize_pending(["phone_google_maps"], limit=10)
        self.assertTrue(
            any(r["company_id"] == cid for r in pending),
            "a company missing phone_google_maps must show as pending",
        )
        pp.company_personalize_set("phone_google_maps", "+1 612 555 0147", company_id=cid)
        pending_after = pp.company_personalize_pending(["phone_google_maps"], limit=10)
        self.assertFalse(any(r["company_id"] == cid for r in pending_after))

    def test_clear_by_field_reaches_company_scope(self):
        _, cid = self._company()
        pp.company_personalize_set("phone_google_maps", "+1 612 555 0147", company_id=cid)
        result = pp.personalize_clear(field="phone_google_maps")
        self.assertEqual(result["deleted"], 1)
        self.assertNotIn("phone_google_maps", om.company_personalize_get(company_id=cid))

    # -- blast-radius guard -------------------------------------------------

    def test_clear_by_field_is_org_wide_and_needs_confirmation(self):
        """`--field X` reads like "remove my test value" and behaves like
        "drop this column from every record". Anything past a handful of rows
        must state the blast radius and stop."""
        for i in range(12):
            lead = om.add_lead(name=f"P{i}", email=f"p{i}@acme.com", company="Acme Corp")
            pp.personalize_set(lead["id"], "phone_google_maps", f"+1 612 555 01{i:02d}")

        blocked = pp.personalize_clear(field="phone_google_maps")
        self.assertEqual(blocked["status"], "error")
        self.assertIn("12", blocked["error"])
        self.assertEqual(blocked["lead_rows"], 12)

        still_there = pp.personalize_clear_preview(field="phone_google_maps")
        self.assertEqual(still_there["total"], 12, "the blocked call must not delete")

        confirmed = pp.personalize_clear(field="phone_google_maps", confirm=True)
        self.assertEqual(confirmed["deleted"], 12)

    def test_small_clears_do_not_need_confirmation(self):
        lead_id, _ = self._company()
        pp.personalize_set(lead_id, "icebreaker", "hi")
        self.assertEqual(pp.personalize_clear(field="icebreaker")["deleted"], 1)

    # -- shadow-field guard -------------------------------------------------

    def test_personalization_may_not_shadow_a_real_lead_column(self):
        lead_id, _ = self._company()
        result = pp.personalize_set(lead_id, "original_source", "apify")
        self.assertEqual(result["status"], "error")
        self.assertIn("real lead column", result["error"])

    def test_personalization_may_not_shadow_a_real_company_column(self):
        _, cid = self._company()
        result = pp.company_personalize_set("industry", "Automotive", company_id=cid)
        self.assertEqual(result["status"], "error")
        self.assertIn("real company column", result["error"])

    def test_malformed_field_names_are_rejected(self):
        lead_id, cid = self._company()
        for bad in ("has space", "UPPER-CASE!", "", "x" * 65):
            self.assertEqual(
                pp.personalize_set(lead_id, bad, "v")["status"], "error", f"{bad!r}")
            self.assertEqual(
                pp.company_personalize_set(bad, "v", company_id=cid)["status"], "error",
                f"{bad!r}")

    def test_field_names_are_normalized_to_lowercase(self):
        _, cid = self._company()
        self.assertEqual(
            pp.company_personalize_set("Phone_Google_Maps", "x", company_id=cid)["status"],
            "ok",
        )
        self.assertIn("phone_google_maps", om.company_personalize_get(company_id=cid))

    # -- routing heuristic is still a heuristic ------------------------------

    def test_lead_scope_still_redirects_company_shaped_names(self):
        lead_id, _ = self._company()
        result = pp.personalize_set(lead_id, "company_name", "Acme")
        self.assertEqual(result["status"], "error")
        self.assertIn("company-scoped", result["error"])

    def test_batch_write_surfaces_validation_errors(self):
        lead_id, _ = self._company()
        result = pp.personalize_set_batch([
            {"lead_id": lead_id, "field": "icebreaker", "value": "hi"},
            {"lead_id": lead_id, "field": "original_source", "value": "apify"},
        ])
        self.assertEqual(result["written"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("original_source", result["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
