#!/usr/bin/env python3
"""Company domain model: purpose, detach, split.

Purpose lives on `company_identities` — the prospect's alias set that email
finding actually walks — not on `sender_domains`, which is your own cold-email
sending infrastructure. The company pane used to render both under one
"domains" heading, which is how they became indistinguishable.
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


class CompanyDomainTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _company(self, name, domain, extra_domains=()):
        conn = om.get_conn()
        try:
            cid = om.ensure_company(conn, name=name, domain=domain)
            for d in extra_domains:
                conn.execute(
                    """INSERT OR IGNORE INTO company_identities
                           (org_id, company_id, identity_type, identity_value_normalized, source)
                       VALUES (?, ?, 'domain', ?, 'test')""",
                    (om.DEFAULT_ORG_ID, cid, d))
            conn.commit()
            return cid
        finally:
            conn.close()

    def _domains(self, company_id):
        conn = om.get_conn()
        try:
            return {
                r["identity_value_normalized"]: r["purpose"]
                for r in conn.execute(
                    """SELECT identity_value_normalized, purpose FROM company_identities
                        WHERE company_id = ? AND identity_type = 'domain'""",
                    (company_id,),
                ).fetchall()
            }
        finally:
            conn.close()

    def _company_domain(self, company_id):
        conn = om.get_conn()
        try:
            return conn.execute(
                "SELECT domain FROM companies WHERE id = ?", (company_id,)).fetchone()["domain"]
        finally:
            conn.close()

    # -- purpose -------------------------------------------------------------

    def test_set_purpose_on_a_known_domain(self):
        cid = self._company("Acme", "acme.com", ["acme.eu"])
        result = om.set_company_domain_purpose(cid, "acme.eu", "branch")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._domains(cid)["acme.eu"], "branch")

    def test_unknown_purpose_is_rejected_with_the_valid_list(self):
        cid = self._company("Acme", "acme.com")
        result = om.set_company_domain_purpose(cid, "acme.com", "sending")
        self.assertEqual(result["status"], "error")
        self.assertIn("email_finding", result["error"])

    def test_setting_primary_moves_the_canonical_column_too(self):
        """'primary' and companies.domain cannot be allowed to disagree — that
        split is what quietly breaks dedup."""
        cid = self._company("Acme", "acme.com", ["acme.eu"])
        om.set_company_domain_purpose(cid, "acme.eu", "primary")
        self.assertEqual(self._company_domain(cid), "acme.eu")

    def test_only_one_domain_is_primary_at_a_time(self):
        cid = self._company("Acme", "acme.com", ["acme.eu"])
        om.set_company_domain_purpose(cid, "acme.com", "primary")
        om.set_company_domain_purpose(cid, "acme.eu", "primary")
        purposes = self._domains(cid)
        self.assertEqual(purposes["acme.eu"], "primary")
        self.assertIsNone(purposes["acme.com"])

    def test_purpose_on_the_legacy_column_creates_the_identity_row(self):
        cid = self._company("Acme", "acme.com")
        conn = om.get_conn()
        try:
            conn.execute(
                "DELETE FROM company_identities WHERE company_id = ? AND identity_type = 'domain'",
                (cid,))
            conn.commit()
        finally:
            conn.close()
        om.set_company_domain_purpose(cid, "acme.com", "email_finding")
        self.assertEqual(self._domains(cid)["acme.com"], "email_finding")

    # -- detach --------------------------------------------------------------

    def test_detach_removes_the_identity(self):
        cid = self._company("Acme", "acme.com", ["notreally.com"])
        result = om.detach_company_domain(cid, "notreally.com")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("notreally.com", self._domains(cid))

    def test_detaching_the_primary_promotes_the_next_best(self):
        """Leaving a company with an alias set but no canonical identity blocks
        email finding and reads as 'missing domain' on the companies page."""
        cid = self._company("Acme", "acme.com", ["acme.eu"])
        om.set_company_domain_purpose(cid, "acme.eu", "email_finding")
        result = om.detach_company_domain(cid, "acme.com")
        self.assertEqual(result["promoted_primary"], "acme.eu")
        self.assertEqual(self._company_domain(cid), "acme.eu")
        self.assertEqual(self._domains(cid)["acme.eu"], "primary")

    def test_detaching_the_only_domain_leaves_it_null_not_stale(self):
        cid = self._company("Acme", "acme.com")
        om.detach_company_domain(cid, "acme.com")
        self.assertIsNone(self._company_domain(cid))

    def test_detaching_an_unknown_domain_errors(self):
        cid = self._company("Acme", "acme.com")
        self.assertEqual(
            om.detach_company_domain(cid, "elsewhere.com")["status"], "error")

    # -- split ---------------------------------------------------------------

    def test_split_moves_the_domain_and_its_leads(self):
        cid = self._company("Acme", "acme.com", ["spinoff.com"])
        stays = om.add_lead(name="Jane", email="jane@acme.com", company="Acme")["id"]
        moves = om.add_lead(name="Bob", email="bob@spinoff.com", company="Acme")["id"]
        conn = om.get_conn()
        try:
            conn.execute("UPDATE leads SET company_id = ? WHERE id IN (?, ?)",
                         (cid, stays, moves))
            conn.commit()
        finally:
            conn.close()

        result = om.split_company_domain(cid, "spinoff.com", "Spinoff Ltd")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["leads_moved"], 1)
        target = result["into_company_id"]
        self.assertNotEqual(target, cid)
        self.assertNotIn("spinoff.com", self._domains(cid))
        self.assertEqual(self._domains(target)["spinoff.com"], "primary")

        conn = om.get_conn()
        try:
            self.assertEqual(
                conn.execute("SELECT company_id FROM leads WHERE id = ?", (moves,))
                .fetchone()["company_id"], target)
            self.assertEqual(
                conn.execute("SELECT company_id FROM leads WHERE id = ?", (stays,))
                .fetchone()["company_id"], cid)
        finally:
            conn.close()

    def test_split_queues_a_reverse_merge_candidate(self):
        """A split made in error has to be visible and undoable — an
        unreviewable destructive edit is how you stop trusting the tool."""
        cid = self._company("Acme", "acme.com", ["spinoff.com"])
        result = om.split_company_domain(cid, "spinoff.com", "Spinoff Ltd")
        conn = om.get_conn()
        try:
            row = conn.execute(
                """SELECT existing_company_id, candidate_company_id, reason, status
                     FROM company_merge_candidates WHERE status = 'pending'""").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["existing_company_id"], cid)
        self.assertEqual(row["candidate_company_id"], result["into_company_id"])
        self.assertIn("split", row["reason"])

    def test_split_dry_run_reports_without_moving(self):
        cid = self._company("Acme", "acme.com", ["spinoff.com"])
        lead_id = om.add_lead(name="Bob", email="bob@spinoff.com", company="Acme")["id"]
        conn = om.get_conn()
        try:
            conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead_id))
            conn.commit()
        finally:
            conn.close()
        result = om.split_company_domain(cid, "spinoff.com", "Spinoff Ltd", dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["leads_to_move"], 1)
        self.assertIn("spinoff.com", self._domains(cid))

    def test_splitting_the_primary_clears_the_canonical_column(self):
        cid = self._company("Acme", "acme.com")
        om.split_company_domain(cid, "acme.com", "Really Other Co")
        self.assertIsNone(self._company_domain(cid))

    def test_split_requires_a_target_name(self):
        cid = self._company("Acme", "acme.com", ["spinoff.com"])
        self.assertEqual(
            om.split_company_domain(cid, "spinoff.com", "")["status"], "error")

    def test_split_of_an_unknown_domain_errors(self):
        cid = self._company("Acme", "acme.com")
        self.assertEqual(
            om.split_company_domain(cid, "elsewhere.com", "Other")["status"], "error")

    def test_split_onto_the_same_company_is_refused(self):
        cid = self._company("Acme", "acme.com", ["acme.eu"])
        self.assertEqual(
            om.split_company_domain(cid, "acme.eu", "Acme")["status"], "error")


if __name__ == "__main__":
    unittest.main()
