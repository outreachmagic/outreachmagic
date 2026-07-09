"""Tests for pipeline.py query subcommand (in-process)."""

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402
import pipeline as om  # noqa: E402
import query_cli  # noqa: E402


class QueryCliInProcessTests(unittest.TestCase):
    def setUp(self):
        self._prev_data = om_paths._DATA_ROOT_OVERRIDE
        self._prev_project = om_paths._PROJECT_ROOT_OVERRIDE
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        om_paths.set_data_root_override(root)
        om_paths.set_project_root_override(root / "project")
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        om.init_db()
        conn = om.get_conn()
        conn.execute("INSERT INTO campaigns (name) VALUES ('pop | camp')")
        cid = conn.execute(
            "SELECT id FROM campaigns WHERE name = 'pop | camp'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO leads (name, email, channel, stage)
               VALUES ('P', 'p@test.com', 'email', 'prospecting')"""
        )
        lid = conn.execute("SELECT id FROM leads WHERE email = 'p@test.com'").fetchone()[0]
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, campaign_id, created_at)
               VALUES (?, 'email_reply', 'inbound', 'email', ?, datetime('now', '-1 hours'))""",
            (lid, cid),
        )

        conn.execute("INSERT OR IGNORE INTO organizations (id, name) VALUES ('default', 'Default')")
        conn.execute(
            "INSERT INTO workspaces (id, org_id, name, slug) VALUES ('ws1', 'default', 'EACE26', 'eace26')"
        )
        conn.execute(
            """INSERT INTO leads (name, email, linkedin_url, company, email_verification_status)
               VALUES ('Alice', 'a@x.com', 'li.com/a', 'Acme', 'valid')"""
        )
        conn.execute("INSERT INTO leads (name, email) VALUES ('Bob', 'b@x.com')")
        tag_lead_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM leads WHERE email IN ('a@x.com', 'b@x.com')"
            ).fetchall()
        ]
        for i, tlid in enumerate(tag_lead_ids):
            conn.execute(
                "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag) VALUES (?, 'ws1', ?, 'eace26')",
                (f"t-tag-{i}", tlid),
            )
        conn.execute(
            "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag) VALUES ('t-serper', 'ws1', ?, 'serper_attempted')",
            (tag_lead_ids[0],),
        )
        conn.execute(
            "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag) VALUES ('t-speaker', 'ws1', ?, 'speaker')",
            (tag_lead_ids[0],),
        )
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) VALUES ('wl1', 'default', 'ws1', ?, 'contacted')",
            (tag_lead_ids[0],),
        )
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) VALUES ('wl2', 'default', 'ws1', ?, 'prospecting')",
            (tag_lead_ids[1],),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        om_paths.set_data_root_override(self._prev_data)
        om_paths.set_project_root_override(self._prev_project)
        self._tmp.cleanup()

    def _args(self, **kwargs):
        defaults = {
            "preset": None,
            "workspace": None,
            "campaign_prefix": None,
            "since": None,
            "direction": "inbound",
            "event_types": None,
            "sql": None,
            "params": None,
            "file": None,
            "limit": 500,
            "json": True,
            "command": "query",
        }
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_engagement_preset_json(self):
        buf = StringIO()
        err = StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            query_cli.cmd_query(self._args(preset="engagement", workspace="pop", since="48h"))
        data = json.loads(buf.getvalue())
        self.assertEqual(data["preset"], "engagement")
        self.assertGreaterEqual(data["row_count"], 1)
        self.assertIn("freshness", data)
        self.assertTrue(
            "Data as of" in err.getvalue() or "never been pulled" in err.getvalue(),
            err.getvalue(),
        )

    def test_sql_rejects_mutation(self):
        with self.assertRaises(SystemExit):
            query_cli.cmd_query(self._args(sql="DELETE FROM events"))

    def test_sql_select_ok(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_query(self._args(sql="SELECT COUNT(*) AS n FROM events"))
        data = json.loads(buf.getvalue())
        self.assertEqual(data["rows"][0]["n"], 1)

    def test_sql_alias_parser_routes_to_query(self):
        """`pipeline.py sql "..."` is a thin argparse alias for `query --sql`, not a reimplementation."""
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        query_cli.register_sql_parser(sub)
        args = parser.parse_args(["sql", "SELECT COUNT(*) AS n FROM events", "--json"])
        self.assertEqual(args.command, "query")
        self.assertIsNone(args.preset)
        self.assertEqual(args.sql, "SELECT COUNT(*) AS n FROM events")
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_query(args)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["rows"][0]["n"], 1)

    def _schema_args(self, **kwargs):
        defaults = {"table": None, "json": True, "command": "schema"}
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_schema_lists_tables(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_schema(self._schema_args())
        data = json.loads(buf.getvalue())
        self.assertIn("leads", data["tables"])
        self.assertTrue(data["db_path"])

    def test_schema_table_columns(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_schema(self._schema_args(table="leads"))
        data = json.loads(buf.getvalue())
        col_names = {c["name"] for c in data["columns"]}
        self.assertIn("email_verification_status", col_names)
        self.assertIn("email_domain", col_names)
        self.assertIn("company", col_names)

    def test_schema_unknown_table(self):
        buf = StringIO()
        with self.assertRaises(SystemExit):
            with patch("sys.stdout", buf):
                query_cli.cmd_schema(self._schema_args(table="not_a_table"))
        data = json.loads(buf.getvalue())
        self.assertIn("not_a_table", data["error"])
        self.assertIn("leads", data["error"])

    def _tag_summary_args(self, **kwargs):
        defaults = {"tag": "eace26", "workspace": "eace26", "json": True, "command": "tag-summary"}
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_tag_summary_json_shape(self):
        buf = StringIO()
        err = StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            query_cli.cmd_tag_summary(self._tag_summary_args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["total_leads"], 2)
        self.assertEqual(data["research"], [{"tag": "serper_attempted", "label": "Serper", "attempted": 1, "pending": 1}])
        self.assertEqual(data["segments"], [{"tag": "speaker", "lead_count": 1}])
        self.assertEqual(data["workspace_status"], {"contacted": 1, "prospecting": 1})
        self.assertEqual(data["data_completeness"]["has_linkedin"], 1)
        self.assertIn("freshness", data)

    def test_tag_summary_text_format(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            query_cli.cmd_tag_summary(self._tag_summary_args(json=False))
        text = buf.getvalue()
        self.assertIn("eace26 Summary (2 leads", text)
        self.assertIn("Serper attempted: 1 / 2", text)
        self.assertIn("speaker: 1", text)

    def test_tag_summary_unknown_workspace(self):
        buf = StringIO()
        with self.assertRaises(SystemExit):
            with patch("sys.stdout", buf):
                query_cli.cmd_tag_summary(self._tag_summary_args(workspace="does-not-exist"))
        data = json.loads(buf.getvalue())
        self.assertIn("does-not-exist", data["error"])


if __name__ == "__main__":
    unittest.main()
