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
        labels = [label for label, _k in cols]
        self.assertIn("lead_id", keys)
        self.assertIn("notes", keys)
        self.assertNotIn("linkedin", keys)
        self.assertIn("✏️ Name", labels)
        self.assertEqual(labels[0], "🔒 Lead Id")

    def test_expand_field_groups(self):
        keys = plr.expand_field_groups(["lead_info", "workspace_stage"])
        self.assertEqual(keys[0], "lead_id")
        self.assertIn("name", keys)
        self.assertIn("workspace_stage", keys)

    def test_build_column_metadata_colors(self):
        cols = plr.build_column_metadata(["lead_id", "name", "email_sent_count"])
        self.assertEqual(cols[0]["type"], "key")
        self.assertEqual(cols[0]["label"], "🔒 Lead Id")
        self.assertEqual(cols[1]["label"], "✏️ Name")
        self.assertTrue(cols[1]["label"].startswith("✏️"))
        self.assertTrue(cols[2]["label"].startswith("🔒"))
        self.assertEqual(cols[1]["format"]["backgroundColor"], plr.EDIT_BG)

    def test_list_presets(self):
        resp = plr.list_presets()
        self.assertEqual(resp["template"], "lead-review")
        self.assertIn("basic", resp["presets"])
        self.assertIn("lead_info", resp["column_groups"])
        self.assertTrue(any(f["key"] == "tags" for f in resp["all_fields"]))

    def test_build_export_payload_includes_columns(self):
        ws = om.create_workspace("Export Meta", slug="export-meta")
        ws_id = f"ws_{ws['slug']}"
        conn = om.get_conn()
        conn.execute(
            "INSERT INTO leads (name, email, company) VALUES ('Pat', 'pat@test.com', 'Acme')"
        )
        lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.execute(
            "INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag) "
            "VALUES (?, ?, ?, ?)",
            (f"wlt_{ws_id}_{lead_id}_tier1", ws_id, lead_id, "tier_1"),
        )
        conn.commit()
        payload = plr.build_export_payload(
            conn,
            workspace="export-meta",
            detail="basic",
            title="Test",
            enrich_fn=om.enrich_lead_rows,
            limit=5,
        )
        self.assertIn("columns", payload)
        self.assertIn("field_keys", payload)
        self.assertTrue(payload.get("freeze_header"))
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("✏️ Name", payload["headers"])
        self.assertEqual(payload["headers"][0], "🔒 Lead Id")
        conn.close()

    def test_company_scope_sync_updates_companies_table(self):
        ws = om.create_workspace("Company Sync", slug="company-sync")
        ws_id = f"ws_{ws['slug']}"
        conn = om.get_conn()
        conn.execute("INSERT INTO companies (name, domain) VALUES ('Old Co', 'oldco.com')")
        company_id = conn.execute("SELECT id FROM companies").fetchone()[0]
        conn.execute(
            "INSERT INTO leads (name, email, company, company_id) VALUES ('Pat', 'p@test.com', 'Old Co', ?)",
            (company_id,),
        )
        lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.commit()

        summary = plr.apply_lead_review_sync(
            conn,
            ws_id,
            [{"lead_id": lead_id, "company": "New Co Name"}],
            upsert_workspace_lead_fn=om.upsert_workspace_lead,
            org_id=om.DEFAULT_ORG_ID,
            dry_run=False,
        )
        self.assertEqual(summary["updated"], 1)
        co = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
        self.assertEqual(co["name"], "New Co Name")
        conn.close()

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

    def test_find_in_row_emoji_headers(self):
        row = {"🔒 Lead Id": "42", "✏️ Name": "Jane", "🔒 Email Sent Count": "3"}
        self.assertEqual(plr._find_in_row(row, "lead_id"), "42")
        self.assertEqual(plr._find_in_row(row, "name"), "Jane")
        self.assertEqual(plr._normalize_header_key("🔒 Lead Id"), "lead_id")
        self.assertEqual(plr._normalize_header_key("✏️ Linkedin Url"), "linkedin_url")
        self.assertEqual(plr._normalize_header_key("✏️ Linkedin"), "linkedin_url")

    def test_apply_sync_skips_unchanged_values(self):
        ws = om.create_workspace("Review Skip", slug="review-skip")
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
            [{"🔒 Lead Id": lead_id, "✏️ Name": "A", "workspace_stage": "prospecting"}],
            upsert_workspace_lead_fn=om.upsert_workspace_lead,
            org_id=om.DEFAULT_ORG_ID,
            dry_run=True,
        )
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["updated"], 0)
        conn.close()

    def test_apply_sync_updates_lead_name_with_emoji_header(self):
        ws = om.create_workspace("Review Name", slug="review-name")
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
            [{"🔒 Lead Id": lead_id, "✏️ Name": "Alice Updated"}],
            upsert_workspace_lead_fn=om.upsert_workspace_lead,
            org_id=om.DEFAULT_ORG_ID,
            dry_run=False,
        )
        self.assertEqual(summary["updated"], 1)
        row = conn.execute("SELECT name FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(row["name"], "Alice Updated")
        conn.close()

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
