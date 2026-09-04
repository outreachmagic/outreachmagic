"""A meeting booked in the window belongs on the Leads list.

Reported as "one day says 1 meeting booked and 0 replies, and the list is
empty -- but yesterday shows leads with only a meeting". Both observations were
correct and had one cause: the list anchors a lead on *when its sentiment
started* (current_sentiment_since), while the daily matrix counts meetings on
*event date*. A lead that turned positive in July and booked today is counted
in today's meeting column and falls outside today's list window. Yesterday's
lead happened to turn positive the same day it booked, so it appeared.

Two clocks, one date picker. This adds the third anchor branch so a meeting in
range is activity in range, whatever the sentiment has said since July.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import reporting_queries  # noqa: E402

DAY = "2026-09-04"
LONG_AGO = "2026-07-17 13:44:38"


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _ws():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    return conn, om.resolve_workspace_identity(conn, "storefront")["id"]


def _lead(conn, ws, name, *, sentiment=None, since=None):
    lead = om.resolve_lead(name=name, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET email = ? WHERE id = ?",
                 (f"lead{lead['id']}@acme.com", lead["id"]))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws, lead["id"])
    if sentiment:
        conn.execute(
            """UPDATE workspace_leads
                  SET current_status_sentiment = ?, current_sentiment_since = ?,
                      current_status_label = 'interested'
                WHERE workspace_id = ? AND lead_id = ?""",
            (sentiment, since, ws, lead["id"]))
    return lead["id"]


def _event(conn, ws, lead_id, event_type, at, *, direction="inbound"):
    cur = conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, created_at)
           VALUES (?, ?, ?, 'email', ?)""",
        (lead_id, event_type, direction, at))
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO workspace_lead_events
               (org_id, workspace_id, lead_id, event_id, event_type, event_at,
                idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (om.DEFAULT_ORG_ID, ws, lead_id, eid, event_type, at, f"idem_{eid}"))
    return eid


def _names(conn, ws, **kw):
    res = reporting_queries.campaign_replies(
        conn, ws, since=DAY, until=DAY, **kw)
    return {r["lead_name"] for r in res["replies"]}, res


def test_meeting_in_range_lists_a_lead_positive_since_july():
    """The reported bug, exactly."""
    conn, ws = _ws()
    lead = _lead(conn, ws, "Mira Booked", sentiment="positive", since=LONG_AGO)
    _event(conn, ws, lead, "meeting_booked", f"{DAY} 02:15:43")
    conn.commit()
    names, res = _names(conn, ws)
    assert "Mira Booked" in names
    row = next(r for r in res["replies"] if r["lead_name"] == "Mira Booked")
    assert row["reply_kind"] == "meeting"
    # The sentiment anchor is untouched -- it still says July, truthfully.
    assert row["sentiment"] == "positive"
    conn.close()


def test_meetings_only_filter():
    conn, ws = _ws()
    booked = _lead(conn, ws, "Mira Booked", sentiment="positive", since=LONG_AGO)
    _event(conn, ws, booked, "meeting_booked", f"{DAY} 02:15:43")
    replier = _lead(conn, ws, "Rex Replied", sentiment="positive", since=f"{DAY} 09:00:00")
    _event(conn, ws, replier, "email_reply", f"{DAY} 09:00:00")
    conn.commit()

    assert _names(conn, ws, reply_kind="meeting")[0] == {"Mira Booked"}
    assert "Mira Booked" not in _names(conn, ws, reply_kind="human")[0]
    assert _names(conn, ws)[0] == {"Mira Booked", "Rex Replied"}
    conn.close()


def test_reply_in_range_still_lists_unchanged():
    """Regression guard on CTE parameter order -- get it wrong and this query
    silently returns the wrong rows rather than erroring."""
    conn, ws = _ws()
    lead = _lead(conn, ws, "Rex Replied", sentiment="positive", since=f"{DAY} 09:00:00")
    _event(conn, ws, lead, "email_reply", f"{DAY} 09:00:00")
    conn.commit()
    assert _names(conn, ws)[0] == {"Rex Replied"}
    conn.close()


def test_meeting_outside_the_range_does_not_list():
    conn, ws = _ws()
    lead = _lead(conn, ws, "Old Booking", sentiment="positive", since=LONG_AGO)
    _event(conn, ws, lead, "meeting_booked", "2026-08-01 10:00:00")
    conn.commit()
    assert _names(conn, ws)[0] == set()
    conn.close()


def test_list_reconciles_with_the_daily_meeting_count():
    """The assertion that closes the ticket: the two clocks must agree."""
    conn, ws = _ws()
    for i, since in enumerate([LONG_AGO, f"{DAY} 08:00:00", "2026-08-14 18:25:41"]):
        lead = _lead(conn, ws, f"Booker {i}", sentiment="positive", since=since)
        _event(conn, ws, lead, "meeting_booked", f"{DAY} 0{i + 1}:00:00")
    conn.commit()

    listed = _names(conn, ws, reply_kind="meeting")[0]
    daily = reporting_queries.campaign_daily(conn, ws, since=DAY, until=DAY)
    day_row = next((d for d in daily["days"] if d["date"] == DAY), {})
    assert len(listed) == day_row.get("meetings"), (listed, day_row)
    conn.close()
