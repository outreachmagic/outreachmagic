#!/usr/bin/env python3
"""Tests for relay vs local cloud_pending and sync defaults."""

import os
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


class CloudPendingLogicTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def test_lead_should_cloud_pending_relay_sources(self):
        self.assertFalse(om._lead_should_cloud_pending("relay_sync", "smartlead"))
        self.assertFalse(om._lead_should_cloud_pending("agent_sync", "relay"))
        self.assertTrue(om._lead_should_cloud_pending("csv", "csv"))
        self.assertTrue(om._lead_should_cloud_pending(None, None))

    def test_resolve_lead_relay_sync_does_not_mark_pending(self):
        result = om.resolve_lead(
            email="relay@example.com",
            name="Relay User",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        self.assertEqual(result["status"], "created")
        conn = om.get_conn()
        row = conn.execute(
            "SELECT cloud_pending FROM leads WHERE id = ?", (result["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(row["cloud_pending"], 0)

    def test_resolve_lead_csv_marks_pending(self):
        result = om.resolve_lead(
            email="csv@example.com",
            name="CSV User",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        conn = om.get_conn()
        row = conn.execute(
            "SELECT cloud_pending FROM leads WHERE id = ?", (result["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(row["cloud_pending"], 1)

    def test_enrich_lead_local_marks_pending(self):
        result = om.resolve_lead(
            email="enrich@example.com",
            name="Enrich User",
            company="Acme",
            source="relay_sync",
            source_platform="instantly",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        conn.execute("UPDATE leads SET cloud_pending = 0, title = NULL WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()

        om.enrich_lead(lead_id, title="VP Sales", overwrite=True)
        conn = om.get_conn()
        row = conn.execute("SELECT cloud_pending, title FROM leads WHERE id = ?", (lead_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "VP Sales")
        self.assertEqual(row["cloud_pending"], 1)

    def test_enrich_lead_relay_ingest_does_not_mark_pending(self):
        result = om.resolve_lead(
            email="pull@example.com",
            name="Pull User",
            company="Acme",
            source="relay_sync",
            source_platform="emailbison",
        )
        lead_id = result["id"]
        om.enrich_lead(lead_id, title="From Relay", overwrite=True, mark_cloud_pending=False)
        conn = om.get_conn()
        row = conn.execute("SELECT cloud_pending, title FROM leads WHERE id = ?", (lead_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "From Relay")
        self.assertEqual(row["cloud_pending"], 0)

    def test_personalize_set_marks_lead_pending(self):
        result = om.resolve_lead(
            email="pers@example.com",
            name="Pers User",
            company="Acme",
            source="relay_sync",
            source_platform="heyreach",
        )
        lead_id = result["id"]
        om.personalize_set(lead_id, "company_name", "Acme Corp")
        conn = om.get_conn()
        row = conn.execute("SELECT cloud_pending FROM leads WHERE id = ?", (lead_id,)).fetchone()
        conn.close()
        self.assertEqual(row["cloud_pending"], 1)

    def test_migrate_clears_false_relay_backlog(self):
        conn = om.get_conn()
        conn.execute(
            """INSERT INTO leads (name, email, cloud_pending, original_source, original_source_platform)
               VALUES ('Stale', 'stale@example.com', 1, 'relay_sync', 'smartlead')"""
        )
        conn.commit()
        conn.close()
        om.migrate_db()
        conn = om.get_conn()
        row = conn.execute(
            "SELECT cloud_pending FROM leads WHERE email = 'stale@example.com'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["cloud_pending"], 0)

    def test_relay_push_defaults(self):
        for key in (
            "OUTREACHMAGIC_SYNC_BATCH_SIZE",
            "OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS",
        ):
            os.environ.pop(key, None)
        settings = om.get_relay_push_settings()
        self.assertEqual(settings["batch_size"], 50)
        self.assertEqual(settings["timeout_seconds"], 120)


if __name__ == "__main__":
    unittest.main()
