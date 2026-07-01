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
               VALUES ('ws_popcam', ?, 'PopCam', 'popcam', 1, datetime('now'), datetime('now'))""",
            (DEFAULT_ORG_ID,),
        )
        conn.commit()
        conn.close()

    def test_cloud_signature_skips_semantically_synced_local_rule(self):
        conn = om.get_conn()
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id="ws_popcam",
            campaign_name="popcam",
            match_strategy="rule_contains",
        )
        conn.commit()
        conn.close()

        cloud_bundle = {
            "workspaces": [{"id": "ws_popcam", "slug": "popcam", "name": "PopCam"}],
            "campaignMaps": [
                {
                    "id": "cloud_cuid_abc",
                    "sourcePlatform": "*",
                    "matchStrategy": "rule_contains",
                    "campaignId": None,
                    "campaignNameNormalized": "popcam",
                    "workspaceSlug": "popcam",
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
            workspace_id="ws_popcam",
            campaign_name="popcam",
            match_strategy="rule_contains",
        )
        conn.commit()
        conn.close()

        cloud_bundle = {
            "workspaces": [{"id": "ws_popcam", "slug": "popcam", "name": "PopCam"}],
            "campaignMaps": [],
        }

        with patch.object(routing_cloud, "fetch_routing_bundle", return_value=cloud_bundle):
            with patch.object(om, "get_agent_key", return_value="test_key"):
                with patch.object(routing_cloud, "cloud_routing_enabled", return_value=True):
                    status = om.get_sync_status()

        self.assertEqual(len(status["pending_rules"]), 1)

    def test_campaign_map_signature_normalizes_platform(self):
        sig = routing_cloud.campaign_map_signature(
            source_platform="*",
            match_strategy="rule_contains",
            campaign_platform_id=None,
            campaign_name_normalized="PopCam",
            workspace_slug="popcam",
        )
        self.assertEqual(sig, ("*", "rule_contains", None, "popcam", "popcam"))


if __name__ == "__main__":
    unittest.main()
