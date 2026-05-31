"""Tests for sender storage, enrichment, export, and project paths."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402


class ExportEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self._prev_data_override = om_paths._DATA_ROOT_OVERRIDE
        self._prev_project_override = om_paths._PROJECT_ROOT_OVERRIDE
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        om_paths.set_data_root_override(self._root)
        om_paths.set_project_root_override(self._root / "project")
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.ensure_project_layout()

    def tearDown(self):
        om_paths.set_data_root_override(self._prev_data_override)
        om_paths.set_project_root_override(self._prev_project_override)
        self._tmp.cleanup()

    def test_ensure_project_layout(self):
        root = om.ensure_project_layout()
        self.assertTrue((root / "input").is_dir())
        self.assertTrue((root / "export").is_dir())
        self.assertTrue((root / "agent_resources").is_dir())

    def test_resolve_project_path_input(self):
        csv_path = om_paths.get_input_dir() / "leads.csv"
        csv_path.write_text("email,name\ntest@x.com,Test\n", encoding="utf-8")
        resolved = om_paths.resolve_project_path("leads.csv", kind="input")
        self.assertEqual(resolved, csv_path)

    def test_build_lead_sync_payload_linkedin(self):
        conn = om.get_conn()
        lead_id = om.add_lead(
            name="Jane", email="jane@test.com",
            linkedin_url="https://www.linkedin.com/in/jane-doe",
        )["id"]
        row = conn.execute(
            "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        self.assertEqual(row["linkedin_url"], "linkedin.com/in/jane-doe")
        payload = om.build_lead_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id)
        conn.close()
        self.assertEqual(payload.get("linkedin"), row["linkedin_url"])
        self.assertNotIn("https://", payload.get("linkedin", ""))

    def test_ingest_sender_and_webhook_event(self):
        ws = om.create_workspace("Acme Corp", slug="acme_corp")
        om.add_campaign_map_cli("prosp", "acme_corp", campaign_name="acme_corp test", match_strategy="name_exact")
        event = {
            "platform": "prosp",
            "event_type": "send_connection",
            "sender": "https://www.linkedin.com/in/sender-one",
            "lead": "linkedin.com/in/lead-one",
            "received_at": "2026-05-27T12:00:00Z",
            "relay_id": 9001,
            "raw": {
                "eventType": "send_connection",
                "eventData": {
                    "campaignName": "acme_corp test",
                    "lead": "https://www.linkedin.com/in/lead-one",
                    "sender": "https://www.linkedin.com/in/sender-one",
                },
            },
        }
        lead_id = om.ingest_relay_event(event, force_workspace_id=ws["id"], quiet=True)
        self.assertIsNotNone(lead_id)
        conn = om.get_conn()
        ev = conn.execute(
            "SELECT event_type, sender, metadata_json FROM events WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        lead = conn.execute(
            "SELECT latest_sender, latest_sender_platform FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(ev["event_type"], "linkedin_connect")
        meta = json.loads(ev["metadata_json"])
        self.assertEqual(meta.get("webhook_event"), "send_connection")
        self.assertEqual(ev["sender"], "linkedin.com/in/sender-one")
        self.assertEqual(lead["latest_sender"], "linkedin.com/in/sender-one")
        self.assertEqual(lead["latest_sender_platform"], "prosp")

    def test_mailmerge_import_and_enrich(self):
        rows = [{
            "email": "import@test.com",
            "name": "Import Test",
            "company": "ImpCo Inc",
            "mailmerge_first_name": "Imp",
            "mailmerge_company_name": "ImpCo",
            "mailmerge_custom_line": "Hello",
        }]
        summary = om.import_profiles(rows)
        self.assertEqual(summary["personalized"], 1)
        lead_id = summary["results"][0]["id"]
        conn = om.get_conn()
        lead_pers = {r["field_name"] for r in conn.execute(
            "SELECT field_name FROM lead_personalization WHERE lead_id = ?", (lead_id,),
        ).fetchall()}
        cid = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()["company_id"]
        co_pers = {r["field_name"] for r in conn.execute(
            "SELECT field_name FROM company_personalization WHERE company_id = ?", (cid,),
        ).fetchall()}
        conn.close()
        self.assertIn("first_name", lead_pers)
        self.assertIn("custom_line", lead_pers)
        self.assertIn("company_name", co_pers)
        enriched = om.enrich_lead_rows(
            [{"id": lead_id, "name": "Import Test", "email": "import@test.com", "stage": "prospecting"}],
        )
        self.assertEqual(enriched[0]["personalization"]["first_name"], "Imp")
        self.assertEqual(enriched[0]["personalization"]["company_name"], "ImpCo")

    def test_export_csv_personalized_columns(self):
        om.create_workspace("Export WS", slug="exportws")
        rows = [{"email": "exp@test.com", "name": "Exp", "mailmerge_first_name": "Exp"}]
        om.import_profiles(rows, workspace="exportws")
        result = om.export_leads(workspace="exportws", fmt="csv", limit=100)
        self.assertEqual(result["count"], 1)
        content = Path(result["file"]).read_text(encoding="utf-8")
        self.assertIn("personalized_first_name", content)
        self.assertIn("latest_sender", content.splitlines()[0])

    def test_add_lead_name_company_matches_existing_without_duplicate(self):
        first = om.add_lead(name="Jane Doe", company="Acme Corp")
        second = om.add_lead(name="Jane Doe", company="Acme Corp", title="VP Marketing")
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(first["id"], second["id"])

        conn = om.get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS c, title FROM leads WHERE LOWER(name)=LOWER(?) AND LOWER(company)=LOWER(?)",
            ("Jane Doe", "Acme Corp"),
        ).fetchone()
        conn.close()
        self.assertEqual(row["c"], 1)
        self.assertEqual(row["title"], "VP Marketing")

    def test_add_lead_name_company_match_is_case_insensitive(self):
        first = om.add_lead(name="Jane Doe", company="Acme Corp")
        second = om.add_lead(name="jane doe", company="ACME CORP", industry="Martech")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "exists")

        conn = om.get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS c, industry FROM leads WHERE id = ?",
            (first["id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(row["c"], 1)
        self.assertEqual(row["industry"], "Martech")


if __name__ == "__main__":
    unittest.main()
