"""Excluding a campaign that is not outreach.

The derivation only calls a campaign a booking tool when *every* event on it
came from one, so a genuine multi-event campaign that is nonetheless not
outreach -- a customer check-in sequence -- correctly fails that test. It was
not misclassified, it was unclassified: there was nowhere to record the
judgement. `campaigns.reporting_role` is that place.

The promise being made is that an excluded campaign's events still ingest and
still show on the lead. `test_events_stay_on_the_lead` asserts it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import reporting_queries as rq  # noqa: E402

DAY = "2026-09-04"


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed():
    """Two campaigns: one real outbound, one customer check-in to be excluded."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")["id"]
    ids = {}
    for name in ("Acme Outbound Q3", "Acme Quarterly Check-In"):
        cur = conn.execute(
            "INSERT INTO campaigns (name, workspace_id) VALUES (?, ?)", (name, ws))
        ids[name] = cur.lastrowid
    for tag, name, n in (("Out", "Acme Outbound Q3", 2),
                         ("Chk", "Acme Quarterly Check-In", 2)):
        for i in range(n):
            lead = om.resolve_lead(name=f"{tag} Person{i}", source="csv",
                                   allow_weak_identity=True, conn=conn)
            conn.execute("UPDATE leads SET email = ? WHERE id = ?",
                         (f"lead{lead['id']}@acme.com", lead["id"]))
            om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws, lead["id"])
            conn.execute(
                "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
                (ids[name], lead["id"]))
            conn.execute(
                """UPDATE workspace_leads
                      SET current_status_sentiment = 'positive',
                          current_sentiment_since = ?, current_status_label = 'interested'
                    WHERE workspace_id = ? AND lead_id = ?""",
                (f"{DAY} 09:00:00", ws, lead["id"]))
            for et, direction in (("email_sent", "outbound"), ("meeting_booked", "inbound")):
                cur = conn.execute(
                    """INSERT INTO events (lead_id, event_type, direction, channel,
                                           campaign_id, created_at)
                       VALUES (?, ?, ?, 'email', ?, ?)""",
                    (lead["id"], et, direction, ids[name], f"{DAY} 09:00:00"))
                conn.execute(
                    """INSERT INTO workspace_lead_events
                           (org_id, workspace_id, lead_id, event_id, event_type,
                            event_at, idempotency_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (om.DEFAULT_ORG_ID, ws, lead["id"], cur.lastrowid, et,
                     f"{DAY} 09:00:00", f"idem_{cur.lastrowid}"))
    conn.commit()
    return conn, ws, ids


def _exclude(conn, ids):
    rq.set_campaign_reporting_role(conn, ids["Acme Quarterly Check-In"], "excluded")


def test_excluded_campaign_leaves_the_campaigns_table():
    conn, ws, ids = _seed()
    before = {c["name"] for c in rq.campaign_audit(conn, ws, since=DAY, until=DAY)["campaigns"]}
    assert "Acme Quarterly Check-In" in before

    _exclude(conn, ids)
    after = rq.campaign_audit(conn, ws, since=DAY, until=DAY)
    assert "Acme Quarterly Check-In" not in {c["name"] for c in after["campaigns"]}
    assert "Acme Outbound Q3" in {c["name"] for c in after["campaigns"]}
    assert after["role_excluded"] == 1
    conn.close()


def test_excluded_meetings_are_dropped_not_reattributed():
    """The scheduling exclusion re-attributes a booking; this must not."""
    conn, ws, ids = _seed()
    _exclude(conn, ids)
    audit = rq.campaign_audit(conn, ws, since=DAY, until=DAY)
    total_meetings = sum(c["meetings"] for c in audit["campaigns"])
    assert total_meetings == 2, "only the outbound campaign's meetings should count"

    daily = rq.campaign_daily(conn, ws, since=DAY, until=DAY)
    day = next(d for d in daily["days"] if d["date"] == DAY)
    assert day["meetings"] == 2
    conn.close()


def test_include_excluded_restores_every_number():
    conn, ws, ids = _seed()
    baseline = rq.campaign_audit(conn, ws, since=DAY, until=DAY)
    _exclude(conn, ids)
    restored = rq.campaign_audit(conn, ws, since=DAY, until=DAY, include_excluded=True)
    assert restored["totals"] == baseline["totals"]
    conn.close()


def test_excluded_leads_leave_the_leads_list():
    conn, ws, ids = _seed()
    _exclude(conn, ids)
    names = {r["lead_name"] for r in
             rq.campaign_replies(conn, ws, since=DAY, until=DAY)["replies"]}
    assert not any(n.startswith("Chk") for n in names), names
    assert any(n.startswith("Out") for n in names), names
    conn.close()


def test_selecting_the_excluded_campaign_still_shows_it():
    """Naming it is an unambiguous request to see it."""
    conn, ws, ids = _seed()
    _exclude(conn, ids)
    res = rq.campaign_replies(
        conn, ws, since=DAY, until=DAY,
        campaign_id=ids["Acme Quarterly Check-In"], attribution="membership")
    assert res["replies"], "an explicitly selected excluded campaign must still list"
    conn.close()


def test_events_stay_on_the_lead():
    """The promise: excluded from the stats, not deleted from the record."""
    conn, ws, ids = _seed()
    _exclude(conn, ids)
    lead_id = conn.execute(
        "SELECT lead_id FROM campaign_leads WHERE campaign_id = ?",
        (ids["Acme Quarterly Check-In"],)).fetchone()["lead_id"]
    n = conn.execute(
        "SELECT COUNT(*) n FROM workspace_lead_events WHERE lead_id = ?",
        (lead_id,)).fetchone()["n"]
    assert n == 2
    conn.close()


def test_role_auto_restores_derivation():
    conn, ws, ids = _seed()
    _exclude(conn, ids)
    assert rq.excluded_campaign_ids(conn) == {ids["Acme Quarterly Check-In"]}
    rq.set_campaign_reporting_role(conn, ids["Acme Quarterly Check-In"], "auto")
    assert rq.excluded_campaign_ids(conn) == set()
    conn.close()


def test_unknown_role_raises_rather_than_filtering_to_nothing():
    conn, ws, ids = _seed()
    with pytest.raises(ValueError, match="unknown reporting role"):
        rq.set_campaign_reporting_role(conn, ids["Acme Outbound Q3"], "ignored")
    conn.close()


def test_manual_outbound_beats_the_scheduling_derivation():
    conn, ws, ids = _seed()
    cid = ids["Acme Outbound Q3"]
    # Make every event on it look like a booking tool, so the derivation would
    # otherwise classify it as a calendar.
    conn.execute(
        """UPDATE events SET metadata_json = json_object('platform', 'calendly')
            WHERE campaign_id = ?""", (cid,))
    conn.commit()
    assert cid in rq.scheduling_campaign_ids(conn)
    rq.set_campaign_reporting_role(conn, cid, "outbound")
    assert cid not in rq.scheduling_campaign_ids(conn)
    conn.close()
