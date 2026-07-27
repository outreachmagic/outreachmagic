#!/usr/bin/env python3
"""Phone numbers as a real table, not personalization fields.

Personalization holds one value per (entity, field), so it cannot hold a
mobile and a switchboard at once, cannot normalize, and cannot dedup. It is
also a user namespace: a client CSV with its own `phone` column would collide
with the field CRM sync maps. Same argument that made `record_type` native.

The table is polymorphic (`owner_type` = lead | company) because the two
sources that drove it are a company switchboard from a Google Maps scrape and
a person's mobile from a contact provider.
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

import phone_numbers as ph  # noqa: E402
import pipeline as om  # noqa: E402


class PhoneNumberTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _lead(self, **kw):
        kw.setdefault("name", "Jane Doe")
        kw.setdefault("email", "jane@acme.com")
        kw.setdefault("company", "Acme Motors")
        return om.add_lead(**kw)["id"]

    def _company_id(self, lead_id):
        conn = om.get_conn()
        try:
            return conn.execute(
                "SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()["company_id"]
        finally:
            conn.close()

    # -- storage and normalization -------------------------------------------

    def test_add_and_list(self):
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "(612) 555-0143", label="mobile", source="apollo")
        phones = ph.list_phones("lead", lead_id)["phones"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["phone_e164"], "+16125550143")
        self.assertEqual(phones[0]["phone_raw"], "(612) 555-0143")
        self.assertEqual(phones[0]["label"], "mobile")
        self.assertEqual(phones[0]["source"], "apollo")

    def test_formatting_variants_are_one_number(self):
        """The point of storing E.164: the dedup key survives reformatting."""
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "(612) 555-0143", label="mobile")
        ph.add_phone("lead", lead_id, "612-555-0143", label="direct")
        phones = ph.list_phones("lead", lead_id)["phones"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["label"], "direct", "re-adding updates the label")

    def test_a_lead_can_hold_several_numbers(self):
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143", label="mobile")
        ph.add_phone("lead", lead_id, "612-555-0199", label="direct")
        self.assertEqual(len(ph.list_phones("lead", lead_id)["phones"]), 2)

    def test_leads_and_companies_are_separate_owners(self):
        lead_id = self._lead()
        company_id = self._company_id(lead_id)
        ph.add_phone("lead", lead_id, "612-555-0143", label="mobile")
        ph.add_phone("company", company_id, "612-555-0100", label="main", source="google_maps")
        self.assertEqual(len(ph.list_phones("lead", lead_id)["phones"]), 1)
        co = ph.list_phones("company", company_id)["phones"]
        self.assertEqual(len(co), 1)
        self.assertEqual(co[0]["label"], "main")
        self.assertEqual(co[0]["source"], "google_maps")

    # -- validation ----------------------------------------------------------

    def test_unknown_label_is_rejected_with_the_valid_list(self):
        lead_id = self._lead()
        with self.assertRaises(ph.PhoneNumberError) as ctx:
            ph.add_phone("lead", lead_id, "612-555-0143", label="cellular")
        self.assertIn("mobile", str(ctx.exception))

    def test_unknown_source_is_rejected(self):
        lead_id = self._lead()
        with self.assertRaises(ph.PhoneNumberError):
            ph.add_phone("lead", lead_id, "612-555-0143", source="mystery_provider")

    def test_unusable_number_is_rejected(self):
        lead_id = self._lead()
        with self.assertRaises(ph.PhoneNumberError):
            ph.add_phone("lead", lead_id, "n/a")

    def test_missing_owner_is_rejected(self):
        with self.assertRaises(ph.PhoneNumberError):
            ph.add_phone("lead", 999999, "612-555-0143")

    # -- primary -------------------------------------------------------------

    def test_first_number_is_primary_without_asking(self):
        """Otherwise the common case -- exactly one number -- has no primary and
        CRM sync has nothing to map."""
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143")
        self.assertEqual(ph.list_phones("lead", lead_id)["phones"][0]["is_primary"], 1)

    def test_promote_moves_the_primary(self):
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143", label="direct")
        ph.add_phone("lead", lead_id, "612-555-0199", label="mobile")
        ph.promote_phone("lead", lead_id, "612-555-0199")
        phones = ph.list_phones("lead", lead_id)["phones"]
        primaries = [p for p in phones if p["is_primary"]]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0]["phone_e164"], "+16125550199")

    def test_removing_the_primary_hands_it_to_a_survivor(self):
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143")
        ph.add_phone("lead", lead_id, "612-555-0199")
        ph.remove_phone("lead", lead_id, "612-555-0143")
        phones = ph.list_phones("lead", lead_id)["phones"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["is_primary"], 1)

    def test_promoting_a_number_not_on_the_owner_errors(self):
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143")
        with self.assertRaises(ph.PhoneNumberError):
            ph.promote_phone("lead", lead_id, "612-555-0000")

    # -- import routing ------------------------------------------------------

    def test_import_routes_phone_columns_into_the_table(self):
        summary = om.import_profiles([{
            "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme Motors",
            "phone": "612-555-0143", "phone_mobile": "612-555-0199",
            "company_phone": "612-555-0100",
        }])
        lead_id = summary["results"][0]["id"]
        labels = {p["label"]: p["phone_e164"] for p in ph.list_phones("lead", lead_id)["phones"]}
        self.assertEqual(labels.get("direct"), "+16125550143")
        self.assertEqual(labels.get("mobile"), "+16125550199")
        co = ph.list_phones("company", self._company_id(lead_id))["phones"]
        self.assertEqual([p["phone_e164"] for p in co], ["+16125550100"])
        self.assertEqual(co[0]["label"], "main")

    def test_phone_columns_never_become_personalization(self):
        """The shadow-field class this table exists to stay out of."""
        om.import_profiles([{
            "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme Motors",
            "phone": "612-555-0143", "company_phone": "612-555-0100",
        }])
        conn = om.get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization "
                "WHERE field_name IN ('phone','phone_mobile','company_phone')"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_import_label_and_source_columns_override_the_defaults(self):
        summary = om.import_profiles([{
            "name": "Bob Roe", "email": "bob@acme.com", "company": "Acme Motors",
            "phone": "612-555-0177", "phone_label": "whatsapp", "phone_source": "serper",
        }])
        p = ph.list_phones("lead", summary["results"][0]["id"])["phones"][0]
        self.assertEqual(p["label"], "whatsapp")
        self.assertEqual(p["source"], "serper")

    def test_an_unparseable_number_does_not_fail_the_row(self):
        """Row 4,000 having a junk phone must not cost you row 4,000."""
        summary = om.import_profiles([{
            "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme Motors",
            "phone": "n/a",
        }])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["phones_skipped"], 1)
        self.assertEqual(ph.list_phones("lead", summary["results"][0]["id"])["phones"], [])

    # -- merges --------------------------------------------------------------

    def test_lead_merge_carries_numbers_to_the_survivor(self):
        keep = self._lead(name="Jane Doe", email="jane@acme.com")
        loser = self._lead(name="Jane D", email="jane.d@acme.com")
        ph.add_phone("lead", keep, "612-555-0143", label="direct")
        ph.add_phone("lead", loser, "612-555-0199", label="mobile")
        om.merge_leads(keep, loser, reason="test")
        phones = ph.list_phones("lead", keep)["phones"]
        self.assertEqual(
            sorted(p["phone_e164"] for p in phones), ["+16125550143", "+16125550199"])
        self.assertEqual(len([p for p in phones if p["is_primary"]]), 1)

    def test_deleting_a_lead_sweeps_its_numbers(self):
        """No FK (owner_type is polymorphic), so the triggers have to do it."""
        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143")
        conn = om.get_conn()
        try:
            conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) n FROM phone_numbers WHERE owner_type = 'lead' AND owner_id = ?",
                (lead_id,),
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    # -- CRM ------------------------------------------------------------------

    def test_crm_sync_maps_the_lead_phone(self):
        import crm_sync

        lead_id = self._lead()
        ph.add_phone("lead", lead_id, "612-555-0143", label="mobile")
        conn = om.get_conn()
        try:
            ws_id = self._seed_workspace(conn, lead_id)
            leads = crm_sync.select_leads(conn, ws_id, lead_id=lead_id)
        finally:
            conn.close()
        self.assertEqual(leads[0]["phone"], "+16125550143")

    def test_crm_sync_falls_back_to_the_company_switchboard(self):
        """A Google Maps import gives you the company line and nothing else --
        still a better answer than an empty field."""
        import crm_sync

        lead_id = self._lead()
        ph.add_phone("company", self._company_id(lead_id), "612-555-0100",
                     label="main", source="google_maps")
        conn = om.get_conn()
        try:
            ws_id = self._seed_workspace(conn, lead_id)
            leads = crm_sync.select_leads(conn, ws_id, lead_id=lead_id)
        finally:
            conn.close()
        self.assertEqual(leads[0]["phone"], "+16125550100")

    def test_crm_sync_does_not_fall_back_to_a_fax(self):
        import crm_sync

        lead_id = self._lead()
        ph.add_phone("company", self._company_id(lead_id), "612-555-0111", label="fax")
        conn = om.get_conn()
        try:
            ws_id = self._seed_workspace(conn, lead_id)
            leads = crm_sync.select_leads(conn, ws_id, lead_id=lead_id)
        finally:
            conn.close()
        self.assertIsNone(leads[0]["phone"])

    def _seed_workspace(self, conn, lead_id):
        ws_id = "ws-phone-test"
        org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?, ?, ?, ?)",
            (ws_id, org_id, "phone-test", "Phone Test"))
        conn.execute(
            "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
            "VALUES (?, ?, ?, ?, 'replied')", (f"wl-{lead_id}", org_id, ws_id, lead_id))
        conn.commit()
        return ws_id


if __name__ == "__main__":
    unittest.main()
