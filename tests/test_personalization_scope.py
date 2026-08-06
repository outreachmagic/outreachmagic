"""Personalization scope: the registry, import routing, and company conflicts.

A personalization field belongs to exactly one scope. Before the registry
existed, scope was guessed per write by a name-shape heuristic and never
reported, which is why the export picker could not group columns, why the
`full` preset silently omitted every company value, and why one sheet could
write eleven different company_name values to eleven companies in silence.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))

from om_paths import set_data_root_override  # noqa: E402

import lead_export  # noqa: E402
import pipeline as om  # noqa: E402
import pipeline_personalize as pp  # noqa: E402


class ScopeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        set_data_root_override(Path(self._tmp.name))
        om.init_db()
        om.create_workspace("acme", "Acme")
        conn = om.get_conn()
        self.ws_id = om.resolve_workspace_identity(conn, "acme")["id"]
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _lead(self, **row):
        row.setdefault("company_domain", "acme.com")
        result = om.import_profiles([row], workspace="acme")
        return result["results"][0]["id"]


class RegistryTests(ScopeTestBase):
    def test_first_write_claims_the_name_for_its_scope(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        om.personalize_set(lead_id, "icp_segment", "mercedes franchise")
        scope, decided_by = pp.resolve_scope("icp_segment")
        self.assertEqual((scope, decided_by), ("lead", "registry"))

    def test_a_contradicting_write_is_refused_not_silently_rerouted(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        om.personalize_set(lead_id, "icp_segment", "mercedes franchise")
        conn = om.get_conn()
        company_id = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()["company_id"]
        conn.close()
        result = om.company_personalize_set("icp_segment", "tier a", company_id=company_id)
        self.assertEqual(result["status"], "error")
        self.assertIn("registered as a LEAD", result["error"])

    def test_an_explicit_company_prefix_beats_the_name_shape_heuristic(self):
        # `icp_tier` looks lead-scoped to looks_company_scoped(); the explicit
        # prefix says otherwise and must win.
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme",
                   company_personalized_icp_tier="A")
        self.assertEqual(pp.resolve_scope("icp_tier")[0], "company")

    def test_the_registry_reports_values_in_use(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        om.personalize_set(lead_id, "icp_segment", "mercedes franchise")
        fields = {f["field_name"]: f for f in pp.list_registered_fields(with_values=True)}
        self.assertIn("icp_segment", fields)
        self.assertEqual(fields["icp_segment"]["top_values"], [["mercedes franchise", 1]])


class ImportRoutingTests(ScopeTestBase):
    def test_the_summary_says_where_every_column_landed(self):
        result = om.import_profiles([{
            "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme",
            "company_domain": "acme.com",
            "personalized_icp_segment": "mercedes franchise",
            "company_personalized_icp_tier": "A",
        }], workspace="acme")
        routing = result["personalization_routing"]
        self.assertEqual(routing["personalized_icp_segment"]["scope"], "lead")
        self.assertEqual(routing["company_personalized_icp_tier"]["scope"], "company")
        self.assertEqual(routing["company_personalized_icp_tier"]["decided_by"], "explicit")

    def test_a_guessed_column_is_called_out_by_name(self):
        result = om.import_profiles([{
            "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme",
            "company_domain": "acme.com", "personalized_mystery": "x",
        }], workspace="acme")
        self.assertIn("personalized_mystery", result["personalization_routing_guessed"])


class CompanyConflictTests(ScopeTestBase):
    ROWS = [
        {"name": "Jane Doe", "email": "jane@acme.com", "company": "Acme",
         "company_domain": "acme.com", "company_personalized_company_name": "Acme"},
        {"name": "Bob Roe", "email": "bob@acme.com", "company": "Acme",
         "company_domain": "acme.com", "company_personalized_company_name": "Acme Motors"},
    ]

    def test_two_values_for_one_company_abort_the_write_by_default(self):
        result = om.import_profiles(self.ROWS, workspace="acme")
        self.assertEqual(len(result["company_personalization_conflicts"]), 1)
        self.assertEqual(result["company_personalized"], 0)
        self.assertEqual(om.company_personalize_get(domain="acme.com"), {})

    def test_last_wins_is_available_and_says_so(self):
        result = om.import_profiles(self.ROWS, workspace="acme",
                                    company_conflict="last-wins")
        self.assertEqual(result["company_personalized"], 1)
        self.assertEqual(
            om.company_personalize_get(domain="acme.com")["company_name"], "Acme Motors")

    def test_validation_reports_the_conflict_before_anything_is_written(self):
        result = om.import_profiles(self.ROWS, workspace="acme", dry_run=True)
        self.assertEqual(result["validation"]["company_conflicts"]["count"], 1)
        self.assertIn("company_conflicts", result["validation"]["blocking"])


class PersonalizationOnlyModeTests(ScopeTestBase):
    def test_a_merge_value_never_becomes_the_persons_name(self):
        lead_id = self._lead(name="Brian Williams", email="brian@acme.com", company="Acme")
        om.import_profiles(
            [{"lead_id": lead_id, "personalized_first_name": "Brian"}],
            workspace="acme", mode="personalization-only")
        conn = om.get_conn()
        name = conn.execute("SELECT name FROM leads WHERE id = ?", (lead_id,)).fetchone()["name"]
        conn.close()
        self.assertEqual(name, "Brian Williams")
        self.assertEqual(om.personalize_get(lead_id, layer="lead")["first_name"], "Brian")

    def test_an_unmatched_row_is_reported_not_created(self):
        result = om.import_profiles(
            [{"lead_id": 987654, "personalized_first_name": "Ghost"}],
            workspace="acme", mode="personalization-only")
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["unmatched"], 1)
        conn = om.get_conn()
        total = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        conn.close()
        self.assertEqual(total, 0)

    def test_upsert_mode_still_promotes_when_there_is_no_other_name(self):
        # The original, legitimate case: a Prosp export whose only name source
        # IS the personalized first name, and no lead_id to say otherwise.
        om.import_profiles(
            [{"email": "new@acme.com", "company": "Acme", "company_domain": "acme.com",
              "personalized_first_name": "Nadia"}],
            workspace="acme")
        conn = om.get_conn()
        name = conn.execute(
            "SELECT name FROM leads WHERE email = 'new@acme.com'").fetchone()["name"]
        conn.close()
        self.assertEqual(name, "Nadia")


class CompanyPersonalizationExportTests(ScopeTestBase):
    def _csv(self, **kwargs):
        conn = om.get_conn()
        try:
            result = lead_export.export_to_csv(
                conn, self.ws_id, workspace_slug="acme", **kwargs)
        finally:
            conn.close()
        with open(result["file"], encoding="utf-8") as fh:
            return result, list(csv.DictReader(fh))

    def setUp(self):
        super().setUp()
        self.lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        om.personalize_set(self.lead_id, "icp_segment", "mercedes franchise")
        conn = om.get_conn()
        company_id = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (self.lead_id,)).fetchone()["company_id"]
        conn.close()
        om.company_personalize_set("company_name", "Acme Motors", company_id=company_id)

    def test_full_preset_includes_company_personalization(self):
        _result, rows = self._csv(preset="full")
        self.assertEqual(rows[0]["company_personalized_company_name"], "Acme Motors")
        self.assertEqual(rows[0]["personalized_icp_segment"], "mercedes franchise")

    def test_the_legacy_bare_name_reads_through_to_company_scope(self):
        # personalized_company_name is what every existing preset and script
        # already asks for, and the value has always lived on the company.
        _result, rows = self._csv(fields=["email", "personalized_company_name"])
        self.assertEqual(rows[0]["personalized_company_name"], "Acme Motors")

    def test_the_picker_separates_the_two_scopes(self):
        conn = om.get_conn()
        try:
            options = lead_export.export_field_options(conn, self.ws_id)
        finally:
            conn.close()
        groups = {g["key"]: g["fields"] for g in options["groups"]}
        self.assertIn("personalized_icp_segment", groups["personalization_lead"])
        self.assertIn("company_personalized_company_name",
                      groups["personalization_company"])

    def test_an_export_can_filter_on_a_personalization_value(self):
        other = self._lead(name="Bob Roe", email="bob@acme.com", company="Acme")
        om.personalize_set(other, "icp_segment", "independent dealer")
        result, rows = self._csv(
            preset="sequencer-upload", personalized=["icp_segment=mercedes franchise"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(rows[0]["email"], "jane@acme.com")


if __name__ == "__main__":
    unittest.main()
