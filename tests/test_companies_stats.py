#!/usr/bin/env python3
"""Companies page: derived tags (D2) and coverage stats (D6).

Contacts stats measure reachability per person. Companies need coverage and
penetration instead — how many accounts you can reach at all, how deep you are
inside each, which are blocked on missing data.

Company tags are derived, never stored: a company carries tag T if any of its
leads in the workspace carries T. A company_tags table would need a sync
surface and would drift from the lead tags that are the actual source of truth.
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


class CompaniesStatsTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        self.org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
            ("ws-a", self.org_id, "wsa", "WS A"))
        conn.commit()
        conn.close()
        self.ws = "ws-a"

    def _lead(self, *, tags=None, **kw):
        lead_id = om.add_lead(**kw)["id"]
        conn = om.get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id) "
                "VALUES (?,?,?,?)", (f"wl-{lead_id}", self.org_id, self.ws, lead_id))
            for tag in tags or []:
                conn.execute(
                    "INSERT OR IGNORE INTO workspace_lead_tags (workspace_id, lead_id, tag) "
                    "VALUES (?,?,?)", (self.ws, lead_id, tag))
            conn.commit()
        finally:
            conn.close()
        return lead_id

    def _stats(self):
        conn = om.get_conn()
        try:
            return dq.companies_stats(conn, self.ws)
        finally:
            conn.close()

    def _search(self, **kw):
        conn = om.get_conn()
        try:
            return dq.search_companies(conn, self.ws, **kw)["companies"]
        finally:
            conn.close()

    # -- derived tags (D2) ---------------------------------------------------

    def test_a_company_carries_its_contacts_tags(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp", tags=["nace"])
        rows = self._search()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], ["nace"])

    def test_tag_filter_matches_a_company_via_any_of_its_leads(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp", tags=["nace"])
        self._lead(name="Bob", email="bob@acme.com", company="Acme Corp")
        self._lead(name="Kim", email="kim@other.com", company="Other Ltd")
        rows = self._search(tag="nace")
        self.assertEqual([r["name"] for r in rows], ["Acme Corp"])

    def test_tagging_a_placeholder_tags_the_company(self):
        """Company-level-only tags still work — you tag the placeholder."""
        stub = self._lead(name="Acme Corp", company="Acme Corp", tags=["target-list"])
        om.set_lead_record_type(stub, "company_placeholder")
        self.assertEqual([r["name"] for r in self._search(tag="target-list")], ["Acme Corp"])

    def test_an_untagged_company_is_not_matched(self):
        self._lead(name="Kim", email="kim@other.com", company="Other Ltd")
        self.assertEqual(self._search(tag="nace"), [])

    # -- coverage stats (D6) -------------------------------------------------

    def test_counts_companies_not_lead_rows(self):
        """Aggregating over the lead join would weight every tile by how many
        contacts a company happens to have."""
        for n in range(3):
            self._lead(name=f"P{n}", email=f"p{n}@acme.com", company="Acme Corp")
        self._lead(name="Kim", email="kim@other.com", company="Other Ltd")
        o = self._stats()["overall"]
        self.assertEqual(o["companies"], 2)
        self.assertEqual(o["contact_rows"], 4)
        self.assertEqual(o["avg_contacts_per_company"], 2.0)

    def test_reachable_versus_the_finder_work_queue(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp")
        self._lead(name="Kim", company="Other Ltd")   # no email anywhere
        o = self._stats()["overall"]
        self.assertEqual(o["with_reachable_contact"], 1)
        self.assertEqual(o["no_reachable_contact"], 1)

    def test_no_reachable_contact_tile_click_lands_on_what_it_counted(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp")
        self._lead(name="Kim", company="Other Ltd")
        rows = self._search(no_reachable_contact=True)
        self.assertEqual([r["name"] for r in rows], ["Other Ltd"])
        self.assertEqual(len(rows), self._stats()["overall"]["no_reachable_contact"])

    def test_an_invalid_email_is_not_reachable(self):
        lead_id = self._lead(name="Jane", email="jane@acme.com", company="Acme Corp")
        conn = om.get_conn()
        try:
            conn.execute(
                "UPDATE leads SET email_verification_status = 'invalid' WHERE id = ?", (lead_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._stats()["overall"]["no_reachable_contact"], 1)

    def test_placeholder_only_companies(self):
        """Never enriched to a real person — the D1 population."""
        stub = self._lead(name="Acme Corp", company="Acme Corp")
        om.set_lead_record_type(stub, "company_placeholder")
        self._lead(name="Kim", email="kim@other.com", company="Other Ltd")
        o = self._stats()["overall"]
        self.assertEqual(o["placeholder_only"], 1)
        self.assertEqual([r["name"] for r in self._search(placeholder_only=True)], ["Acme Corp"])

    def test_missing_domain_is_the_email_finding_blocker(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp")
        self._lead(name="Kim", company="Other Ltd")
        o = self._stats()["overall"]
        self.assertEqual(o["missing_domain"], 1)
        self.assertEqual([r["name"] for r in self._search(missing_domain=True)], ["Other Ltd"])

    def test_by_tag_uses_the_same_derived_rule(self):
        self._lead(name="Jane", email="jane@acme.com", company="Acme Corp", tags=["nace"])
        self._lead(name="Kim", company="Other Ltd", tags=["nace"])
        self._lead(name="Sam", email="sam@third.com", company="Third Inc")
        by_tag = {t["tag"]: t for t in self._stats()["by_tag"]}
        self.assertEqual(by_tag["nace"]["companies"], 2)
        self.assertEqual(by_tag["nace"]["no_reachable_contact"], 1)
        self.assertNotIn("Third Inc", [t["tag"] for t in self._stats()["by_tag"]])

    def test_empty_workspace_does_not_divide_by_zero(self):
        o = self._stats()["overall"]
        self.assertEqual(o["companies"], 0)
        self.assertEqual(o["avg_contacts_per_company"], 0.0)


if __name__ == "__main__":
    unittest.main()
