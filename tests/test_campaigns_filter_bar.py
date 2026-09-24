"""The Campaigns tab asks each filter question exactly once.

It used to ask "which campaign" in three cards, with three independent answers,
and `loadCampaigns` apologised in prose that "the daily chart above is
unfiltered". These are cheap structural guards so a later merge cannot
reintroduce the duplicate controls, plus a server-side check that the chart and
the table actually agree once they are given the same filter.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import reporting_queries as rq  # noqa: E402

HTML = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
DAY = "2026-09-04"


@pytest.mark.parametrize("dead_id", [
    "daily-campaign",     # the chart's own campaign picker
    "replies-campaign",   # the leads list's own campaign picker
    "replies-sentiment",
    "replies-status",
    "replies-kind",
    "campaigns-kind",         # the table's own kind picker
    "campaigns-scheduling",   # the table's own scheduling toggle
])
def test_duplicate_filter_controls_are_gone(dead_id):
    assert f'id="{dead_id}"' not in HTML, (
        f"{dead_id} is a per-card copy of a filter that now lives in the tab's "
        "filter bar; two copies is how the answers drift apart")


def test_the_filter_bar_exists_with_one_of_each():
    for wanted in ("cf-campaign", "cf-kind", "cf-sentiment", "cf-status",
                   "cf-scheduling", "cf-excluded", "cf-clear"):
        assert HTML.count(f'id="{wanted}"') == 1, wanted


def test_positives_anchor_stays_with_its_own_table():
    """It selects how a column is dated, not which leads are in scope."""
    assert 'id="campaigns-anchor"' in HTML


def test_no_stale_helper_references():
    """A control removed with its markup must take its readers with it."""
    for dead in ("repliesKind(", '$("replies-kind")', '$("campaigns-kind")'):
        assert dead not in HTML, dead


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")["id"]
    cid = conn.execute(
        "INSERT INTO campaigns (name, workspace_id) VALUES ('Acme Outbound Q3', ?)",
        (ws,)).lastrowid
    for i in range(3):
        lead = om.resolve_lead(name=f"Person {i}", source="csv",
                               allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET email = ? WHERE id = ?",
                     (f"lead{lead['id']}@acme.com", lead["id"]))
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws, lead["id"])
        if i == 0:
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, 'existing_customer')""",
                (f"wlt_{lead['id']}", ws, lead["id"]))
        cur = conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel,
                                   campaign_id, created_at)
               VALUES (?, 'email_sent', 'outbound', 'email', ?, ?)""",
            (lead["id"], cid, f"{DAY} 09:00:00"))
        conn.execute(
            """INSERT INTO workspace_lead_events
                   (org_id, workspace_id, lead_id, event_id, event_type,
                    event_at, idempotency_key)
               VALUES (?, ?, ?, ?, 'email_sent', ?, ?)""",
            (om.DEFAULT_ORG_ID, ws, lead["id"], cur.lastrowid, f"{DAY} 09:00:00",
             f"idem_{cur.lastrowid}"))
    conn.commit()
    return conn, ws, cid


def _sent(daily):
    return sum(d.get("email_sent", 0) for d in daily["days"])


def test_subtitle_no_longer_exempts_kind_from_the_chart():
    """The chart used to say kind and sentiment 'apply to the tables below'."""
    assert "kind and sentiment apply to the tables below" not in HTML
    assert "outcomeParams" in HTML


def test_chart_and_table_agree_under_the_same_filter():
    """One bar, one population -- the chart used to ignore the tag filter."""
    conn, ws, cid = _seed()
    audit = rq.campaign_audit(conn, ws, since=DAY, until=DAY)
    daily = rq.campaign_daily(conn, ws, since=DAY, until=DAY)
    assert _sent(daily) == audit["totals"]["sent"] == 3

    tagged_audit = rq.campaign_audit(
        conn, ws, since=DAY, until=DAY, tags_none=["existing_customer"])
    tagged_daily = rq.campaign_daily(
        conn, ws, since=DAY, until=DAY, tags_none=["existing_customer"])
    assert tagged_audit["totals"]["sent"] == 2
    assert _sent(tagged_daily) == 2, "the chart must honour the tag filter too"
    conn.close()
