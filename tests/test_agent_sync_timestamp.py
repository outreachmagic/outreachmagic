#!/usr/bin/env python3
"""Regression tests for agent_sync event_log timestamp, sender, and workspace events."""

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


class AgentSyncTimestampTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        om.set_workspace_routing("single")

    def _ingest_event_log(self, *, entity_key: str, timestamp: str, **payload_extra):
        data = {
            "event_type": "email_sent",
            "direction": "outbound",
            "channel": "email",
            "campaign": "popcam | test",
            **payload_extra,
        }
        return om.ingest_agent_entry(
            {
                "platform": "agent",
                "entity_key": entity_key,
                "event_type": "event_log",
                "received_at": timestamp,
                "relay_id": 99001,
                "payload": {
                    "action": "event_log",
                    "client_id": "remote-agent-client",
                    "workspace": "default",
                    "timestamp": timestamp,
                    "data": data,
                },
            },
            quiet=True,
        )

    def test_ingest_preserves_event_timestamp_not_import_time(self):
        om.resolve_lead(
            email="ts-preserve@example.com",
            name="TS Preserve",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        lead_id = self._ingest_event_log(
            entity_key="ts-preserve@example.com",
            timestamp="2024-03-15T14:22:11.000000+00:00",
        )
        self.assertIsNotNone(lead_id)
        conn = om.get_conn()
        row = conn.execute(
            """SELECT created_at, sender, metadata_json FROM events
               WHERE lead_id = ? AND json_extract(metadata_json, '$.source') = 'agent_sync'""",
            (lead_id,),
        ).fetchone()
        lead = conn.execute(
            "SELECT last_contact_at FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        wle = conn.execute(
            """SELECT event_at FROM workspace_lead_events
               WHERE lead_id = ? AND lower(event_type) = 'email_sent'""",
            (lead_id,),
        ).fetchone()
        conn.close()
        self.assertTrue(str(row["created_at"]).startswith("2024-03-15"))
        self.assertTrue(str(lead["last_contact_at"]).startswith("2024-03-15"))
        self.assertTrue(str(wle["event_at"]).startswith("2024-03-15"))
        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta.get("relay_id"), 99001)

    def test_ingest_restores_sender(self):
        om.resolve_lead(
            email="sender-replay@example.com",
            name="Sender Replay",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        lead_id = self._ingest_event_log(
            entity_key="sender-replay@example.com",
            timestamp="2026-05-20T10:00:00Z",
            sender="outreach@popcam.com",
        )
        conn = om.get_conn()
        row = conn.execute(
            "SELECT sender FROM events WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row["sender"], "outreach@popcam.com")

    def test_export_includes_sender_in_payload(self):
        result = om.resolve_lead(
            email="export-sender@example.com",
            name="Export Sender",
            company="Acme",
            source="csv",
        )
        lead_id = result["id"]
        om.log_event(
            lead_id,
            "email_sent",
            metadata={"source": "csv"},
            sender="mailbox@example.com",
            event_at="2026-04-01T12:00:00Z",
        )
        conn = om.get_conn()
        conn.execute("DELETE FROM relay_ingested WHERE dedupe_key LIKE 'event:%'")
        conn.commit()
        conn.close()

        export = om.export_local_changes(events_only=True)
        logs = [e for e in export["entries"] if e.get("action") == "event_log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["payload"].get("sender"), "mailbox@example.com")
        self.assertTrue(logs[0]["timestamp"].startswith("2026-04-01"))

    def test_workspace_event_idempotent_on_replay(self):
        om.resolve_lead(
            email="idem@example.com",
            name="Idem",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        event = {
            "platform": "agent",
            "entity_key": "idem@example.com",
            "event_type": "event_log",
            "received_at": "2026-02-10T08:00:00Z",
            "relay_id": 99002,
            "payload": {
                "action": "event_log",
                "client_id": "remote-agent-client",
                "workspace": "default",
                "timestamp": "2026-02-10T08:00:00Z",
                "data": {
                    "event_type": "email_sent",
                    "direction": "outbound",
                    "channel": "email",
                },
            },
        }
        first = om.ingest_agent_entry(event, quiet=True)
        second = om.ingest_agent_entry(event, quiet=True)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        conn = om.get_conn()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_lead_events WHERE lead_id = ?",
            (first,),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(int(count), 1)

    def test_full_pull_replay_timestamps_span_event_day(self):
        entity_key = "full-ts@example.com"
        core_event = {
            "platform": "agent",
            "relay_id": 50_101,
            "entity_key": entity_key,
            "event_type": "lead_core_update",
            "received_at": "2026-06-01T10:00:00Z",
            "payload": {
                "action": "lead_core_update",
                "client_id": "upstream-client",
                "timestamp": "2026-06-01T10:00:00Z",
                "data": {"email": entity_key, "name": "Full TS", "company": "Acme"},
            },
        }
        log_event = {
            "platform": "agent",
            "relay_id": 50_102,
            "entity_key": entity_key,
            "event_type": "event_log",
            "received_at": "2026-03-17T11:30:00Z",
            "payload": {
                "action": "event_log",
                "client_id": "upstream-client",
                "workspace": "default",
                "timestamp": "2026-03-17T11:30:00Z",
                "data": {
                    "event_type": "email_sent",
                    "direction": "outbound",
                    "channel": "email",
                    "campaign": "popcam | headshot lounge",
                    "sender": "jackson@popcam.com",
                },
            },
        }

        def fake_pull(*_args, **kwargs):
            if kwargs.get("snapshots_only"):
                return {"events": [core_event], "max_snapshot_id": 1, "has_more_snapshots": False}
            return {"events": [log_event], "max_id": 50_102, "has_more_events": False}

        om.init_db()
        om.set_workspace_routing("single")
        conn = om.get_conn()
        config = om.get_org_routing_config(conn, DEFAULT_ORG_ID)
        ws_map = om._pull_workspace_slug_map(conn, DEFAULT_ORG_ID)
        conn.close()

        lead_id = om.ingest_agent_entry(
            log_event,
            routing_config=config,
            ws_slug_map=ws_map,
            quiet=True,
        )
        self.assertIsNotNone(lead_id)
        conn = om.get_conn()
        ev = conn.execute(
            """SELECT created_at, sender FROM events
               WHERE json_extract(metadata_json, '$.source') = 'agent_sync'"""
        ).fetchone()
        wle = conn.execute(
            "SELECT event_at FROM workspace_lead_events WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        conn.close()
        self.assertTrue(str(ev["created_at"]).startswith("2026-03-17"))
        self.assertEqual(ev["sender"], "jackson@popcam.com")
        self.assertTrue(str(wle["event_at"]).startswith("2026-03-17"))


if __name__ == "__main__":
    unittest.main()
