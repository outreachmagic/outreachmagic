#!/usr/bin/env python3
"""Tests for cross-platform lead activity sync (last contacted, counts, status)."""

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
from workspace_routing import DEFAULT_ORG_ID, ensure_organization  # noqa: E402


class ActivitySyncTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        conn = om.get_conn()
        ensure_organization(conn)
        conn.close()

    def _workspace_id(self, slug: str = "default") -> str:
        conn = om.get_conn()
        row = om.resolve_workspace_identity(conn, slug)
        conn.close()
        self.assertIsNotNone(row)
        return row["id"]

    def test_merge_activity_summary_max_counts_and_latest_date(self):
        merged = om.merge_activity_summary(
            {
                "email_sent_count": 2,
                "linkedin_sent_count": 0,
                "total_replies_count": 0,
                "last_contacted_at": "2026-01-01T00:00:00",
            },
            {
                "email_sent_count": 1,
                "linkedin_sent_count": 3,
                "total_replies_count": 1,
                "last_contacted_at": "2026-02-01T00:00:00",
            },
        )
        self.assertEqual(merged["email_sent_count"], 2)
        self.assertEqual(merged["linkedin_sent_count"], 3)
        self.assertEqual(merged["total_replies_count"], 1)
        self.assertEqual(merged["total_contacted_count"], 5)
        self.assertEqual(merged["last_contacted_at"], "2026-02-01T00:00:00")

    def test_activity_in_sync_payload_and_roundtrip(self):
        ws_id = self._workspace_id()
        result = om.resolve_lead(
            email="activity@example.com",
            name="Activity Lead",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        om.upsert_workspace_lead(
            conn, DEFAULT_ORG_ID, ws_id, lead_id,
            status="contacted",
            current_status_label="information request",
        )
        conn.commit()
        conn.close()

        summary = om.set_lead_activity_summary(
            lead_id,
            ws_id,
            last_contacted_at="2026-02-23T21:35:48.310000",
            email_sent_count=2,
            linkedin_sent_count=1,
            total_replies_count=0,
            merge=False,
            mark_cloud_pending=False,
        )
        self.assertEqual(summary["total_contacted_count"], 3)

        conn = om.get_conn()
        payload = om.build_lead_sync_payload(conn, DEFAULT_ORG_ID, lead_id, workspace_slug="default")
        conn.close()
        self.assertEqual(payload.get("lead_status"), "information request")
        activity = payload.get("activity") or {}
        self.assertEqual(activity.get("email_sent_count"), 2)
        self.assertEqual(activity.get("linkedin_sent_count"), 1)
        self.assertEqual(activity.get("total_contacted_count"), 3)
        self.assertEqual(activity.get("last_contacted_at"), "2026-02-23T21:35:48.310000")

        om.init_db()
        replay_payload = dict(payload)
        replay_payload["email"] = "activity@example.com"
        event = {
            "platform": "agent",
            "action": "lead_update",
            "entity_key": "activity@example.com",
            "client_id": "other-client-activity",
            "timestamp": "2026-05-27T12:00:00Z",
            "workspace": "default",
            "payload": replay_payload,
        }
        replay_id = om.ingest_agent_entry(event)
        self.assertIsNotNone(replay_id)

        conn = om.get_conn()
        wl = conn.execute(
            """SELECT email_sent_count, linkedin_sent_count, total_replies_count, last_activity_at,
                      current_status_label
               FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, replay_id),
        ).fetchone()
        conn.close()
        self.assertEqual(wl["email_sent_count"], 2)
        self.assertEqual(wl["linkedin_sent_count"], 1)
        self.assertEqual(wl["last_activity_at"], "2026-02-23T21:35:48.310000")
        self.assertEqual(wl["current_status_label"], "information request")

    def test_refresh_activity_from_events(self):
        ws_id = self._workspace_id()
        result = om.resolve_lead(
            email="events@example.com",
            name="Events Lead",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        om.upsert_workspace_lead(conn, DEFAULT_ORG_ID, ws_id, lead_id, status="contacted")
        conn.commit()
        conn.close()

        om.log_event(
            lead_id,
            event_type="email_sent",
            direction="outbound",
            channel="email",
            event_at="2026-03-01T10:00:00",
        )
        om.log_event(
            lead_id,
            event_type="email_sent",
            direction="outbound",
            channel="email",
            event_at="2026-03-02T10:00:00",
        )
        om.log_event(
            lead_id,
            event_type="email_reply",
            direction="inbound",
            channel="email",
            event_at="2026-03-03T10:00:00",
        )

        conn = om.get_conn()
        payload = om.build_lead_sync_payload(conn, DEFAULT_ORG_ID, lead_id, workspace_slug="default")
        conn.close()
        activity = payload.get("activity") or {}
        self.assertEqual(activity.get("email_sent_count"), 2)
        self.assertEqual(activity.get("total_replies_count"), 1)
        self.assertEqual(activity.get("total_contacted_count"), 2)
        self.assertEqual(activity.get("last_contacted_at"), "2026-03-02T10:00:00")


    def test_multi_workspace_prefetch_sync_payloads(self):
        om.create_workspace("Team B", slug="team-b")
        ws_default = self._workspace_id("default")
        ws_b = self._workspace_id("team-b")
        result = om.resolve_lead(
            email="multi-ws@example.com",
            name="Multi WS Lead",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        om.upsert_workspace_lead(
            conn, DEFAULT_ORG_ID, ws_default, lead_id,
            status="contacted",
            current_status_label="warm",
        )
        om.upsert_workspace_lead(
            conn, DEFAULT_ORG_ID, ws_b, lead_id,
            status="prospecting",
            current_status_label="cold",
        )
        conn.commit()
        conn.close()

        om.set_lead_activity_summary(
            lead_id, ws_default,
            email_sent_count=5,
            merge=False,
            mark_cloud_pending=False,
        )
        om.set_lead_activity_summary(
            lead_id, ws_b,
            email_sent_count=2,
            merge=False,
            mark_cloud_pending=False,
        )

        conn = om.get_conn()
        prefetch = om._load_lead_sync_prefetch(conn, DEFAULT_ORG_ID, [lead_id])
        payload_default = om.build_lead_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug="default", prefetch=prefetch,
        )
        payload_b = om.build_lead_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug="team-b", prefetch=prefetch,
        )
        conn.close()

        self.assertEqual(len(prefetch["memberships"][lead_id]), 2)
        self.assertEqual(payload_default["lead_status"], "warm")
        self.assertEqual(payload_b["lead_status"], "cold")
        self.assertEqual(payload_default["activity"]["email_sent_count"], 5)
        self.assertEqual(payload_b["activity"]["email_sent_count"], 2)


if __name__ == "__main__":
    unittest.main()
