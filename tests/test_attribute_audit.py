#!/usr/bin/env python3
"""`attribute-audit` — best-performing lead attributes, over a real window.

Two bugs meet here. The dashboard's attributes card ignored the page date
range entirely and answered from lifetime counters, so a "last 14 days" label
sat above an all-time number. And the question had no CLI command at all,
which is why an agent asked it in hand-written SQL, reached for
`current_sentiment_since` (when the status LAST changed), got nothing back for
14 days, and concluded the data was missing.
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
    base = dict(workspace="alpha", field="title", since=None, until=None,
                min_sample=1, campaign_id=None, kind="all", tags_any=None,
                tags_all=None, tags_none=None, positive_anchor="first",
                sort="reply_rate", limit=0, json=True, quiet=True)
    base.update(kw)
    return argparse.Namespace(command="attribute-audit", **base)


def _run(**kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        deliverability_cli.cmd_attribute_audit(_args(**kw))
    return buf.getvalue()


class AttributeAuditTests(unittest.TestCase):
    """Two titles. "Director" was reached recently and replied; "Coordinator"
    was reached a month ago. A 14-day question must see one and not the other.
    """

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

        self.recent = om.add_lead("Dana Reyes", company="Acme",
                                  email="dana@acme-example.com", title="Director")["id"]
        self.old = om.add_lead("Kim Alvarez", company="Acme",
                               email="kim@acme-example.com", title="Coordinator")["id"]
        for lead_id in (self.recent, self.old):
            lead_actions.log_event_scoped(
                lead_id, "email_sent", workspace_slug="alpha", subject="Hi",
                metadata={"campaign": CAMPAIGN})
        lead_actions.log_event_scoped(
            self.recent, "email_reply", workspace_slug="alpha", direction="inbound",
            subject="Re: Hi", metadata={"campaign": CAMPAIGN})
        lead_actions.change_stage_scoped(
            self.recent, "interested", workspace_slug="alpha", sentiment="positive")

        conn = om.get_conn()
        try:
            _backfill_first_positive_at(conn)
            # Push the Coordinator's only activity a month back, and leave the
            # Director's inside the window.
            conn.execute(
                "UPDATE workspace_lead_events SET event_at = datetime('now', '-30 days') "
                "WHERE lead_id = ?", (self.old,))
            conn.commit()
        finally:
            conn.close()

    def test_a_window_excludes_activity_outside_it(self):
        rows = json.loads(_run(since="14d"))["rows"]
        values = [r["value"] for r in rows]
        self.assertIn("Director", values)
        self.assertNotIn("Coordinator", values,
                         "contacted 30 days ago, so not contacted in 14 days")

    def test_without_a_window_the_lifetime_columns_still_answer(self):
        """No range means no behaviour change: the old columns, the old answer.

        Note what the fixture shows about those columns. Both leads were sent
        an email, but only the one whose activity was later refreshed carries
        email_sent_count > 0 -- the counters are maintained by relay ingest,
        not by every local write. That unreliability is background to why the
        windowed path counts events instead.
        """
        payload = json.loads(_run())
        self.assertEqual(payload["window"], "lifetime")
        self.assertIsNone(payload["since_resolved_utc"])
        self.assertEqual([r["value"] for r in payload["rows"]], ["Director"])

    def test_the_window_is_named_and_resolved(self):
        payload = json.loads(_run(since="14d"))
        self.assertEqual(payload["window"], "activity")
        self.assertEqual(payload["window_days"], 14.0)
        self.assertTrue(payload["since_resolved_utc"].endswith("Z"))

    def test_positives_come_from_first_positive_at(self):
        """Not current_sentiment_since, which is when the status LAST changed."""
        conn = om.get_conn()
        try:
            # The lead turned positive inside the window and has since gone
            # cold: `first` must still credit it, `current` must not.
            conn.execute(
                "UPDATE workspace_leads SET current_status_sentiment = 'negative', "
                "current_sentiment_since = datetime('now', '-200 days') "
                "WHERE lead_id = ?", (self.recent,))
            conn.commit()
            first = dq.attribute_performance(
                conn, self.ws_id, field="title", min_sample=1, since="14d",
                positive_anchor="first")
            current = dq.attribute_performance(
                conn, self.ws_id, field="title", min_sample=1, since="14d",
                positive_anchor="current")
        finally:
            conn.close()
        self.assertEqual(first["rows"][0]["positive"], 1)
        self.assertEqual(current["rows"][0]["positive"], 0)

    def test_min_sample_drops_thin_rows(self):
        self.assertEqual(json.loads(_run(min_sample=5))["rows"], [])

    def test_reply_rate_is_replied_over_contacted_in_window(self):
        row = json.loads(_run(since="14d"))["rows"][0]
        self.assertEqual((row["contacted"], row["replied"]), (1, 1))
        self.assertEqual(row["reply_rate"], 1.0)

    def test_an_unknown_field_names_the_valid_ones(self):
        with self.assertRaises(SystemExit):
            _run(field="favourite_colour")

    def test_tag_filters_reach_the_command(self):
        payload = json.loads(_run(since="14d", tags_none=["existing_customer"]))
        self.assertEqual(payload["tags_none"], ["existing_customer"])

    def test_text_mode_states_the_window_it_means(self):
        out = _run(json=False, since="14d")
        self.assertIn("activity window", out)
        self.assertIn("Director", out)

    def test_the_command_is_registered_on_the_real_parser(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        deliverability_cli.register_attribute_audit_parser(sub)
        args = parser.parse_args(
            ["attribute-audit", "--workspace", "alpha", "--since", "14d",
             "--field", "title", "--json"])
        self.assertEqual(args.command, "attribute-audit")
        self.assertEqual(args.since, "14d")
        import pipeline_cli
        self.assertIn("register_attribute_audit_parser",
                      Path(pipeline_cli.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
