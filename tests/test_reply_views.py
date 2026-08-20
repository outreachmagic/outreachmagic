#!/usr/bin/env python3
"""The read-only views an agent selects from instead of re-deriving reply SQL.

The failure this closes, reported from the field: the reply condition is an OR
nested inside an AND. Hand-written it loses its parentheses, so

    WHERE workspace_id = ? AND event_type = 'email_reply' OR direction = 'inbound'
      AND event_at >= '2026-08-01'

binds the date to one branch only and the query silently returns all-time
totals in every time window. v_reply_events applies and parenthesizes the
condition INSIDE the view, so the caller writes a flat AND chain and the bug
cannot be expressed.
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
import schema_views  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402

WS = {}
CAMPAIGN = "alpha | outbound"


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Team Alpha", slug="alpha")
    om.create_workspace("Team Beta", slug="beta")
    conn = om.get_conn()
    for slug in ("alpha", "beta"):
        WS[slug] = om.resolve_workspace_identity(conn, slug)["id"]
    conn.close()


def _reply(name, email, *, workspace="alpha", auto=False, at=None):
    lead_id = om.add_lead(name, company="Acme", email=email)["id"]
    meta = {"campaign": CAMPAIGN}
    if auto:
        meta["is_auto_reply"] = 1
    lead_actions.log_event_scoped(
        lead_id, "email_reply", workspace_slug=workspace, direction="inbound",
        subject="Re: Hi", metadata=meta)
    if at:
        conn = om.get_conn()
        try:
            conn.execute(
                "UPDATE workspace_lead_events SET event_at = ? WHERE lead_id = ?",
                (at, lead_id))
            conn.execute(
                "UPDATE events SET created_at = ? WHERE lead_id = ?", (at, lead_id))
            conn.commit()
        finally:
            conn.close()
    return lead_id


def _send(name, email):
    lead_id = om.add_lead(name, company="Acme", email=email)["id"]
    lead_actions.log_event_scoped(
        lead_id, "email_sent", workspace_slug="alpha", subject="Hi",
        metadata={"campaign": CAMPAIGN})
    return lead_id


def _rows(sql, params=()):
    conn = om.get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_the_views_exist_after_init():
    names = {r["name"] for r in _rows(
        "SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert {"v_reply_events", "v_lead_sentiment",
            "v_inbound_events_by_campaign"} <= names


def test_a_flat_and_chain_is_enough_to_scope_a_window():
    """The regression test. Two replies, one in range: the view returns one."""
    _reply("Old Reply", "old@x-example.com", at="2026-01-05 10:00:00")
    _reply("New Reply", "new@x-example.com", at="2026-08-05 10:00:00")
    _send("Just Sent", "sent@x-example.com")
    rows = _rows(
        """SELECT lead_id FROM v_reply_events
            WHERE workspace_id = ? AND event_at >= ? AND event_at < ?""",
        (WS["alpha"], "2026-08-01", "2026-09-01"))
    assert len(rows) == 1


def test_the_view_holds_replies_only_never_sends():
    _reply("A Replier", "a@x-example.com")
    _send("B Sender", "b@x-example.com")
    types = {r["event_type"] for r in _rows(
        "SELECT event_type FROM v_reply_events WHERE workspace_id = ?",
        (WS["alpha"],))}
    assert types == {"email_reply"}


def test_the_view_is_workspace_scoped():
    _reply("Alpha Person", "alpha@x-example.com", workspace="alpha")
    _reply("Beta Person", "beta@x-example.com", workspace="beta")
    for slug in ("alpha", "beta"):
        rows = _rows("SELECT lead_id FROM v_reply_events WHERE workspace_id = ?",
                     (WS[slug],))
        assert len(rows) == 1


def test_reply_kind_separates_humans_from_autoresponders():
    _reply("Hank Human", "hank@x-example.com")
    _reply("Ora Ooo", "ora@x-example.com", auto=True)
    kinds = {r["reply_kind"] for r in _rows(
        "SELECT reply_kind FROM v_reply_events WHERE workspace_id = ?",
        (WS["alpha"],))}
    assert kinds == {"human", "auto_reply"}
    humans = _rows(
        "SELECT lead_id FROM v_reply_events WHERE workspace_id = ? AND reply_kind = ?",
        (WS["alpha"], "human"))
    assert len(humans) == 1


def test_the_view_agrees_with_the_python_reply_condition():
    """One definition of "a reply", whichever path the caller takes."""
    import reporting_queries as dq
    from platform_registry import reply_event_sql_condition

    _reply("Hank Human", "hank@x-example.com")
    _reply("Ora Ooo", "ora@x-example.com", auto=True)
    _send("Just Sent", "sent@x-example.com")
    conn = om.get_conn()
    try:
        cte, params = dq._ws_events(None, None)
        via_python = conn.execute(
            cte + f"SELECT COUNT(*) AS n FROM ws_events"
            f" WHERE {reply_event_sql_condition()}", (WS["alpha"], *params)
        ).fetchone()["n"]
        via_view = conn.execute(
            "SELECT COUNT(*) AS n FROM v_reply_events WHERE workspace_id = ?",
            (WS["alpha"],)).fetchone()["n"]
    finally:
        conn.close()
    assert via_view == via_python == 2


def test_lead_sentiment_view_names_both_anchors():
    lead_id = _reply("Pos Person", "pos@x-example.com")
    lead_actions.change_stage_scoped(
        lead_id, "interested", workspace_slug="alpha", sentiment="positive")
    row = _rows(
        """SELECT sentiment, current_sentiment_since, first_positive_at
             FROM v_lead_sentiment WHERE workspace_id = ? AND lead_id = ?""",
        (WS["alpha"], lead_id))[0]
    assert row["sentiment"] == "positive"
    assert row["current_sentiment_since"] is not None
    assert "first_positive_at" in row.keys()


def test_ensure_read_views_replaces_a_changed_definition():
    """CREATE VIEW IF NOT EXISTS would not, and an upgraded install would keep
    reporting against last release's logic forever."""
    conn = om.get_conn()
    try:
        conn.execute("DROP VIEW IF EXISTS v_reply_events")
        conn.execute("CREATE VIEW v_reply_events AS SELECT 1 AS stale")
        schema_views.ensure_read_views(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(v_reply_events)")}
    finally:
        conn.close()
    assert "stale" not in cols
    assert {"workspace_id", "lead_id", "event_at", "reply_kind"} <= cols
