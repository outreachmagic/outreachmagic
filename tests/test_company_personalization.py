#!/usr/bin/env python3
"""Company-wide personalization tests."""

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


class CompanyPersonalizationTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def test_merge_lead_overrides_company(self):
        lead = om.add_lead(name="Jane", email="j@acme.com", company="Acme Corporation Inc")
        lid = lead["id"]
        conn = om.get_conn()
        cid = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lid,)).fetchone()["company_id"]
        conn.close()
        om.company_personalize_set("company_name", "Acme", company_id=cid)
        om.company_personalize_set("company_icebreaker", "Love your product", company_id=cid)
        om.personalize_set(lid, "first_name", "Jane")
        merged = om.resolve_personalization(lid)
        self.assertEqual(merged["company_name"], "Acme")
        self.assertEqual(merged["company_icebreaker"], "Love your product")
        self.assertEqual(merged["first_name"], "Jane")

    def test_split_sync_payloads(self):
        lead = om.add_lead(name="Bob", email="bob@acme.com", company="Acme Corp")
        lid = lead["id"]
        conn = om.get_conn()
        cid = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lid,)).fetchone()["company_id"]
        conn.close()
        om.personalize_set(lid, "first_name", "Bob")
        om.company_personalize_set("company_name", "Acme", company_id=cid)
        conn = om.get_conn()
        lead_payload = om.build_lead_sync_payload(conn, om.DEFAULT_ORG_ID, lid)
        co_payload = om.build_company_sync_payload(conn, cid)
        conn.close()
        self.assertIn("first_name", lead_payload["personalization"])
        self.assertNotIn("company_name", lead_payload.get("personalization", {}))
        self.assertEqual(co_payload["personalization"]["company_name"], "Acme")

    def test_company_update_ingest(self):
        conn = om.get_conn()
        cid = om.ensure_company(conn, name="SyncCo", domain="syncco.com")
        conn.commit()
        conn.close()
        event = {
            "platform": "agent",
            "action": "company_update",
            "client_id": "remote-client",
            "entity_key": "company:domain:syncco.com",
            "timestamp": "2026-05-31T12:00:00Z",
            "payload": {
                "personalization": {"company_icebreaker": "Great team"},
                "personalization_at": "2026-05-31T12:00:01Z",
            },
        }
        om.ingest_relay_event(event, quiet=True)
        got = om.company_personalize_get(domain="syncco.com")
        self.assertEqual(got["company_icebreaker"], "Great team")


if __name__ == "__main__":
    unittest.main()
