"""Tests for lead review export/sync helpers."""

from __future__ import annotations

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
import pipeline_lead_review as plr  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


class TestLeadReview(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        _reset_db()

    def test_normalize_review_template_aliases(self):
        self.assertEqual(plr.normalize_review_template("dedup"), "dedup-review")
        self.assertEqual(plr.normalize_review_template("lead"), "lead-review")

    def test_resolve_columns_basic(self):
        cols = plr.resolve_columns("basic")
        keys = [k for _label, k in cols]
        self.assertIn("lead_id", keys)
        self.assertIn("notes_action", keys)
        self.assertNotIn("linkedin", keys)

    def test_email_finder_candidates_rejects_company_name_domain(self):
        leads = [
            {
                "id": 1,
                "name": "Jane",
                "company": "Ohio University",
                "company_domain": "Ohio University",
            },
            {
                "id": 2,
                "name": "John",
                "company": "Acme",
                "company_domain": "acme.com",
            },
        ]
        out = plr.email_finder_candidates_from_leads(leads)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["company_domain"], "acme.com")
        self.assertEqual(out[0]["lead_id"], 2)

    def test_apply_sync_updates_stage_and_tags(self):
        ws = om.create_workspace("Review Test", slug="review-ws")
        ws_id = f"ws_{ws['slug']}"
        conn = om.get_conn()
        conn.execute(
            "INSERT INTO leads (name, email, stage) VALUES ('A', 'a@test.com', 'prospecting')"
        )
        lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id, status="prospecting")
        conn.commit()

        summary = plr.apply_lead_review_sync(
            conn,
            ws_id,
            [{"lead_id": lead_id, "workspace_stage": "contacted", "tags": "tier_1; nace"}],
            upsert_workspace_lead_fn=om.upsert_workspace_lead,
            org_id=om.DEFAULT_ORG_ID,
            dry_run=False,
        )
        self.assertEqual(summary["updated"], 1)
        row = conn.execute(
            "SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        self.assertEqual(row["status"], "contacted")
        tags = {
            r[0]
            for r in conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE lead_id = ?", (lead_id,)
            ).fetchall()
        }
        self.assertIn("tier_1", tags)
        self.assertIn("nace", tags)
        conn.close()


if __name__ == "__main__":
    unittest.main()
