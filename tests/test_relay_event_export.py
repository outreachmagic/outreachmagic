#!/usr/bin/env python3
"""Tests for event_log relay export including campaign names."""

import json
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
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


class RelayEventExportTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def test_export_event_log_includes_campaign(self):
        result = om.resolve_lead(
            email="export-camp@example.com",
            name="Export Camp",
            company="Acme",
            source="csv",
        )
        lead_id = result["id"]
        om.log_event(
            lead_id,
            "email_sent",
            metadata={"source": "csv"},
            campaign="popcam | test campaign",
        )
        conn = om.get_conn()
        conn.execute("DELETE FROM relay_ingested WHERE dedupe_key LIKE 'event:%'")
        conn.commit()
        conn.close()

        export = om.export_local_changes(events_only=True)
        logs = [e for e in export["entries"] if e.get("action") == "event_log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["payload"].get("campaign"), "popcam | test campaign")
        self.assertIsNotNone(logs[0].get("event_id"))

    def test_ingest_event_log_restores_campaign(self):
        result = om.resolve_lead(
            email="ingest-camp@example.com",
            name="Ingest Camp",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        from workspace_routing import lead_entity_key

        entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, lead_id)
        conn.close()
        om.ingest_agent_entry(
            {
                "platform": "agent",
                "action": "event_log",
                "client_id": "other-device",
                "entity_key": entity_key,
                "timestamp": "2026-06-01T12:00:00Z",
                "workspace": "default",
                "payload": {
                    "event_type": "email_reply",
                    "direction": "inbound",
                    "channel": "email",
                    "campaign": "popcam | career services",
                },
            }
        )
        conn = om.get_conn()
        row = conn.execute(
            """SELECT campaign_id, metadata_json FROM events
               WHERE lead_id = ? AND json_extract(metadata_json, '$.source') = 'agent_sync'""",
            (lead_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta.get("campaign"), "popcam | career services")
        self.assertIsNotNone(row["campaign_id"])


if __name__ == "__main__":
    unittest.main()
