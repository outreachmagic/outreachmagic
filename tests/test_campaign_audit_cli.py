#!/usr/bin/env python3
"""`pipeline.py campaign-audit` — the command the Campaigns table is made of.

The governing rule for this repo is that every capability ships as a library
function with a CLI command and --json; the dashboard is a second consumer, not
the owner. campaign_audit was the one big reporting surface with no command at
all, which is why agents hand-wrote reply SQL against it and kept losing the
date filter to operator precedence.
"""

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import deliverability_cli  # noqa: E402
import lead_actions  # noqa: E402
import pipeline as om  # noqa: E402
import reporting_queries as dq  # noqa: E402
from pipeline_migration import _backfill_first_positive_at  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

CAMPAIGN = "alpha | outbound"


def _args(**kw):
    base = dict(workspace="alpha", since=None, until=None, kind="all",
                tags_any=None, tags_all=None, tags_none=None,
                include_scheduling=False, positive_anchor="first",
                sort="sent", limit=0, json=True, quiet=True)
    base.update(kw)
    return argparse.Namespace(command="campaign-audit", **base)


def _run(**kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        deliverability_cli.cmd_campaign_audit(_args(**kw))
    return buf.getvalue()


class CampaignAuditCliTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
        om.create_workspace("Team Alpha", slug="alpha")
        conn = om.get_conn()
        self.ws_id = om.resolve_workspace_identity(conn, "alpha")["id"]
        conn.close()
        for i in range(3):
            lead_id = om.add_lead(f"Lead {i}", company="Acme",
                                  email=f"l{i}@x-example.com")["id"]
            lead_actions.log_event_scoped(
                lead_id, "email_sent", workspace_slug="alpha", subject="Hi",
                metadata={"campaign": CAMPAIGN})
            if i == 0:
                lead_actions.log_event_scoped(
                    lead_id, "email_reply", workspace_slug="alpha",
                    direction="inbound", subject="Re: Hi",
                    metadata={"campaign": CAMPAIGN})
                lead_actions.change_stage_scoped(
                    lead_id, "interested", workspace_slug="alpha",
                    sentiment="positive")
        # The CLI defaults to the `first` anchor, like the dashboard. That
        # column is written by relay ingest or its one-shot backfill, never by a
        # local stage change -- so seed it the way production does.
        conn = om.get_conn()
        try:
            _backfill_first_positive_at(conn)
            conn.commit()
        finally:
            conn.close()

    def test_json_matches_the_library_call(self):
        payload = json.loads(_run())
        conn = om.get_conn()
        try:
            direct = dq.campaign_audit(conn, self.ws_id, positive_anchor="first")
        finally:
            conn.close()
        self.assertEqual(payload["campaigns"], direct["campaigns"])
        self.assertEqual(payload["totals"], direct["totals"])
        self.assertEqual(payload["workspace"], "alpha")

    def test_the_headline_question_is_one_command(self):
        """"Positive human replies in a window, by campaign" — no SQL."""
        payload = json.loads(_run(kind="human", since="30d"))
        row = payload["campaigns"][0]
        self.assertEqual(row["positive"], 1)
        self.assertEqual(row["sent"], 3, "kind never shrinks the denominator")
        self.assertEqual(payload["reply_kind"], "human")

    def test_text_mode_prints_a_total_line(self):
        out = _run(json=False)
        self.assertIn("TOTAL", out)
        self.assertIn(CAMPAIGN[:34], out)
        total_line = [ln for ln in out.splitlines() if ln.strip().startswith("TOTAL")][0]
        self.assertIn("3", total_line.split())

    def test_text_mode_states_an_active_filter(self):
        out = _run(json=False, kind="human", tags_none=["existing_customer"])
        self.assertIn("kind=human", out)
        self.assertIn("excluding=existing_customer", out)

    def test_unknown_kind_exits_with_an_error_not_an_empty_table(self):
        with self.assertRaises(SystemExit) as ctx:
            _run(kind="reply")
        self.assertEqual(ctx.exception.code, 1)

    def test_sort_and_limit_apply_to_the_rows_not_the_totals(self):
        payload = json.loads(_run(sort="positive", limit=1))
        self.assertEqual(len(payload["campaigns"]), 1)
        self.assertEqual(payload["totals"]["sent"], 3)

    def test_the_command_is_registered_on_the_real_parser(self):
        import pipeline_cli

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        deliverability_cli.register_campaign_audit_parser(sub)
        args = parser.parse_args(
            ["campaign-audit", "--workspace", "alpha", "--kind", "human",
             "--exclude-tag", "existing_customer", "--json"])
        self.assertEqual(args.command, "campaign-audit")
        self.assertEqual(args.tags_none, ["existing_customer"])
        self.assertIn("register_campaign_audit_parser",
                      Path(pipeline_cli.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
