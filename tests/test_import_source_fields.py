#!/usr/bin/env python3
"""Provenance columns must be settable by their real names on import.

Until CANONICAL_SOURCE_IMPORT_FIELDS existed, only the `list_source` /
`import_name` aliases were understood. A CSV column literally headed
`original_source` was not recognised as provenance: it fell through to the
personalization loop and became `personalized_original_source`, while the real
leads.original_source kept the importer's `csv_import` placeholder. 1,285 leads
landed that way in production -- true origin in the shadow, wrong value in the
column every report groups by.
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
import pipeline_migration as pm  # noqa: E402


class ImportSourceFieldTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _lead(self, email):
        conn = om.get_conn()
        try:
            return dict(conn.execute(
                "SELECT * FROM leads WHERE email = ?", (email,)).fetchone())
        finally:
            conn.close()

    def _personalization(self, email):
        conn = om.get_conn()
        try:
            return {
                r["field_name"]: r["field_value"]
                for r in conn.execute(
                    "SELECT p.field_name, p.field_value FROM lead_personalization p "
                    "JOIN leads l ON l.id = p.lead_id WHERE l.email = ?", (email,))
            }
        finally:
            conn.close()

    # -- the reported bug ---------------------------------------------------

    def test_canonical_source_columns_write_real_lead_columns(self):
        om.import_profiles([{
            "name": "Ann Lee", "email": "ann@dealer.com", "company": "Dealer Co",
            "original_source": "google_maps",
            "original_source_detail": "car dealer keywords, 120km",
        }])
        lead = self._lead("ann@dealer.com")
        self.assertEqual(lead["original_source"], "google_maps")
        self.assertEqual(lead["original_source_detail"], "car dealer keywords, 120km")

    def test_canonical_source_columns_do_not_become_personalization(self):
        om.import_profiles([{
            "name": "Ann Lee", "email": "ann@dealer.com", "company": "Dealer Co",
            "original_source": "google_maps",
            "original_source_detail": "car dealer keywords, 120km",
            "latest_source": "google_maps",
            "latest_source_detail": "car dealer keywords, 120km",
        }])
        pers = self._personalization("ann@dealer.com")
        for shadow in ("original_source", "original_source_detail",
                       "latest_source", "latest_source_detail"):
            self.assertNotIn(shadow, pers, f"{shadow} must not shadow a real column")

    def test_latest_source_columns_write_real_lead_columns(self):
        om.import_profiles([{
            "name": "Ann Lee", "email": "ann@dealer.com", "company": "Dealer Co",
            "latest_source": "apollo", "latest_source_detail": "q3 list",
        }])
        lead = self._lead("ann@dealer.com")
        self.assertEqual(lead["latest_source"], "apollo")
        self.assertEqual(lead["latest_source_detail"], "q3 list")

    # -- precedence and overwrite semantics ---------------------------------

    def test_canonical_column_beats_legacy_alias(self):
        om.import_profiles([{
            "name": "Ann Lee", "email": "ann@dealer.com", "company": "Dealer Co",
            "original_source": "google_maps", "list_source": "some_list",
        }])
        self.assertEqual(self._lead("ann@dealer.com")["original_source"], "google_maps")

    def test_legacy_alias_still_works(self):
        om.import_profiles([{
            "name": "Ann Lee", "email": "ann@dealer.com", "company": "Dealer Co",
            "list_source": "sales_navigator", "import_name": "q3 list",
        }])
        lead = self._lead("ann@dealer.com")
        self.assertEqual(lead["original_source"], "sales_navigator")
        self.assertEqual(lead["original_source_detail"], "q3 list")

    def test_original_source_is_fill_only_by_default(self):
        """First-touch provenance is the point of the column: a later import
        must not silently rewrite where a lead originally came from."""
        om.import_profiles([{"name": "Ann", "email": "ann@dealer.com",
                             "company": "D", "original_source": "google_maps"}])
        om.import_profiles([{"name": "Ann", "email": "ann@dealer.com",
                             "company": "D", "original_source": "apollo"}])
        self.assertEqual(self._lead("ann@dealer.com")["original_source"], "google_maps")

    def test_overwrite_source_replaces_it_when_asked(self):
        om.import_profiles([{"name": "Ann", "email": "ann@dealer.com",
                             "company": "D", "original_source": "google_maps"}])
        om.import_profiles(
            [{"name": "Ann", "email": "ann@dealer.com", "company": "D",
              "original_source": "apollo", "original_source_detail": "q3"}],
            overwrite_source=True,
        )
        lead = self._lead("ann@dealer.com")
        self.assertEqual(lead["original_source"], "apollo")
        self.assertEqual(lead["original_source_detail"], "q3")

    def test_dry_run_reports_the_source_mapping(self):
        summary = om.import_profiles([{
            "name": "Ann", "email": "ann@dealer.com", "company": "D",
            "original_source": "google_maps", "list_source": "legacy",
        }], dry_run=True)
        mapped = summary.get("source_fields_mapped") or []
        self.assertIn("original_source -> leads.original_source", mapped)
        self.assertIn("list_source -> leads.original_source (alias)", mapped)
        self.assertIn("--overwrite-source", summary.get("source_fields_note", ""))


class ShadowSourceFoldTests(unittest.TestCase):
    """The backfill for leads already carrying shadow rows."""

    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.execute("DELETE FROM migration_flags WHERE name = 'shadow_source_personalization_fold'")
        conn.commit()
        conn.close()

    def _seed_shadow(self, email, *, real: dict, shadow: dict):
        lead_id = om.add_lead(name="Ann", email=email, company="Dealer Co")["id"]
        conn = om.get_conn()
        try:
            for col, val in real.items():
                conn.execute(f"UPDATE leads SET {col} = ? WHERE id = ?", (val, lead_id))
            for field, val in shadow.items():
                conn.execute(
                    "INSERT INTO lead_personalization (lead_id, field_name, field_value) "
                    "VALUES (?, ?, ?)", (lead_id, field, val))
            conn.commit()
        finally:
            conn.close()
        return lead_id

    def _fold(self):
        conn = om.get_conn()
        try:
            stats = pm._fold_shadow_source_personalization(conn)
            conn.commit()
            return stats
        finally:
            conn.close()

    def _lead(self, lead_id):
        conn = om.get_conn()
        try:
            return dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
        finally:
            conn.close()

    def test_generic_placeholder_is_displaced_by_the_shadow(self):
        lead_id = self._seed_shadow(
            "a@d.com",
            real={"original_source": "csv_import", "original_source_detail": None},
            shadow={"original_source": "google_maps", "original_source_detail": "kw list"},
        )
        self._fold()
        lead = self._lead(lead_id)
        self.assertEqual(lead["original_source"], "google_maps")
        self.assertEqual(lead["original_source_detail"], "kw list")

    def test_a_real_user_chosen_source_is_not_overwritten(self):
        lead_id = self._seed_shadow(
            "b@d.com",
            real={"original_source": "sales_navigator"},
            shadow={"original_source": "google_maps"},
        )
        self._fold()
        self.assertEqual(self._lead(lead_id)["original_source"], "sales_navigator")

    def test_transferred_shadows_are_retired_and_others_kept(self):
        keep = self._seed_shadow(
            "c@d.com", real={"original_source": "sales_navigator"},
            shadow={"original_source": "google_maps"})
        moved = self._seed_shadow(
            "d@d.com", real={"original_source": "csv_import"},
            shadow={"original_source": "google_maps"})
        stats = self._fold()
        conn = om.get_conn()
        try:
            remaining = {
                r["lead_id"] for r in conn.execute(
                    "SELECT lead_id FROM lead_personalization WHERE field_name = 'original_source'")
            }
        finally:
            conn.close()
        self.assertIn(keep, remaining, "an untransferred value must not be dropped")
        self.assertNotIn(moved, remaining, "a transferred value must not linger as a shadow")
        self.assertEqual(stats["shadow_rows_kept"], 1)

    def test_latest_source_pair_moves_together_or_not_at_all(self):
        """Filling the detail while the source names a different, later import
        yields 'latest touch: list-B, described by: list-A' -- worse than NULL."""
        lead_id = self._seed_shadow(
            "e@d.com",
            real={"latest_source": "list-b-v3", "latest_source_detail": None},
            shadow={"latest_source": "google_maps", "latest_source_detail": "kw list"},
        )
        self._fold()
        lead = self._lead(lead_id)
        self.assertEqual(lead["latest_source"], "list-b-v3")
        self.assertIsNone(lead["latest_source_detail"])

    def test_latest_source_pair_fills_when_both_empty(self):
        lead_id = self._seed_shadow(
            "f@d.com", real={"latest_source": None, "latest_source_detail": None},
            shadow={"latest_source": "google_maps", "latest_source_detail": "kw list"})
        self._fold()
        lead = self._lead(lead_id)
        self.assertEqual(lead["latest_source"], "google_maps")
        self.assertEqual(lead["latest_source_detail"], "kw list")

    def test_transport_strings_are_never_promoted_to_provenance(self):
        lead_id = self._seed_shadow(
            "g@d.com", real={"original_source": "csv_import"},
            shadow={"original_source": "agent_sync"})
        self._fold()
        self.assertEqual(self._lead(lead_id)["original_source"], "csv_import")

    def test_fold_runs_once(self):
        self._seed_shadow("h@d.com", real={"original_source": "csv_import"},
                          shadow={"original_source": "google_maps"})
        self._fold()
        self.assertEqual(self._fold(), {"skipped": True})


if __name__ == "__main__":
    unittest.main()
