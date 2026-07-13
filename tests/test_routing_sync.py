#!/usr/bin/env python3
"""Tests for routing rule sync deduplication (local vs cloud IDs)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import routing_cloud  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID, assign_campaign_map  # noqa: E402


class RoutingSyncPendingTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.execute(
            """INSERT OR IGNORE INTO workspaces (id, org_id, name, slug, cloud_synced, created_at, updated_at)
               VALUES ('ws_acme', ?, 'AcmeCo', 'acme', 1, datetime('now'), datetime('now'))""",
            (DEFAULT_ORG_ID,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = om.get_conn()
        conn.execute("DELETE FROM campaign_workspace_map")
        conn.commit()
        conn.close()

    def test_cloud_signature_skips_semantically_synced_local_rule(self):
        conn = om.get_conn()
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id="ws_acme",
            campaign_name="acme",
            match_strategy="rule_contains",
        )
        conn.commit()
        conn.close()

        cloud_bundle = {
            "workspaces": [{"id": "ws_acme", "slug": "acme", "name": "AcmeCo"}],
            "campaignMaps": [
                {
                    "id": "cloud_cuid_abc",
                    "sourcePlatform": "*",
                    "matchStrategy": "rule_contains",
                    "campaignId": None,
                    "campaignNameNormalized": "acme",
                    "workspaceSlug": "acme",
                }
            ],
        }

        with patch.object(routing_cloud, "fetch_routing_bundle", return_value=cloud_bundle):
            with patch.object(om, "get_agent_key", return_value="test_key"):
                with patch.object(routing_cloud, "cloud_routing_enabled", return_value=True):
                    status = om.get_sync_status()

        self.assertEqual(status["pending_rules"], [])

    def test_local_only_rule_still_pending(self):
        conn = om.get_conn()
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id="ws_acme",
            campaign_name="acme",
            match_strategy="rule_contains",
        )
        conn.commit()
        conn.close()

        cloud_bundle = {
            "workspaces": [{"id": "ws_acme", "slug": "acme", "name": "AcmeCo"}],
            "campaignMaps": [],
        }

        with patch.object(routing_cloud, "fetch_routing_bundle", return_value=cloud_bundle):
            with patch.object(om, "get_agent_key", return_value="test_key"):
                with patch.object(routing_cloud, "cloud_routing_enabled", return_value=True):
                    status = om.get_sync_status()

        self.assertEqual(len(status["pending_rules"]), 1)

    def test_apply_bundle_deactivates_shadowed_backfill(self):
        conn = om.get_conn()
        assign_campaign_map(
            conn, DEFAULT_ORG_ID, source_platform="*", workspace_id="ws_acme",
            campaign_name="acme summer", match_strategy="name_exact",
            map_source="single_mode_backfill",
        )
        conn.commit()
        bundle = {
            "mode": "multi",
            "workspaces": [{"id": "ws_acme", "slug": "acme", "name": "AcmeCo"}],
            "campaignMaps": [
                {
                    "id": "cloud_rule_1",
                    "sourcePlatform": "*",
                    "matchStrategy": "rule_contains",
                    "campaignPlatformId": None,
                    "campaignNameNormalized": "acme",
                    "workspaceId": "ws_acme",
                }
            ],
        }
        result = routing_cloud.apply_routing_bundle_to_sqlite(conn, bundle, org_id=DEFAULT_ORG_ID)
        conn.commit()
        self.assertEqual(len(result["deactivated_shadowed_rules"]), 1)
        row = conn.execute(
            """SELECT is_active FROM campaign_workspace_map
               WHERE match_strategy = 'name_exact' AND campaign_name_normalized = 'acme summer'"""
        ).fetchone()
        conn.close()
        self.assertEqual(row["is_active"], 0)

    def test_add_campaign_map_cli_deactivates_and_reports(self):
        conn = om.get_conn()
        assign_campaign_map(
            conn, DEFAULT_ORG_ID, source_platform="*", workspace_id="ws_acme",
            campaign_name="acme summer", match_strategy="name_exact",
            map_source="single_mode_backfill",
        )
        conn.commit()
        conn.close()
        result = om.add_campaign_map_cli(
            "*", "acme", campaign_name="acme", match_strategy="rule_contains"
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(result.get("deactivated_shadowed_rules", [])), 1)
        self.assertNotIn("unresolved_conflicts", result)

    def test_add_campaign_map_cli_surfaces_unresolved_manual_conflict(self):
        conn = om.get_conn()
        # A deliberate manual name_exact -> ws_acme (reads as 'manual').
        assign_campaign_map(
            conn, DEFAULT_ORG_ID, source_platform="*", workspace_id="ws_acme",
            campaign_name="acme summer", match_strategy="name_exact",
        )
        conn.execute(
            """INSERT OR IGNORE INTO workspaces (id, org_id, name, slug, cloud_synced, created_at, updated_at)
               VALUES ('ws_beta', ?, 'Beta', 'beta', 1, datetime('now'), datetime('now'))""",
            (DEFAULT_ORG_ID,),
        )
        conn.commit()
        conn.close()
        # New rule_contains -> a different workspace, so the manual row still shadows it.
        result = om.add_campaign_map_cli(
            "*", "beta", campaign_name="acme", match_strategy="rule_contains"
        )
        self.assertIn("unresolved_conflicts", result)
        self.assertTrue(
            any(c["campaign_name"] == "acme summer" for c in result["unresolved_conflicts"])
        )

    def test_campaign_map_signature_normalizes_platform(self):
        sig = routing_cloud.campaign_map_signature(
            source_platform="*",
            match_strategy="rule_contains",
            campaign_platform_id=None,
            campaign_name_normalized="AcmeCo",
            workspace_slug="acme",
        )
        self.assertEqual(sig, ("*", "rule_contains", None, "acmeco", "acme"))


if __name__ == "__main__":
    unittest.main()
