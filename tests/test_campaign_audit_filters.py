#!/usr/bin/env python3
"""Campaign audit: the reply-kind filter and the tag filter, and their reach.

Two filters with deliberately different reach, and the reach is the point:

  * a TAG describes the lead, so it narrows every column including `sent` --
    excluding a tag from the numerator but not the denominator would report a
    reply rate for a population that does not exist;
  * a KIND describes the reply, so it narrows the reply and sentiment columns
    only. Filtering sends by "this lead's last reply was human" collapses the
    denominator and turns every rate into fiction.
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
import pipeline_tags  # noqa: E402
import reporting_queries as dq  # noqa: E402
from pipeline_migration import _backfill_first_positive_at  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

WS = {}
CAMPAIGN = "alpha | outbound q3"


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


def _send(lead_id, subject="Quick question"):
    lead_actions.log_event_scoped(
        lead_id, "email_sent", workspace_slug="alpha", subject=subject,
        metadata={"campaign": CAMPAIGN})


def _reply(lead_id, *, auto=False):
    meta = {"campaign": CAMPAIGN}
    if auto:
        meta["is_auto_reply"] = 1
    lead_actions.log_event_scoped(
        lead_id, "email_reply", workspace_slug="alpha", direction="inbound",
        subject="Re: Quick question", metadata=meta)


def _sentiment(lead_id, sentiment, stage="interested"):
    lead_actions.change_stage_scoped(
        lead_id, stage, workspace_slug="alpha", sentiment=sentiment)


def _materialize_first_positive():
    """first_positive_at is written by relay ingest or its one-shot backfill,
    never by a local stage change -- so seed it the way production does."""
    conn = om.get_conn()
    try:
        _backfill_first_positive_at(conn)
        conn.commit()
    finally:
        conn.close()


def _audit(**kw):
    conn = om.get_conn()
    try:
        return dq.campaign_audit(conn, WS["alpha"], **kw)
    finally:
        conn.close()


def _row(payload):
    rows = [c for c in payload["campaigns"] if c["name"] == CAMPAIGN]
    return rows[0] if rows else None


def _seed_two_kinds():
    """One human replier who went positive, one whose only 'reply' is a status.

    Both were sent to, so `sent` is 2 whatever the kind filter says.
    """
    human = _lead("Hank Human", "hank@h-example.com")
    silent = _lead("Sam Status", "sam@s-example.com")
    _send(human)
    _send(silent)
    _reply(human)
    _sentiment(human, "positive")
    # No reply event at all -- a sequencer status change is what made this lead
    # positive, which is exactly the `status_only` kind.
    _sentiment(silent, "positive")
    return human, silent


# -- kind ------------------------------------------------------------------

def test_kind_narrows_positives_but_never_sends():
    _seed_two_kinds()
    everything = _row(_audit())
    assert everything["sent"] == 2
    assert everything["positive"] == 2

    human_only = _row(_audit(reply_kind="human"))
    assert human_only["positive"] == 1, "the status-only lead drops out"
    assert human_only["sent"] == 2, "a kind filter must never shrink the denominator"
    assert human_only["replies"] == 1


def test_kind_holds_on_the_first_positive_anchor():
    """The anchor the campaigns page actually defaults to.

    The first-positive branch is a separate query; forgetting the filter there
    makes --kind silently revert to unfiltered on the default view.
    """
    _seed_two_kinds()
    _materialize_first_positive()
    assert _row(_audit(positive_anchor="first"))["positive"] == 2
    assert _row(_audit(positive_anchor="first", reply_kind="human"))["positive"] == 1


def test_kind_separates_auto_replies_from_humans():
    human = _lead("Hank Human", "hank@h-example.com")
    robot = _lead("Ora Ooo", "ora@o-example.com")
    _send(human)
    _send(robot)
    _reply(human)
    _reply(robot, auto=True)
    _sentiment(human, "positive")
    _sentiment(robot, "positive", stage="contacted")

    assert _row(_audit(reply_kind="human"))["positive"] == 1
    assert _row(_audit(reply_kind="auto_reply"))["positive"] == 1
    assert _row(_audit(reply_kind="human,auto_reply"))["positive"] == 2
    assert _row(_audit(reply_kind="all"))["positive"] == 2


def test_unknown_kind_raises_rather_than_returning_an_empty_table():
    _seed_two_kinds()
    with pytest.raises(ValueError) as exc:
        _audit(reply_kind="reply")
    assert "unknown reply kind" in str(exc.value)


# -- tags ------------------------------------------------------------------

def test_excluded_tag_leaves_every_column_including_sends():
    keep = _lead("Ken Keep", "ken@k-example.com")
    drop = _lead("Cus Tomer", "cus@c-example.com")
    _send(keep)
    _send(drop)
    _reply(keep)
    _reply(drop)
    _sentiment(keep, "positive")
    _sentiment(drop, "positive")
    pipeline_tags.tag_add(WS["alpha"], drop, "existing_customer")

    before = _row(_audit())
    assert (before["sent"], before["replies"], before["positive"]) == (2, 2, 2)

    after = _row(_audit(tags_none=["existing_customer"]))
    assert after["sent"] == 1, "a tag filter reaches the raw event columns too"
    assert after["replies"] == 1
    assert after["positive"] == 1
    assert after["reply_rate"] == 1.0


def test_tags_any_and_all_agree_with_the_contacts_filter():
    """Same tag semantics on both pages -- both go through lead_filter_clause."""
    ab = _lead("Both AB", "ab@x-example.com")
    a = _lead("Only A", "a@x-example.com")
    for lead_id in (ab, a):
        _send(lead_id)
        pipeline_tags.tag_add(WS["alpha"], lead_id, "alpha_tag")
    pipeline_tags.tag_add(WS["alpha"], ab, "beta_tag")

    assert _row(_audit(tags_any=["alpha_tag"]))["sent"] == 2
    assert _row(_audit(tags_all=["alpha_tag", "beta_tag"]))["sent"] == 1
    assert _row(_audit(tags_any=["beta_tag"]))["sent"] == 1

    conn = om.get_conn()
    try:
        found = dq.search_leads(conn, WS["alpha"], tags_all=["alpha_tag", "beta_tag"])
    finally:
        conn.close()
    assert [r["name"] for r in found["leads"]] == ["Both AB"]


def test_tag_filter_does_not_inherit_the_contacts_defaults():
    """It is a TAG filter, not the contacts list.

    lead_filter_clause hides test leads, company placeholders and suppressed
    leads by default. Inheriting that here would make picking one tag silently
    drop them out of every campaign column -- a filter nobody asked for,
    applied by a filter they did.
    """
    lead_id = _lead("Tes Ter", "tes@t-example.com")
    _send(lead_id)
    pipeline_tags.tag_add(WS["alpha"], lead_id, "keepme")
    conn = om.get_conn()
    try:
        conn.execute("UPDATE leads SET is_test = 1 WHERE id = ?", (lead_id,))
        conn.commit()
    finally:
        conn.close()
    assert _row(_audit(tags_any=["keepme"]))["sent"] == 1


def test_filters_compose():
    human = _lead("Hank Human", "hank@h-example.com")
    tagged = _lead("Cus Tomer", "cus@c-example.com")
    _send(human)
    _send(tagged)
    _reply(human)
    _reply(tagged)
    _sentiment(human, "positive")
    _sentiment(tagged, "positive")
    pipeline_tags.tag_add(WS["alpha"], tagged, "existing_customer")
    row = _row(_audit(reply_kind="human", tags_none=["existing_customer"]))
    assert (row["sent"], row["replies"], row["positive"]) == (1, 1, 1)


def test_payload_states_what_it_counted():
    _seed_two_kinds()
    payload = _audit(reply_kind="human", tags_none=["existing_customer"])
    assert payload["reply_kind"] == "human"
    assert payload["tags_none"] == ["existing_customer"]
    assert "positive" in payload["kind_scoped_columns"]
    assert "sent" not in payload["kind_scoped_columns"]


def test_kind_scopes_exactly_the_columns_the_payload_claims():
    """The contract, pinned: --kind narrows KIND_SCOPED_COLUMNS and nothing else.

    Believed true and never asserted as a whole. The failure it guards against
    is silent and bad in both directions: a kind filter reaching `sent` would
    collapse every denominator, and one failing to reach a sentiment column
    would report human positives that include auto-replies.
    """
    _seed_two_kinds()
    everything = _row(_audit())
    human = _row(_audit(reply_kind="human"))

    untouched = [k for k in everything if k not in dq.KIND_SCOPED_COLUMNS
                 and isinstance(everything[k], (int, float))]
    for column in untouched:
        assert human[column] == everything[column], (
            f"--kind changed `{column}`, which is not in kind_scoped_columns")

    # And it must actually bite on the columns it claims: the status-only lead
    # is positive with no human reply behind it.
    assert everything["positive"] == 2
    assert human["positive"] == 1
    assert human["sent"] == everything["sent"] == 2


def test_kind_reaches_the_by_day_replies_too():
    """The daily array is the same query grouped by day; a filter that applied
    to one and not the other would put two populations on one screen."""
    _seed_two_kinds()
    for kind in ("all", "human"):
        row = _row(_audit(reply_kind=kind, since="30d", by_day=True))
        assert sum(d["replies"] for d in row["daily"]) == row["replies"]
        assert sum(d["sent"] for d in row["daily"]) == row["sent"]
