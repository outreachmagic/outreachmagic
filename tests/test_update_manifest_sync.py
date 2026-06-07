"""Ensure pipeline update downloads every file listed in update-manifest.json."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402

# Mirror scripts/generate-update-manifest.py MANIFEST_FILES (scripts only).
MANIFEST_SCRIPT_FILES = {
    "pipeline.py",
    "constants.py",
    "db_conn.py",
    "formatters.py",
    "bounces.py",
    "activity_sync.py",
    "event_classification.py",
    "lead_sync.py",
    "platform_registry.py",
    "relay_ingest.py",
    "quarantine_resolutions.py",
    "relay_extractors.py",
    "workspace_routing.py",
    "workspace_archive.py",
    "routing_cloud.py",
    "agent_secrets_cloud.py",
    "api_key_pool.py",
    "connections_cloud.py",
    "db_health.py",
    "om_paths.py",
    "device_login.py",
    "read_queries.py",
    "query_cli.py",
    "data_freshness.py",
    "schema.py",
    "schema_views.py",
}


class UpdateManifestSyncTests(unittest.TestCase):
    def test_update_script_files_match_manifest_generator(self):
        self.assertEqual(set(om.UPDATE_SCRIPT_FILES), MANIFEST_SCRIPT_FILES)


if __name__ == "__main__":
    unittest.main()
