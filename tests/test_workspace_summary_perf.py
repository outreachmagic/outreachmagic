#!/usr/bin/env python3
"""workspace summary performance and --tags-only flag."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
from workspace_routing import DEFAULT_ORG_ID, ensure_organization, upsert_workspace_lead  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    conn = om.get_conn()
    ensure_organization(conn)
    conn.close()


class TestWorkspaceSummaryPerf(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.ws = om.create_workspace("Perf WS", slug="perfws")
        self.ws_id = self.ws["id"]

    def _seed_leads_with_tags(self, count: int, *, with_linkedin_status: bool = False) -> None:
        conn = om.get_conn()
        try:
            for i in range(count):
                result = om.resolve_lead(
                    email=f"user{i}@example.com",
                    name=f"User {i}",
                    company=f"Co {i}",
                    source="csv",
                    source_platform="csv",
                    conn=conn,
                )
                lead_id = int(result["id"])
                upsert_workspace_lead(conn, DEFAULT_ORG_ID, self.ws_id, lead_id)
                for tag in ("nace",) + (("serper_attempted",) if i % 10 == 0 else ()):
                    tag_id = f"wlt_{self.ws_id}_{lead_id}_{tag}"
                    conn.execute(
                        """INSERT OR IGNORE INTO workspace_lead_tags
                           (id, workspace_id, lead_id, tag) VALUES (?, ?, ?, ?)""",
                        (tag_id, self.ws_id, lead_id, tag),
                    )
                if with_linkedin_status:
                    row_id = f"lis_{self.ws_id}_{lead_id}_sender1"
                    conn.execute(
                        """INSERT OR IGNORE INTO workspace_lead_linkedin_status
                           (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            row_id,
                            self.ws_id,
                            lead_id,
                            "linkedin.com/in/sender1",
                            1 if i % 3 == 0 else 0,
                            1 if i % 5 == 0 else 0,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def test_tags_only_skips_linkedin_aggregates(self):
        self._seed_leads_with_tags(50, with_linkedin_status=True)
        full = om.get_workspace_summary("perfws", tags_only=False)
        tags = om.get_workspace_summary("perfws", tags_only=True)
        self.assertGreater(full.get("lead_count", 0), 0)
        self.assertGreater(full.get("linkedin_connected_leads", 0), 0)
        self.assertEqual(tags.get("linkedin_connected_leads"), 0)
        self.assertEqual(tags.get("linkedin_senders"), [])
        self.assertTrue(any(t.get("tag") == "nace" for t in tags.get("tags") or []))

    def test_large_workspace_summary_under_five_seconds(self):
        self._seed_leads_with_tags(2500)
        t0 = time.monotonic()
        summary = om.get_workspace_summary("perfws", tags_only=True)
        elapsed = time.monotonic() - t0
        self.assertEqual(summary.get("workspace"), "perfws")
        self.assertGreaterEqual(summary.get("lead_count", 0), 2500)
        self.assertLess(elapsed, 5.0, f"tags-only summary took {elapsed:.2f}s")

    def test_cli_tags_only_json(self):
        self._seed_leads_with_tags(10)
        pending = {
            "workspaces": 0,
            "rules": 0,
            "local_agent_events": 0,
            "leads_pending": 0,
            "workspace_leads_pending": 0,
            "total": 0,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(om, "get_local_pending_counts", lambda: pending),
            patch.object(om, "notify_update_available", lambda **kwargs: None),
            patch.object(
                sys,
                "argv",
                [
                    "pipeline.py",
                    "workspace",
                    "summary",
                    "--workspace",
                    "perfws",
                    "--json",
                    "--tags-only",
                ],
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            om.main()
        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["workspace"], "perfws")
        self.assertEqual(payload["linkedin_connected_leads"], 0)


if __name__ == "__main__":
    unittest.main()
