#!/usr/bin/env python3
"""`leads.record_type` — is this row a person, or a stand-in for a company?

Google Maps / Apify scrapes are lists of businesses. Imported with
name = company_name they produce a "contact" that is really an account: no
email, no LinkedIn, nobody to send to. That is a legitimate stage -- import the
list, then research real contacts -- but it has to be recorded where every
query can see it.

A native column, not a `personalized_record_type` field: personalization is a
user namespace, import turns unrecognised CSV columns into personalization
(which is exactly how original_source got shadowed), and every send/enrich
eligibility check would otherwise need a join.
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
import pipeline_migration as pm  # noqa: E402


class RecordTypeTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _record_type(self, lead_id):
        conn = om.get_conn()
        try:
            return conn.execute(
                "SELECT record_type FROM leads WHERE id = ?", (lead_id,)).fetchone()["record_type"]
        finally:
            conn.close()

    # -- auto-detection ------------------------------------------------------

    def test_company_only_row_is_detected(self):
        """The Google Maps signature: the name IS the company, nothing personal."""
        om.import_profiles([{
            "name": "Vandelay Imports", "company": "Vandelay Imports",
            "company_domain": "vandelayimports.com",
        }])
        conn = om.get_conn()
        try:
            row = conn.execute(
                "SELECT id, record_type FROM leads WHERE name = 'Vandelay Imports'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["record_type"], "company_placeholder")

    def test_a_real_person_is_not_detected(self):
        om.import_profiles([{
            "name": "Jane Doe", "email": "jane@vandelayimports.com",
            "company": "Vandelay Imports",
        }])
        conn = om.get_conn()
        try:
            row = conn.execute(
                "SELECT record_type FROM leads WHERE name = 'Jane Doe'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["record_type"], "contact")

    def test_name_matching_company_but_with_an_email_is_a_contact(self):
        """A person can share their company's name -- an email means a person."""
        self.assertFalse(om.detect_company_placeholder(
            {"name": "Acme", "company": "Acme", "email": "a@acme.com"}, {}))

    def test_name_matching_company_but_with_linkedin_is_a_contact(self):
        self.assertFalse(om.detect_company_placeholder(
            {"name": "Acme", "company": "Acme", "linkedin": "linkedin.com/in/x"}, {}))

    def test_explicit_flag_overrides_detection(self):
        om.import_profiles(
            [{"name": "Jane Doe", "email": "jane@acme.com", "company": "Acme"}],
            record_type="company_placeholder")
        conn = om.get_conn()
        try:
            rt = conn.execute(
                "SELECT record_type FROM leads WHERE name = 'Jane Doe'").fetchone()["record_type"]
        finally:
            conn.close()
        self.assertEqual(rt, "company_placeholder")

    def test_csv_column_sets_it_per_row(self):
        om.import_profiles([
            {"name": "A Co", "company": "A Co", "record_type": "contact"},
        ])
        conn = om.get_conn()
        try:
            rt = conn.execute(
                "SELECT record_type FROM leads WHERE name = 'A Co'").fetchone()["record_type"]
        finally:
            conn.close()
        self.assertEqual(rt, "contact", "an explicit column beats auto-detection")

    def test_record_type_column_does_not_become_personalization(self):
        om.import_profiles([{"name": "A Co", "company": "A Co",
                             "record_type": "company_placeholder"}])
        conn = om.get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization WHERE field_name = 'record_type'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    # -- validation ----------------------------------------------------------

    def test_unknown_record_type_is_rejected(self):
        lead_id = om.add_lead(name="Jane", email="j@acme.com", company="Acme")["id"]
        result = om.set_lead_record_type(lead_id, "account")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self._record_type(lead_id), "contact")

    # -- the guards this field exists for ------------------------------------

    def _seed_workspace(self):
        conn = om.get_conn()
        try:
            org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
                ("ws-a", org_id, "wsa", "WS A"))
            conn.commit()
            return org_id, "ws-a"
        finally:
            conn.close()

    def _add_to_workspace(self, lead_id, org_id, ws_id):
        conn = om.get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id) "
                "VALUES (?,?,?,?)", (f"wl-{lead_id}", org_id, ws_id, lead_id))
            conn.commit()
        finally:
            conn.close()

    def test_contacts_search_hides_placeholders_by_default(self):
        org_id, ws_id = self._seed_workspace()
        person = om.add_lead(name="Jane", email="j@acme.com", company="Acme")["id"]
        stub = om.add_lead(name="Acme Corp", company="Acme Corp")["id"]
        om.set_lead_record_type(stub, "company_placeholder")
        for lid in (person, stub):
            self._add_to_workspace(lid, org_id, ws_id)

        conn = om.get_conn()
        try:
            default = dq.search_leads(conn, ws_id)
            only_stubs = dq.search_leads(conn, ws_id, record_type="company_placeholder")
            everything = dq.search_leads(conn, ws_id, record_type="all")
        finally:
            conn.close()
        self.assertEqual([r["lead_id"] for r in default["leads"]], [person])
        self.assertEqual([r["lead_id"] for r in only_stubs["leads"]], [stub])
        self.assertEqual(len(everything["leads"]), 2)

    def test_placeholders_are_not_email_finder_candidates(self):
        org_id, ws_id = self._seed_workspace()
        stub = om.add_lead(name="Acme Corp", company="Acme Corp")["id"]
        om.set_lead_record_type(stub, "company_placeholder")
        self._add_to_workspace(stub, org_id, ws_id)
        conn = om.get_conn()
        try:
            qualified = dq.search_leads(conn, ws_id, record_type="all", qualify_finding=True)
        finally:
            conn.close()
        self.assertEqual(
            qualified["leads"], [],
            "searching for a placeholder's email burns credits on a person who "
            "does not exist",
        )

    # -- lifecycle -----------------------------------------------------------

    def _company_with_stub_and_contact(self, *, stub_has_events: bool):
        stub = om.add_lead(name="Acme Corp", company="Acme Corp")["id"]
        om.set_lead_record_type(stub, "company_placeholder")
        person = om.add_lead(name="Jane", email="jane@acme.com", company="Acme Corp")["id"]
        conn = om.get_conn()
        try:
            cid = conn.execute(
                "SELECT company_id FROM leads WHERE id = ?", (person,)).fetchone()["company_id"]
            conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, stub))
            if stub_has_events:
                conn.execute(
                    "INSERT INTO events (lead_id, event_type, direction, channel) "
                    "VALUES (?, 'email_sent', 'outbound', 'email')", (stub,))
            conn.commit()
        finally:
            conn.close()
        return stub, person, cid

    def test_unsent_placeholder_is_deleted_once_real_contacts_exist(self):
        stub, _, cid = self._company_with_stub_and_contact(stub_has_events=False)
        preview = om.resolve_company_placeholders(cid, dry_run=True)
        self.assertEqual(preview["deletable"], 1)
        result = om.resolve_company_placeholders(cid, dry_run=False)
        self.assertEqual(result["deleted"], 1)
        conn = om.get_conn()
        try:
            self.assertIsNone(
                conn.execute("SELECT id FROM leads WHERE id = ?", (stub,)).fetchone())
        finally:
            conn.close()

    def test_placeholder_with_history_is_superseded_not_deleted(self):
        """Something was actually sent to that address. Deleting it would erase
        the record of outreach that happened."""
        stub, _, cid = self._company_with_stub_and_contact(stub_has_events=True)
        result = om.resolve_company_placeholders(cid, dry_run=False)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["superseded"], 1)
        conn = om.get_conn()
        try:
            row = conn.execute(
                "SELECT superseded_at FROM leads WHERE id = ?", (stub,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the lead must survive")
        self.assertIsNotNone(row["superseded_at"])

    def test_placeholder_alone_at_its_company_is_left_alone(self):
        stub = om.add_lead(name="Acme Corp", company="Acme Corp")["id"]
        om.set_lead_record_type(stub, "company_placeholder")
        result = om.resolve_company_placeholders(dry_run=False)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self._record_type(stub), "company_placeholder")

    # -- migration from the personalization field ----------------------------

    def test_migration_folds_personalized_record_type(self):
        lead_id = om.add_lead(name="Acme Corp", company="Acme Corp")["id"]
        conn = om.get_conn()
        try:
            conn.execute("UPDATE leads SET record_type = 'contact' WHERE id = ?", (lead_id,))
            conn.execute(
                "INSERT INTO lead_personalization (lead_id, field_name, field_value) "
                "VALUES (?, 'record_type', 'company_placeholder')", (lead_id,))
            conn.execute(
                "DELETE FROM migration_flags WHERE name = 'lead_record_type_fold'")
            conn.commit()
            stats = pm._add_lead_record_type(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(stats["folded"], 1)
        self.assertEqual(stats["shadow_rows_dropped"], 1)
        self.assertEqual(self._record_type(lead_id), "company_placeholder")

        conn = om.get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) n FROM lead_personalization WHERE field_name = 'record_type'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0, "the shadow field must not survive the fold")


if __name__ == "__main__":
    unittest.main()


class CompanyNameSignalTests(unittest.TestCase):
    """Auto-detection must require a positive business signal.

    Three real people -- sole traders whose `company` is their own name --
    were misclassified on the live database by a rule that only checked
    name == company. A misclassified person is silently dropped from sending,
    enrichment targeting and CRM sync; a missed stub costs nothing.
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

    def test_business_names_are_recognised(self):
        for name in ("Vandelay Auto Sales", "Q&L Auto Sales, Inc.",
                     "ZARKFELD HYUNDAI", "Bluthe Cargo Vans, Inc.",
                     "Ossining Brothers, Inc.", "4vanworks", "QX Bus Sales",
                     "Wernham Auto Group, LLC", "Zarkfeld Ford of Ossining"):
            with self.subTest(name=name):
                self.assertTrue(om.looks_like_company_name(name))

    def test_person_names_are_not(self):
        for name in ("Marisol Okonkwo", "Petra Lindqvist", "Delia Marchetti",
                     "Jane Doe", "Rowan Achterberg"):
            with self.subTest(name=name):
                self.assertFalse(om.looks_like_company_name(name))

    def test_sole_trader_is_not_auto_detected(self):
        self.assertFalse(om.detect_company_placeholder(
            {"name": "Marisol Okonkwo", "company": "Marisol Okonkwo"}, {}))

    def test_business_stub_still_is(self):
        self.assertTrue(om.detect_company_placeholder(
            {"name": "Vandelay Auto Sales", "company": "Vandelay Auto Sales"}, {}))

    def test_explicit_flag_still_beats_the_heuristic(self):
        """A person's name can be forced when the caller knows better."""
        om.import_profiles(
            [{"name": "Marisol Okonkwo", "company": "Marisol Okonkwo"}],
            record_type="company_placeholder")
        conn = om.get_conn()
        try:
            rt = conn.execute(
                "SELECT record_type FROM leads WHERE name = 'Marisol Okonkwo'"
            ).fetchone()["record_type"]
        finally:
            conn.close()
        self.assertEqual(rt, "company_placeholder")
