#!/usr/bin/env python3
"""The all-campaigns row.

Two properties, both of which have been got wrong on tables like this before:

  * the sums are the row sums -- one definition of the total, computed server
    side, so the dashboard footer, the CLI's TOTAL line and an agent reading the
    JSON cannot each arrive at a different number;
  * the rates are WEIGHTED (sum/sum), never the mean of the per-row rates. A
    campaign with 2 sends and 1 reply would otherwise drag a 50% into an average
    that describes nobody.
"""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import lead_actions  # noqa: E402
import pipeline as om  # noqa: E402
import reporting_queries as dq  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

WS = {}


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    conn = om.get_conn()
    WS["alpha"] = om.resolve_workspace_identity(conn, "alpha")["id"]
    conn.close()


def _lead(name, email):
    return om.add_lead(name, company="Acme Corp", email=email)["id"]


def _send(lead_id, campaign):
    lead_actions.log_event_scoped(
        lead_id, "email_sent", workspace_slug="alpha", subject="Hi",
        metadata={"campaign": campaign})


def _reply(lead_id, campaign):
    lead_actions.log_event_scoped(
        lead_id, "email_reply", workspace_slug="alpha", direction="inbound",
        subject="Re: Hi", metadata={"campaign": campaign})


def _audit(**kw):
    conn = om.get_conn()
    try:
        return dq.campaign_audit(conn, WS["alpha"], **kw)
    finally:
        conn.close()


def _seed_two_campaigns():
    """Big campaign: 4 sends, 1 reply. Small: 2 sends, 1 reply.

    Weighted reply rate is 2/6 = 33.3%. The mean of the row rates would be
    (25% + 50%) / 2 = 37.5% -- the wrong answer this test exists to reject.
    """
    for i in range(4):
        lead_id = _lead(f"Big {i}", f"big{i}@x-example.com")
        _send(lead_id, "alpha | big")
        if i == 0:
            _reply(lead_id, "alpha | big")
            lead_actions.change_stage_scoped(
                lead_id, "interested", workspace_slug="alpha", sentiment="positive")
    for i in range(2):
        lead_id = _lead(f"Small {i}", f"small{i}@x-example.com")
        _send(lead_id, "alpha | small")
        if i == 0:
            _reply(lead_id, "alpha | small")


def test_totals_are_the_row_sums():
    _seed_two_campaigns()
    payload = _audit()
    rows, totals = payload["campaigns"], payload["totals"]
    assert totals["campaigns"] == len(rows) == 2
    for key in ("sent", "replies", "bounces", "meetings", "positive",
                "negative", "invalid", "autoreply", "email_sent"):
        assert totals[key] == sum(r[key] for r in rows), key


def test_rates_are_weighted_not_averaged():
    _seed_two_campaigns()
    totals = _audit()["totals"]
    assert totals["sent"] == 6 and totals["replies"] == 2
    assert totals["reply_rate"] == round(2 / 6, 4)
    mean_of_rates = sum(r["reply_rate"] for r in _audit()["campaigns"]) / 2
    assert totals["reply_rate"] != round(mean_of_rates, 4)


def test_totals_follow_the_filters():
    _seed_two_campaigns()
    assert _audit()["totals"]["sent"] == 6
    # A kind nothing matches empties the sentiment columns and leaves sends whole.
    filtered = _audit(reply_kind="bounce")["totals"]
    assert filtered["sent"] == 6
    assert filtered["positive"] == 0
    assert filtered["replies"] == 0


def test_an_empty_workspace_totals_to_zero_not_an_error():
    totals = _audit()["totals"]
    assert totals["campaigns"] == 0
    assert totals["sent"] == 0
    assert totals["reply_rate"] is None


def test_rates_are_none_rather_than_zero_when_nothing_was_sent():
    """A campaign that only ever received (no sends) has no rate to report.

    Reporting 0% would read as "we sent and nobody replied".
    """
    lead_id = _lead("Inbound Only", "in@x-example.com")
    _reply(lead_id, "alpha | inbound")
    totals = _audit()["totals"]
    assert totals["sent"] == 0
    assert totals["reply_rate"] is None
    assert totals["bounce_rate"] is None
