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
                sort="sent", limit=0, json=True, quiet=True,
                by_day=False, compare=False)
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


class CampaignAuditByDayTests(CampaignAuditCliTests):
    """`--by-day` exists so nobody writes the daily breakdown as SQL again.

    The last hand-written attempt was a four-way join with no row-reducing
    predicate on `events`; it never returned. The flag has to give the same
    answer as the totals it sits beside, or it just moves the problem.
    """

    def _spread_events_over_days(self):
        """Back-date the fixture's activity across three days."""
        conn = om.get_conn()
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT event_id FROM workspace_lead_events ORDER BY event_id").fetchall()]
            for offset, event_id in enumerate(ids):
                conn.execute(
                    "UPDATE workspace_lead_events SET event_at = "
                    "datetime('now', ?) WHERE event_id = ?",
                    (f"-{offset % 3} days", event_id))
            conn.commit()
        finally:
            conn.close()

    def test_daily_rows_sum_to_the_totals(self):
        self._spread_events_over_days()
        payload = json.loads(_run(by_day=True, since="30d"))
        self.assertTrue(payload["campaigns"])
        for row in payload["campaigns"]:
            self.assertIn("daily", row)
            for column in ("sent", "replies", "bounces"):
                self.assertEqual(
                    sum(d[column] for d in row["daily"]), row[column],
                    f"{column} disagrees with its daily rows on {row['name']}")
        # Not asserted for `meetings`: it is counted once per lead per DAY in
        # the array and once per lead per RANGE in the total, so a lead booking
        # on two days legitimately adds two to one and one to the other. And
        # not asserted for the four sentiment columns, which are anchored on
        # first_positive_at rather than on the event stream -- summing those
        # against per-day event counts would encode an invariant that is false.

    def test_daily_rows_are_dated_and_ordered(self):
        self._spread_events_over_days()
        row = json.loads(_run(by_day=True, since="30d"))["campaigns"][0]
        dates = [d["date"] for d in row["daily"]]
        self.assertEqual(dates, sorted(dates))
        self.assertGreater(len(dates), 1, "the fixture spans three days")
        self.assertEqual(set(row["daily"][0]), {"date", *dq.BY_DAY_KEYS})

    def test_no_daily_array_without_the_flag(self):
        row = json.loads(_run())["campaigns"][0]
        self.assertNotIn("daily", row)
        self.assertIsNone(json.loads(_run())["by_day_columns"])

    def test_the_window_is_resolved_in_the_payload(self):
        payload = json.loads(_run(since="14d"))
        self.assertEqual(payload["since"], "14d")
        self.assertTrue(payload["since_resolved_utc"].endswith("Z"))
        self.assertEqual(payload["window_days"], 14.0)

    def test_compare_previous_window_returns_both_halves(self):
        payload = json.loads(_run(compare=True, since="7d"))
        self.assertIn("previous_totals", payload)
        self.assertEqual(payload["previous_totals"]["sent"], 0,
                         "the fixture has no activity in the prior week")
        self.assertEqual(payload["totals"]["sent"], 3)
        self.assertEqual(payload["previous_window"]["until_resolved_utc"],
                         payload["since_resolved_utc"])

    def test_the_two_windows_do_not_overlap(self):
        """Back-to-back, not overlapping — the same send must not be in both.

        The previous window's upper bound is a timestamp, and a timestamp is an
        exact bound; rounding it up to the end of its day (which is right for a
        bare `--until 2026-08-14`) would hand the same 24 hours to both halves.
        """
        conn = om.get_conn()
        try:
            conn.execute("UPDATE workspace_lead_events SET event_at = "
                         "datetime('now', '-10 days')")
            conn.commit()
        finally:
            conn.close()
        payload = json.loads(_run(compare=True, since="7d"))
        self.assertEqual(payload["totals"]["sent"], 0,
                         "everything is older than 7 days")
        self.assertEqual(payload["previous_totals"]["sent"], 3,
                         "and lands in the 7 days before that")

    def test_compare_needs_a_window(self):
        with self.assertRaises(SystemExit):
            _run(compare=True, since=None)


if __name__ == "__main__":
    unittest.main()
