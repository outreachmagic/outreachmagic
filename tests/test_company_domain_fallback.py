"""Tests for company_domain fallback from leads.email_domain."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
LE_SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(LE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LE_SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402
import enrich  # noqa: E402


class CompanyDomainFallbackTests(unittest.TestCase):
    def setUp(self):
        self._prev_data_override = om_paths._DATA_ROOT_OVERRIDE
        self._prev_project_override = om_paths._PROJECT_ROOT_OVERRIDE
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        om_paths.set_data_root_override(self._root)
        om_paths.set_working_root_override(self._root / "workspace")
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        self.ws = om.create_workspace("Domain Fallback", slug="domain-fallback")
        self.ws_id = f"ws_{self.ws['slug']}"

    def tearDown(self):
        om_paths.set_data_root_override(self._prev_data_override)
        om_paths.set_working_root_override(self._prev_project_override)
        self._tmp.cleanup()

    def _add_lead_with_null_company_domain(
        self,
        *,
        email: str,
        company: str,
        name: str,
        linkedin: str,
        tag: str = "domain-test",
    ) -> int:
        result = om.resolve_lead(
            email=email,
            name=name,
            company=company,
            linkedin_url=linkedin,
        )
        lead_id = int(result["id"])
        conn = om.get_conn()
        conn.execute(
            "UPDATE companies SET domain = NULL WHERE id = (SELECT company_id FROM leads WHERE id = ?)",
            (lead_id,),
        )
        conn.commit()
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, self.ws_id, lead_id)
        conn.commit()
        conn.close()
        om.tag_add(self.ws_id, lead_id, tag)
        return lead_id

    def test_export_company_domain_fallback_from_email_domain(self):
        self._add_lead_with_null_company_domain(
            email="teresa.stock@purdueglobal.edu",
            name="Teresa Stock",
            company="Purdue University Global",
            linkedin="https://www.linkedin.com/in/teresa-stock",
        )
        result = om.export_leads(
            workspace="domain-fallback",
            tag="domain-test",
            fmt="json",
            limit=10,
        )
        leads = result["leads"]
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["company_domain"], "purdueglobal.edu")

    def test_export_gmail_company_domain_blank(self):
        self._add_lead_with_null_company_domain(
            email="mike.test@gmail.com",
            name="Mike Test",
            company="StartupCo",
            linkedin="https://www.linkedin.com/in/mike-test-gmail",
        )
        result = om.export_leads(
            workspace="domain-fallback",
            tag="domain-test",
            fmt="json",
            limit=10,
        )
        leads = result["leads"]
        self.assertEqual(len(leads), 1)
        self.assertIn(leads[0]["company_domain"], (None, ""))

    def test_link_lead_company_backfills_from_existing_email_domain(self):
        lead_id = self._add_lead_with_null_company_domain(
            email="teresa.stock@purdueglobal.edu",
            name="Teresa Stock",
            company="Purdue University Global",
            linkedin="https://www.linkedin.com/in/teresa-stock",
        )
        om.resolve_lead(
            linkedin_url="https://www.linkedin.com/in/teresa-stock",
            name="Teresa Stock",
            company="Purdue University Global",
        )
        conn = om.get_conn()
        row = conn.execute(
            """SELECT c.domain
               FROM leads l
               LEFT JOIN companies c ON l.company_id = c.id
               WHERE l.id = ?""",
            (lead_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["domain"], "purdueglobal.edu")

    def test_require_domain_includes_email_domain_only(self):
        self._add_lead_with_null_company_domain(
            email="teresa.stock@purdueglobal.edu",
            name="Teresa Stock",
            company="Purdue University Global",
            linkedin="https://www.linkedin.com/in/teresa-stock",
        )
        rows, _ = om.query_leads_for_export(
            workspace="domain-fallback",
            tag="domain-test",
            require_domain=True,
            limit=10,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_domain"], "purdueglobal.edu")

    def test_build_lead_sync_payload_company_domain_fallback(self):
        lead_id = self._add_lead_with_null_company_domain(
            email="teresa.stock@purdueglobal.edu",
            name="Teresa Stock",
            company="Purdue University Global",
            linkedin="https://www.linkedin.com/in/teresa-stock",
        )
        conn = om.get_conn()
        payload = om.build_lead_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id)
        conn.close()
        self.assertEqual(payload.get("company_domain"), "purdueglobal.edu")

    def test_map_to_outreachmagic_email_domain_fallback(self):
        person = {
            "full_name": "Teresa Stock",
            "company_name": "Purdue University Global",
            "email": "teresa.stock@purdueglobal.edu",
        }
        enrichment = {
            "company_website": "",
            "linkedin_url": "https://www.linkedin.com/in/teresa-stock",
            "confidence": "low",
        }
        mapped = enrich.map_to_outreachmagic(person, enrichment)
        self.assertEqual(mapped["profile"]["company_domain"], "purdueglobal.edu")

    def test_map_to_outreachmagic_skips_gmail_fallback(self):
        person = {
            "full_name": "Mike Test",
            "company_name": "StartupCo",
            "email": "mike.test@gmail.com",
        }
        enrichment = {"linkedin_url": "https://www.linkedin.com/in/mike-test-gmail"}
        mapped = enrich.map_to_outreachmagic(person, enrichment)
        self.assertNotIn("company_domain", mapped["profile"])


if __name__ == "__main__":
    unittest.main()
