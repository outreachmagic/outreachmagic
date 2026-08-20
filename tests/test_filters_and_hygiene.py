#!/usr/bin/env python3
"""The invariants behind the 2026-08-12 filter/attribution work.

Each test here corresponds to a defect that shipped, and asserts the property
that would have caught it -- not the implementation that fixes it.
"""

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

import json  # noqa: E402

import data_hygiene  # noqa: E402
import reporting_queries as dq  # noqa: E402
import pipeline as om  # noqa: E402
from normalize import normalize_sentinel  # noqa: E402
from pipeline_update import normalize_relay_timestamp_for_storage as norm_ts  # noqa: E402

WS = "acme"


class _DbCase(unittest.TestCase):
    """conftest gives every test its own data root, so the schema is built per
    test rather than once for the module."""

    def setUp(self):
        om.init_db()


class SentinelTests(_DbCase):
    def test_placeholders_become_none_and_real_values_survive(self):
        for placeholder in ("unknown", "Unknown", "  N/A ", "none", "-", "TBD", ""):
            self.assertIsNone(normalize_sentinel(placeholder), placeholder)
        # The conservative half: only whole-string placeholders.
        for real in ("Unknown Systems Ltd", "N/A Consulting", "VP Sales"):
            self.assertEqual(normalize_sentinel(real), real.strip())

    def test_a_placeholder_never_reaches_the_database(self):
        """The guard sits in resolve_lead, so every ingest path inherits it."""
        result = om.resolve_lead(
            email="sentinel@example.test", name="Real Person",
            title="unknown", company="Acme", industry="N/A")
        conn = om.get_conn()
        row = conn.execute("SELECT title, industry FROM leads WHERE id = ?",
                           (result["id"],)).fetchone()
        conn.close()
        self.assertIsNone(row["title"])
        self.assertIsNone(row["industry"])


class TimestampTests(unittest.TestCase):
    def test_every_relay_dialect_collapses_to_one_comparable_shape(self):
        for raw in ("2025-04-18T13:59:23Z", "2025-04-18T13:59:23",
                    "2025-04-18 13:59:23.250000", "2025-04-18 13:59:23"):
            self.assertEqual(norm_ts(raw), "2025-04-18 13:59:23", raw)

    def test_mixed_formats_would_have_broken_range_comparisons(self):
        """Why this matters: every *_at filter is a text comparison."""
        # 'T' (0x54) sorts above ' ' (0x20), so within the same day an EARLIER
        # ISO timestamp compares as greater than a LATER space-formatted one.
        earlier_iso, later_plain = "2025-04-18T13:59:23", "2025-04-18 14:00:00"
        self.assertGreater(earlier_iso, later_plain)          # the bug
        self.assertLess(norm_ts(earlier_iso), norm_ts(later_plain))   # fixed

    def test_unparseable_input_is_left_visible_not_replaced_with_now(self):
        self.assertEqual(norm_ts("not a date"), "not a date")


class FilterClauseTests(_DbCase):
    def test_every_new_filter_is_registered(self):
        """A key missing from LEAD_FILTER_KEYS is an export that disagrees with
        the list it claims to mirror -- the list, the select-all ids, the tiles
        and the CSV all filter through this one tuple."""
        for key in ("sentiment", "sentiment_none", "verify_none", "contacted_within",
                    "contacted_since", "contacted_until", "has_email", "has_sales_nav"):
            self.assertIn(key, dq.LEAD_FILTER_KEYS)

    def test_each_new_filter_builds_valid_sql(self):
        conn = om.get_conn()
        for kwargs in ({"sentiment": ["positive"]}, {"sentiment_none": ["negative"]},
                       {"verify": ["valid", "catch_all"]}, {"verify": ["unverified"]},
                       {"verify_none": ["invalid"]}, {"contacted_within": "90d"},
                       {"contacted_since": "2026-01-01"}, {"has_email": "0"},
                       {"has_sales_nav": "1"}, {"has_linkedin": "0"}):
            where, params = dq.lead_filter_clause(WS, **kwargs)
            conn.execute(
                f"SELECT COUNT(*) FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id"
                f" LEFT JOIN companies co ON co.id = l.company_id WHERE {where}", params)
        conn.close()

    def test_excluding_a_sentiment_keeps_leads_that_have_none(self):
        """NULL <> 'negative' is NULL, so a bare comparison silently drops every
        lead that never replied -- which is most of the list."""
        where, _ = dq.lead_filter_clause(WS, sentiment_none=["negative"])
        self.assertIn("COALESCE", where)


class HeadcountBandTests(_DbCase):
    def test_bands_cover_the_boundaries(self):
        conn = om.get_conn()
        sql = dq.headcount_band_sql("?")
        for value, expected in ((1, "1"), (2, "2-10"), (10, "2-10"), (11, "11-50"),
                                (200, "51-200"), (201, "201-500"), (5000, "1001-5000"),
                                (10001, "10001+"), (999999, "10001+")):
            got = conn.execute(f"SELECT {sql} AS band", (value,) * sql.count("?")).fetchone()
            self.assertEqual(got["band"], expected, value)
        conn.close()

    def test_bands_sort_by_size_not_alphabetically(self):
        """'1001-5000' sorts before '11-50' as text, which shuffles the sizes."""
        conn = om.get_conn()
        order = dq.headcount_band_order_sql("?")
        n = order.count("?")
        small = conn.execute(f"SELECT {order} AS o", (50,) * n).fetchone()["o"]
        large = conn.execute(f"SELECT {order} AS o", (5000,) * n).fetchone()["o"]
        conn.close()
        self.assertLess(small, large)


class HygieneBackfillTests(_DbCase):
    def test_cleanup_commands_default_to_a_dry_run(self):
        for fn in (data_hygiene.cleanup_sentinels, data_hygiene.cleanup_timestamps):
            result = fn()
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["updated"], 0)


if __name__ == "__main__":
    unittest.main()


class AttributionReconciliationTests(_DbCase):
    """The Campaigns table and the Replies list must count the same set.

    They used to disagree: the table resolved a lead's last known campaign
    (falling back to a colleague's at the same company), the list filtered on
    raw campaign_leads membership. On live data those differed by up to 5x for
    a single campaign.
    """

    def _setup_workspace(self):
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.execute("INSERT INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
                     (WS, "default", WS, "Acme"))
        company = conn.execute(
            "INSERT INTO companies (name, domain) VALUES (?,?)", ("Acme", "acme.test")).lastrowid
        conn.commit()
        return conn, company

    def _lead(self, conn, company, name):
        lead_id = conn.execute("INSERT INTO leads (name, company_id) VALUES (?,?)",
                               (name, company)).lastrowid
        conn.execute("INSERT INTO workspace_leads (workspace_id, org_id, lead_id, status)"
                     " VALUES (?,?,?,?)", (WS, "default", lead_id, "contacted"))
        conn.commit()
        return lead_id

    def _event(self, conn, lead_id, etype, direction, at, campaign_id):
        event_id = conn.execute(
            "INSERT INTO events (lead_id, event_type, direction, campaign_id, metadata_json,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (lead_id, etype, direction, campaign_id,
             json.dumps({"platform": "plusvibe"}), at)).lastrowid
        conn.execute(
            "INSERT INTO workspace_lead_events (workspace_id, org_id, lead_id, event_id,"
            " event_type, event_at, idempotency_key) VALUES (?,?,?,?,?,?,?)",
            (WS, "default", lead_id, event_id, etype, at, f"{WS}:{event_id}"))
        conn.commit()

    def test_a_lead_in_two_campaigns_is_counted_by_the_one_that_reached_them(self):
        conn, company = self._setup_workspace()
        reached = conn.execute("INSERT INTO campaigns (name) VALUES (?)", ("reached",)).lastrowid
        also_in = conn.execute("INSERT INTO campaigns (name) VALUES (?)", ("also_in",)).lastrowid
        lead = self._lead(conn, company, "Replier")
        # Membership of both; only one actually sent to them.
        for cid in (reached, also_in):
            conn.execute("INSERT INTO campaign_leads (campaign_id, lead_id) VALUES (?,?)",
                         (cid, lead))
        self._event(conn, lead, "email_sent", "outbound", "2026-01-02 09:00:00", reached)
        self._event(conn, lead, "email_reply", "inbound", "2026-01-04 10:00:00", reached)
        conn.execute("UPDATE workspace_leads SET current_status_sentiment = 'positive',"
                     " current_sentiment_since = '2026-01-04 10:00:00' WHERE lead_id = ?", (lead,))
        conn.commit()

        table = {c["id"]: c for c in dq.campaign_audit(conn, WS, since="all")["campaigns"]}

        def listed(campaign_id):
            return len(dq.campaign_replies(conn, WS, campaign_id=campaign_id, since="all",
                                           sentiment="positive", limit=100)["replies"])

        # The campaign that reached them: table and list agree, and both say 1.
        self.assertEqual(table[reached]["positive"], 1)
        self.assertEqual(listed(reached), 1)
        # The campaign they were merely a member of produced no events, so it is
        # not a row in the table at all -- and the list must agree by returning
        # nothing. Under raw membership it returned this lead, which is exactly
        # how one reply inflated every campaign that ever touched them.
        self.assertEqual(table.get(also_in, {}).get("positive", 0), 0)
        self.assertEqual(listed(also_in), 0)
        self.assertEqual(
            listed(also_in) + 1,
            len(dq.campaign_replies(conn, WS, campaign_id=also_in, since="all",
                                    sentiment="positive", limit=100,
                                    attribution="membership")["replies"]),
            "membership mode should still return the lead -- it is the default that changed")
        conn.close()


class FirstPositiveTests(_DbCase):
    def test_it_survives_the_lead_going_cold(self):
        """current_sentiment_since drops a lead that later cools, which is what
        made a past month's number change when you looked at it again."""
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.execute("INSERT INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
                     (WS, "default", WS, "Acme"))
        lead_id = conn.execute("INSERT INTO leads (name) VALUES (?)", ("Cooled",)).lastrowid
        conn.execute(
            "INSERT INTO workspace_leads (workspace_id, org_id, lead_id, status,"
            " current_status_sentiment, current_sentiment_since, first_positive_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (WS, "default", lead_id, "contacted", "negative",
             "2026-03-01 00:00:00", "2026-01-15 00:00:00"))
        conn.commit()
        # January, on each anchor.
        window = dict(since="2026-01-01", until="2026-01-31")
        self.assertEqual(dq.positive_count_in_range(conn, WS, anchor="first", **window), 1)
        self.assertEqual(dq.positive_count_in_range(conn, WS, anchor="current", **window), 0)
        conn.close()


class AttributionPlanTests(_DbCase):
    def test_the_attributed_set_is_a_relation_not_a_literal_list(self):
        """A literal IN list is opaque to the query planner.

        Inlining the resolved ids stopped SQLite driving campaign_replies from
        the narrow lead set and it fell back to scanning: one campaign's replies
        went from 1.4s to 926s against the live database. Materializing the ids
        into an indexed TEMP table restores the plan. This asserts the shape,
        which is the thing that is easy to "simplify" back into a bug.
        """
        import inspect

        source = inspect.getsource(dq.campaign_replies)
        self.assertIn("_attributed_leads", source)
        self.assertNotIn('IN (%s)" % (",".join(str(int(i)) for i in ids)', source)
