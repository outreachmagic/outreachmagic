"""Tests for cloud-synced quarantine resolutions."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import quarantine_resolutions as qres  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


class QuarantineResolutionTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        if db_path.exists():
            db_path.unlink()
        om.init_db()
        om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
        om.create_workspace("Team Alpha", slug="alpha")

    def _quarantine_event(self, relay_id: int = 501, email: str = "ghost@test.com"):
        event = {
            "platform": "smartlead",
            "event_type": "email_sent",
            "lead": email,
            "received_at": "2026-05-23T00:00:00Z",
            "relay_id": relay_id,
            "raw": {"campaign_id": "missing", "campaign_name": "Ghost", "to_email": email},
        }
        self.assertIsNone(om.ingest_relay_event(event, quiet=True))
        pending = [
            p for p in om.list_quarantine(status="pending", limit=10)
            if str(p.get("external_event_id")) == str(relay_id)
        ]
        self.assertEqual(len(pending), 1)
        return pending[0]["id"], event

    def test_skip_sets_cloud_pending(self):
        qid, _ = self._quarantine_event()
        result = om.skip_quarantine(qid)
        self.assertEqual(result["status"], "ok")
        conn = om.get_conn()
        row = conn.execute(
            "SELECT status, cloud_pending FROM unmapped_campaign_queue WHERE id = ?",
            (qid,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["cloud_pending"], 1)

    def test_assign_deferred(self):
        qid, _ = self._quarantine_event()
        result = om.assign_quarantine(qid, "alpha")
        self.assertEqual(result["status"], "ok")
        conn = om.get_conn()
        row = conn.execute(
            "SELECT status, assigned_workspace, cloud_pending FROM unmapped_campaign_queue WHERE id = ?",
            (qid,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "assigned")
        self.assertEqual(row["assigned_workspace"], "alpha")
        self.assertEqual(row["cloud_pending"], 1)

    def test_parse_queue_resolutions(self):
        raw = [
            {"relay_id": 1, "status": "skipped"},
            {"relay_id": 2, "status": "assigned", "workspace_slug": "alpha"},
            {"relay_id": "bad", "status": "skipped"},
        ]
        parsed = qres.parse_queue_resolutions(raw)
        self.assertEqual(parsed[1]["status"], "skipped")
        self.assertEqual(parsed[2]["status"], "assigned")
        self.assertEqual(parsed[2]["workspace_slug"], "alpha")
        self.assertNotIn(3, qres.parse_queue_resolutions([{"relay_id": 3, "status": "assigned"}]))

    def test_ingest_skips_cloud_resolved(self):
        _, event = self._quarantine_event(relay_id=601)
        resolution_map = {601: {"status": "skipped"}}
        batch = om._ingest_relay_page([event], resolution_map=resolution_map, quiet=True)
        self.assertEqual(batch["skipped_resolved"], 1)
        self.assertEqual(batch["imported"], 0)
        # Local pending row remains until user runs quarantine skip; cloud skip only affects pull.
        self.assertEqual(len(om.list_quarantine(status="pending")), 1)

    def test_ingest_assigned_resolution(self):
        _, event = self._quarantine_event(relay_id=701)
        resolution_map = {701: {"status": "assigned", "workspace_slug": "alpha"}}
        batch = om._ingest_relay_page([event], resolution_map=resolution_map, quiet=True)
        self.assertEqual(batch["assigned_resolved"], 1)
        self.assertEqual(batch["imported"], 1)
        conn = om.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM leads WHERE email = ?", ("ghost@test.com",)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    @patch.object(qres, "push_resolutions_to_relay")
    def test_sync_pushes_resolutions(self, mock_push):
        mock_push.return_value = {"status": "ok", "synced": 1, "errors": []}
        qid, _ = self._quarantine_event()
        om.skip_quarantine(qid)
        with patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with patch.object(om.routing_cloud, "cloud_routing_enabled", return_value=True):
                with patch.object(
                    om.routing_cloud,
                    "fetch_routing_bundle",
                    return_value={"workspaces": [], "campaignMaps": []},
                ):
                    with patch.object(om, "_push_pending_lead_updates", return_value={"pushed": 0}):
                        with patch.object(om, "_push_pending_company_updates", return_value={"pushed": 0}):
                            with patch.object(om, "_push_agent_events_to_relay", return_value={"pushed": 0}):
                                om.sync_all(no_health_report=True)
        mock_push.assert_called_once()
        resolves = mock_push.call_args[0][2]
        self.assertEqual(resolves[0]["status"], "skipped")
        conn = om.get_conn()
        pending = conn.execute(
            "SELECT cloud_pending FROM unmapped_campaign_queue WHERE id = ?", (qid,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(pending, 0)

    @patch.object(qres, "push_resolutions_to_relay")
    def test_sync_clears_only_successful_relay_ids(self, mock_push):
        qid_a, _ = self._quarantine_event(relay_id=801, email="a@test.com")
        qid_b, _ = self._quarantine_event(relay_id=802, email="b@test.com")
        om.skip_quarantine(qid_a)
        om.skip_quarantine(qid_b)
        mock_push.return_value = {
            "status": "ok",
            "synced": 1,
            "errors": [{"relay_id": 801, "error": "invalid_status"}],
        }
        with patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with patch.object(om.routing_cloud, "cloud_routing_enabled", return_value=True):
                with patch.object(
                    om.routing_cloud,
                    "fetch_routing_bundle",
                    return_value={"workspaces": [], "campaignMaps": []},
                ):
                    with patch.object(om, "_push_pending_lead_updates", return_value={"pushed": 0}):
                        with patch.object(om, "_push_pending_company_updates", return_value={"pushed": 0}):
                            with patch.object(om, "_push_agent_events_to_relay", return_value={"pushed": 0}):
                                om._push_pending_quarantine_resolutions("om_agent_test")
        conn = om.get_conn()
        rows = {
            int(r["external_event_id"]): r["cloud_pending"]
            for r in conn.execute(
                "SELECT external_event_id, cloud_pending FROM unmapped_campaign_queue"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(rows.get(801), 1)
        self.assertEqual(rows.get(802), 0)

    def test_pull_requests_resolutions_only_on_first_page(self):
        calls: list[dict] = []

        def fake_pull(agent_key, after_id=None, **kwargs):
            calls.append({"after_id": after_id, **kwargs})
            return {"events": [], "queue_resolutions": []}

        with patch.object(om, "pull_events_org", side_effect=fake_pull):
            with patch.object(om, "maybe_sync_routing_from_cloud"):
                with patch.object(om, "get_last_max_id", return_value=0):
                    with patch.object(om, "get_last_snapshot_after_id", return_value=0):
                        with patch.object(om, "set_last_max_id"):
                            with patch.object(om, "set_last_snapshot_after_id"):
                                om.sync_from_relay_org("key", full=True, quiet=True)
        event_calls = [c for c in calls if not c.get("snapshots_only")]
        self.assertGreaterEqual(len(event_calls), 1)
        self.assertTrue(event_calls[0].get("include_queue_resolutions"))


if __name__ == "__main__":
    unittest.main()
