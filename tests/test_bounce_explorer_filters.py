"""Clicking a failure filters the contacts below it.

Reported as: "it says 6 contacts 6 events for one failure and if you click on it
the right hand pane is only for one person". A group row opened a drawer for one
example bounce, so a group of six answered with one person's story and three
sibling lists nobody asked for.

The group counts and the filtered rows have to describe the same set -- that is
the assertion the report is really asking for, and it is what makes the drill-in
trustworthy.
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

MSG_A = "550 5.1.1 The email account that you tried to reach does not exist"
MSG_B = "452 4.2.2 The recipient's mailbox is over its storage limit"


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed():
    conn = om.get_conn()
    # One registered sending domain, one that nobody registered -- the second is
    # why the join has to be a LEFT JOIN.
    conn.execute(
        """INSERT INTO sender_domains (domain, provider, reseller)
           VALUES ('acmemail.com', 'Google Workspace', 'Acme Domains')""")
    rows = [
        ("a@one.example", "ann@acmemail.com", MSG_A, "hard", "5.1.1"),
        ("b@one.example", "bob@acmemail.com", MSG_A, "hard", "5.1.1"),
        ("c@two.example", "cy@unregistered.example", MSG_A, "hard", "5.1.1"),
        ("d@two.example", "dee@acmemail.com", MSG_B, "soft", "4.2.2"),
    ]
    for i, (lead_email, sender, msg, btype, code) in enumerate(rows):
        lead = om.resolve_lead(name=f"Bounced {i}", source="csv",
                               allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET email = ? WHERE id = ?", (lead_email, lead["id"]))
        conn.execute(
            """INSERT INTO bounce_events
                   (org_id, lead_id, lead_email, sender_email, bounce_message,
                    bounce_type, smtp_code, platform, occurrence_count,
                    first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'acme_sequencer', 2, ?, ?)""",
            (om.DEFAULT_ORG_ID, lead["id"], lead_email, sender, msg, btype, code,
             f"2026-09-0{i + 1} 09:00:00", f"2026-09-0{i + 1} 09:00:00"))
    conn.commit()
    return conn


def test_group_counts_match_the_filtered_rows():
    """The assertion behind '6 contacts, 6 events' showing one person."""
    conn = _seed()
    grouped = rq.bounce_explorer(conn, since="all", group_by="signature")
    assert grouped["groups"], "expected failure groups"
    for g in grouped["groups"]:
        drilled = rq.bounce_explorer(
            conn, since="all", group_by="signature", signature=g["key"])
        assert drilled["total"] == g["bounces"], g["key"]
        assert len(drilled["bounces"]) == g["bounces"], g["key"]
        assert drilled["total_occurrences"] == g["occurrences"], g["key"]
    conn.close()


def test_signature_filter_selects_only_that_failure():
    conn = _seed()
    grouped = rq.bounce_explorer(conn, since="all", group_by="signature")
    key = next(g["key"] for g in grouped["groups"] if g["bounces"] == 3)
    drilled = rq.bounce_explorer(conn, since="all", signature=key)
    assert {b["lead_email"] for b in drilled["bounces"]} == {
        "a@one.example", "b@one.example", "c@two.example"}
    conn.close()


def test_rows_carry_sending_domain_provider_and_reseller():
    conn = _seed()
    rows = {b["lead_email"]: b for b in
            rq.bounce_explorer(conn, since="all")["bounces"]}
    ann = rows["a@one.example"]
    assert ann["sending_domain"] == "acmemail.com"
    assert ann["provider"] == "Google Workspace"
    assert ann["reseller"] == "Acme Domains"


def test_unregistered_sending_domain_still_lists():
    """LEFT JOIN: a domain nobody registered must not drop its bounces."""
    conn = _seed()
    rows = {b["lead_email"]: b for b in
            rq.bounce_explorer(conn, since="all")["bounces"]}
    cy = rows["c@two.example"]
    assert cy["sending_domain"] == "unregistered.example"
    assert cy["provider"] is None and cy["reseller"] is None
    conn.close()


def test_recipient_domain_and_smtp_code_filters():
    conn = _seed()
    by_recipient = rq.bounce_explorer(conn, since="all", recipient_domain="two.example")
    assert {b["lead_email"] for b in by_recipient["bounces"]} == {
        "c@two.example", "d@two.example"}

    by_code = rq.bounce_explorer(conn, since="all", smtp_code="4.2.2")
    assert [b["lead_email"] for b in by_code["bounces"]] == ["d@two.example"]
    conn.close()


def test_filters_are_echoed_back():
    """The UI renders the active filter from the answer, not its own memory."""
    conn = _seed()
    res = rq.bounce_explorer(conn, since="all", smtp_code="5.1.1")
    assert res["filters"]["smtp_code"] == "5.1.1"
    assert res["filters"]["signature"] is None
    conn.close()


def test_signature_filter_paginates():
    conn = _seed()
    grouped = rq.bounce_explorer(conn, since="all", group_by="signature")
    key = next(g["key"] for g in grouped["groups"] if g["bounces"] == 3)
    page = rq.bounce_explorer(conn, since="all", signature=key, limit=2, offset=0)
    rest = rq.bounce_explorer(conn, since="all", signature=key, limit=2, offset=2)
    assert len(page["bounces"]) == 2 and len(rest["bounces"]) == 1
    # The total describes the whole match, not the page.
    assert page["total"] == rest["total"] == 3
    conn.close()
