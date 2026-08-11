#!/usr/bin/env python3
"""The deliverability reporting surface: rosters, bounces, reply kinds.

Skill-first: every one of these is what the CLI returns AND what the dashboard
renders, so the field names are the contract and are asserted here.
Placeholder data only (acme / acmemail.com).
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
import reporting_queries as rq  # noqa: E402
from bounces import record_bounce_event  # noqa: E402
from campaign_stats import classify_reply_kind  # noqa: E402
from pipeline_sender_accounts import (  # noqa: E402
    ensure_sender_account,
    link_sender_account_to_workspace,
    set_sender_domain_cost,
)
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


def _mailbox(email="sender@acmemail.com", *, has_sent=True):
    """A sending mailbox. `has_sent=False` builds one that never sent anything.

    A real sending mailbox has a last_outbound_at; a row without one is either
    brand new or -- far more often -- not a mailbox of ours at all, because
    inbound events used to bootstrap a sender account from the other party's
    address. domain_health hides those by default, so the fixture has to say
    which kind it is building.
    """
    conn = om.get_conn()
    try:
        sender_id = ensure_sender_account(conn, email)
        link_sender_account_to_workspace(conn, WS["alpha"], sender_id)
        if has_sent:
            conn.execute(
                "UPDATE sender_accounts SET last_outbound_at = datetime('now') WHERE id = ?",
                (sender_id,))
        conn.commit()
    finally:
        conn.close()
    return email


# --- provider vs reseller --------------------------------------------------

def test_domain_provider_is_the_platform_not_the_reseller():
    """The Provider column used to be filled from `reseller`."""
    _mailbox()
    set_sender_domain_cost(
        "acmemail.com", reseller="cheapinboxes", provider="google_workspace")

    conn = om.get_conn()
    try:
        row = next(d for d in rq.domain_health(conn, WS["alpha"])["domains"]
                   if d["domain"] == "acmemail.com")
    finally:
        conn.close()
    assert row["provider"] == "google_workspace"
    assert row["reseller"] == "cheapinboxes"


def test_mailbox_carries_its_domains_reseller():
    _mailbox()
    set_sender_domain_cost("acmemail.com", reseller="cheapinboxes", provider="microsoft_365")

    conn = om.get_conn()
    try:
        row = next(m for m in rq.mailbox_health(conn, WS["alpha"])["mailboxes"]
                   if m["email"] == "sender@acmemail.com")
    finally:
        conn.close()
    assert row["domain_reseller"] == "cheapinboxes"
    assert row["domain_provider"] == "microsoft_365"


def test_domain_row_reports_sends_for_the_range():
    """Sends is the denominator under every rate on the row."""
    mailbox = _mailbox()
    lead = om.add_lead("Jane Doe", company="Acme", email="jane@example.com")["id"]
    _send(lead, mailbox, subject="Quick question")

    conn = om.get_conn()
    try:
        row = next(d for d in rq.domain_health(conn, WS["alpha"], since="all")["domains"]
                   if d["domain"] == "acmemail.com")
    finally:
        conn.close()
    assert row["sends"] == 1


def test_domain_age_is_computed_from_the_registration_date():
    _mailbox()
    set_sender_domain_cost("acmemail.com")
    conn = om.get_conn()
    try:
        conn.execute(
            "UPDATE sender_domains SET registration_date = '2020-01-01' WHERE domain = ?",
            ("acmemail.com",))
        conn.commit()
        row = next(d for d in rq.domain_health(conn, WS["alpha"])["domains"]
                   if d["domain"] == "acmemail.com")
    finally:
        conn.close()
    assert row["domain_age_days"] > 1800
    assert row["registration_date"] == "2020-01-01"


def test_vocab_offers_values_already_in_use():
    set_sender_domain_cost("acmemail.com", reseller="cheapinboxes", provider="zoho")
    conn = om.get_conn()
    try:
        vocab = rq.deliverability_vocab(conn)
    finally:
        conn.close()
    assert "cheapinboxes" in vocab["resellers"]
    assert "zoho" in vocab["providers"]
    assert "google_workspace" in vocab["providers"]  # seeded


def test_vocab_does_not_offer_the_same_value_twice_in_different_cases():
    """Offering GOOGLE_WORKSPACE and google_workspace is the confusion the
    picker exists to remove; the value already in the data wins."""
    set_sender_domain_cost("acmemail.com", provider="GOOGLE_WORKSPACE")
    conn = om.get_conn()
    try:
        providers = rq.deliverability_vocab(conn)["providers"]
    finally:
        conn.close()
    lowered = [p.lower() for p in providers]
    assert len(lowered) == len(set(lowered))
    assert "GOOGLE_WORKSPACE" in providers


# --- DNSBL evidence --------------------------------------------------------

def test_domain_row_carries_the_per_list_dnsbl_report():
    """A verdict without its evidence is what made 'listed' untrustworthy."""
    _mailbox()
    set_sender_domain_cost("acmemail.com")
    om.update_sender_domain_blacklist_status("acmemail.com", {
        "checked_at": "2026-08-07T00:00:00+00:00",
        "all_clean": True,
        "summary": {"clean": 1, "listed": 0, "errors": 1},
        "tiers_scanned": ["tier1"],
        "partial": True,
        "results": [
            {"name": "Spamhaus_DBL", "status": "clean", "addresses": []},
            {"name": "URIBL", "status": "check_failed", "addresses": ["127.0.0.1"],
             "error": "refused"},
        ],
    })
    conn = om.get_conn()
    try:
        row = next(d for d in rq.domain_health(conn, WS["alpha"])["domains"]
                   if d["domain"] == "acmemail.com")
    finally:
        conn.close()
    assert len(row["dnsbl_results"]) == 2
    assert row["dnsbl_checks_failed"] == 1
    assert row["dnsbl_listed_on"] == []
    assert row["listed"] is False
    assert row["dnsbl_partial"] is True


# --- bounce explorer -------------------------------------------------------

def _send(lead_id, mailbox="sender@acmemail.com", subject="Quick question"):
    """An outbound event attributed to a mailbox (events.sender is what the
    deliverability rollups read)."""
    result = lead_actions.log_event_scoped(
        lead_id, "email_sent", workspace_slug="alpha", subject=subject)
    conn = om.get_conn()
    try:
        conn.execute(
            "UPDATE events SET sender = ? WHERE id = ("
            "SELECT MAX(id) FROM events WHERE lead_id = ?)", (mailbox, lead_id))
        conn.commit()
    finally:
        conn.close()
    return result


def _bounce(lead_id, message, sender="sender@acmemail.com", email="jane@example.com"):
    conn = om.get_conn()
    try:
        out = record_bounce_event(
            conn, lead_id=lead_id, event_id=None, platform="acme_sequencer",
            sender_email=sender, lead_email=email,
            payload={"bounce_type": "hard", "bounce_message": message},
            workspace_id=WS["alpha"])
        conn.commit()
    finally:
        conn.close()
    return out


def test_signature_groups_the_same_failure_and_keeps_the_status_code():
    """Recipient-specific detail is normalized away; the code that classifies
    the failure is not."""
    first = rq.bounce_signature(
        "5.7.193 UnifiedGroupAgent; delivery failed for jane@example.com from 10.0.0.1")
    second = rq.bounce_signature(
        "5.7.193 UnifiedGroupAgent; delivery failed for bob@example.org from 10.0.0.9")
    assert first == second
    assert first.startswith("5.7.193")


def test_signature_keeps_diagnostic_tokens_that_look_like_hostnames():
    """TRANSPORT.RULES.RejectMessage is the failure, not a hostname."""
    sig = rq.bounce_signature("TRANSPORT.RULES.RejectMessage; rejected by policy")
    assert "transport rules rejectmessage" in sig


def test_signature_normalizes_a_real_hostname():
    sig = rq.bounce_signature("host aspmx.l.google.com[74.125.202.27] said:")
    assert "<host>" in sig and "<ip>" in sig


def test_bounce_explorer_groups_and_names_the_no_message_case():
    """94% of real bounces carry no diagnostic; that is one honest row."""
    lead = om.add_lead("Jane Doe", company="Acme", email="jane@example.com")["id"]
    _bounce(lead, "550 5.1.1 recipient address rejected")
    other = om.add_lead("Bob Roe", company="Acme", email="bob@example.com")["id"]
    _bounce(other, None, email="bob@example.com")

    conn = om.get_conn()
    try:
        out = rq.bounce_explorer(conn, WS["alpha"], since="all", group_by="signature")
    finally:
        conn.close()
    assert out["total"] == 2
    keys = [g["key"] for g in out["groups"]]
    assert any("no diagnostic message" in k for k in keys)


def test_bounce_detail_returns_the_sends_that_came_before_it():
    """'Is this the copy or is this the list' needs the copy."""
    lead = om.add_lead("Jane Doe", company="Acme", email="jane@example.com")["id"]
    _send(lead, subject="Quick question")
    result = _bounce(lead, "550 5.1.1 recipient address rejected")

    conn = om.get_conn()
    try:
        detail = rq.bounce_detail(conn, result["id"])
    finally:
        conn.close()
    assert detail["bounce"]["bounce_message"].startswith("550")
    assert [e["subject"] for e in detail["sends_before"]] == ["Quick question"]


def test_bounce_trend_reports_the_rule_inputs_not_just_a_flag():
    lead = om.add_lead("Jane Doe", company="Acme", email="jane@example.com")["id"]
    _bounce(lead, "550 5.1.1 recipient address rejected")

    conn = om.get_conn()
    try:
        trend = rq.bounce_trend(conn, mailbox="sender@acmemail.com", days=14)
    finally:
        conn.close()
    assert trend["bounces_last_7d"] == 1
    assert trend["bounces_prior_7d"] == 0
    assert trend["delta"] == 1
    assert trend["rule_fired"] == "rising_bounces"
    assert trend["bounce_rate_threshold"] == rq.MAILBOX_BOUNCE_RATE_THRESHOLD


# --- reply kinds -----------------------------------------------------------

def test_bounce_is_not_counted_as_a_reply():
    assert classify_reply_kind(has_reply_event=True, has_bounce=True) == "bounce"
    assert classify_reply_kind(has_reply_event=True, event_type="email_bounce") == "bounce"


def test_auto_reply_is_its_own_kind():
    assert classify_reply_kind(has_reply_event=True, is_auto_reply=True) == "auto_reply"
    assert classify_reply_kind(has_reply_event=True, sentiment="autoreply") == "auto_reply"


def test_a_plain_inbound_reply_is_human():
    assert classify_reply_kind(has_reply_event=True, sentiment="positive") == "human"


def test_sentiment_with_no_inbound_event_is_status_only():
    assert classify_reply_kind(has_reply_event=False, sentiment="positive") == "status_only"


def test_campaign_replies_splits_kinds_and_counts_before_filtering():
    """The split must stay visible whichever kind is being shown."""
    replier = om.add_lead("Jane Doe", company="Acme", email="jane@example.com")["id"]
    bouncer = om.add_lead("Bob Roe", company="Acme", email="bob@example.com")["id"]
    lead_actions.log_event_scoped(
        replier, "email_reply", workspace_slug="alpha", direction="inbound",
        subject="Re: hello")
    lead_actions.log_event_scoped(bouncer, "email_bounce", workspace_slug="alpha")
    _bounce(bouncer, "550 rejected", email="bob@example.com")
    # The sentiment anchor is what campaign_replies is built on.
    conn = om.get_conn()
    try:
        for lead_id, sentiment, label in (
                (replier, "positive", "Interested"), (bouncer, "invalid", "Bounced")):
            conn.execute(
                """UPDATE workspace_leads
                      SET current_status_sentiment = ?, current_status_label = ?,
                          current_sentiment_since = datetime('now')
                    WHERE workspace_id = ? AND lead_id = ?""",
                (sentiment, label, WS["alpha"], lead_id))
        conn.commit()
    finally:
        conn.close()

    conn = om.get_conn()
    try:
        everything = rq.campaign_replies(conn, WS["alpha"], since="all")
        humans = rq.campaign_replies(conn, WS["alpha"], since="all", reply_kind="human")
    finally:
        conn.close()

    kinds = {r["lead_id"]: r["reply_kind"] for r in everything["replies"]}
    assert kinds[replier] == "human"
    assert kinds[bouncer] == "bounce"
    # Filtering narrows the rows but never the counts.
    assert [r["lead_id"] for r in humans["replies"]] == [replier]
    assert humans["kind_counts"]["bounce"] == 1
    assert humans["kind_counts"]["human"] == 1


def test_kind_filter_applies_before_the_row_cap():
    """Asking for human replies must not return an empty page just because the
    first N rows by date happened to be bounces."""
    leads = []
    for index in range(6):
        lead = om.add_lead(f"Person {index}", company="Acme",
                           email=f"p{index}@example.com")["id"]
        is_human = index == 5
        lead_actions.log_event_scoped(
            lead, "email_reply" if is_human else "email_bounce",
            workspace_slug="alpha",
            direction="inbound" if is_human else "outbound",
            subject="Re: hello" if is_human else None)
        if not is_human:
            _bounce(lead, "550 rejected", email=f"p{index}@example.com")
        leads.append((lead, is_human, index))

    conn = om.get_conn()
    try:
        for lead, is_human, index in leads:
            conn.execute(
                """UPDATE workspace_leads
                      SET current_status_sentiment = ?, current_status_label = ?,
                          current_sentiment_since = datetime('now', ?)
                    WHERE workspace_id = ? AND lead_id = ?""",
                ("positive" if is_human else "invalid",
                 "Interested" if is_human else "Bounced",
                 f"-{10 - index} minutes", WS["alpha"], lead))
        conn.commit()

        # A cap of 3 would have fetched three bounces and then filtered to none.
        humans = rq.campaign_replies(
            conn, WS["alpha"], since="all", limit=3, reply_kind="human")
    finally:
        conn.close()

    assert len(humans["replies"]) == 1
    assert humans["replies"][0]["reply_kind"] == "human"
    # Counts describe the whole selection, not the page.
    assert humans["kind_counts"]["bounce"] == 5


def test_a_domain_nothing_ever_sent_from_is_hidden_by_default():
    """isenberg.umass.edu was on the Deliverability page as a sending domain.

    It got there because a reply from someone at that address created a sender
    account: log_event bootstrapped one from any event carrying a sender,
    inbound included. 237 of 779 accounts in the live database had never sent
    anything. The write path is fixed; this is the read side refusing to show
    the rows it already wrote.
    """
    _mailbox("reply@isenberg.umass.test", has_sent=False)
    _mailbox("sender@acmemail.com")
    conn = om.get_conn()
    try:
        default = rq.domain_health(conn, WS["alpha"])
        shown = {d["domain"] for d in default["domains"]}
        assert "acmemail.com" in shown
        assert "isenberg.umass.test" not in shown
        assert default["never_sent_hidden"] == 1

        everything = rq.domain_health(conn, WS["alpha"], exclude_never_sent=False)
        rows = {d["domain"]: d for d in everything["domains"]}
        assert "isenberg.umass.test" in rows
        assert rows["isenberg.umass.test"]["never_sent"] is True
        assert rows["acmemail.com"]["never_sent"] is False
    finally:
        conn.close()


def test_an_inbound_reply_does_not_create_a_sending_mailbox():
    """The write-path half of the same bug.

    log_event bootstrapped a sender account from any event carrying a sender.
    On an inbound event that address is frequently the CONTACT's, not ours, so
    replying to us made your domain one of our sending domains.
    """
    lead_id = om.add_lead("Prof Recipient", email="prof@isenberg.umass.test")["id"]
    om.log_event(lead_id, "email_reply", direction="inbound",
                 sender="prof@isenberg.umass.test")
    conn = om.get_conn()
    try:
        found = conn.execute(
            "SELECT COUNT(*) AS n FROM sender_accounts WHERE email = ?",
            ("prof@isenberg.umass.test",)).fetchone()["n"]
        assert found == 0, "an inbound reply must not create a sender account"
    finally:
        conn.close()


def test_an_outbound_send_still_creates_its_mailbox():
    """The guard must not break the path that legitimately bootstraps seats.

    Prosp/LinkedIn accounts have no CSV export; outbound events are the only
    thing that ever creates them.
    """
    lead_id = om.add_lead("Target", email="target@acme.test")["id"]
    om.log_event(lead_id, "email_sent", direction="outbound",
                 sender="rep@acmemail.com")
    conn = om.get_conn()
    try:
        found = conn.execute(
            "SELECT COUNT(*) AS n FROM sender_accounts WHERE email = ?",
            ("rep@acmemail.com",)).fetchone()["n"]
        assert found == 1
    finally:
        conn.close()


def test_prune_spares_a_never_sent_mailbox_on_a_domain_we_do_send_from():
    """A spare or still-warming mailbox is not prospect leakage.

    On the live database this was 167 of 233 never-sent accounts -- deleting
    them would have removed real sending infrastructure. Only a domain where
    NOTHING has ever sent is the leaked-prospect case.
    """
    from pipeline_sender_accounts import prune_never_sent_senders

    _mailbox("active@acmemail.com")                      # this domain sends
    _mailbox("spare@acmemail.com", has_sent=False)       # ...so the spare stays
    _mailbox("prof@isenberg.umass.test", has_sent=False)  # nothing here ever sent

    report = prune_never_sent_senders()
    assert "prof@isenberg.umass.test" in report["account_emails"]
    assert "spare@acmemail.com" not in report["account_emails"]
    assert report["accounts"] == 1


def test_prune_reports_before_it_deletes():
    from pipeline_sender_accounts import prune_never_sent_senders

    _mailbox("ghost@nowhere.test", has_sent=False)
    _mailbox("real@acmemail.com")

    report = prune_never_sent_senders()
    assert report["applied"] is False
    assert "ghost@nowhere.test" in report["account_emails"]
    assert "real@acmemail.com" not in report["account_emails"]

    conn = om.get_conn()
    try:
        still_there = conn.execute(
            "SELECT COUNT(*) AS n FROM sender_accounts").fetchone()["n"]
    finally:
        conn.close()
    assert still_there == 2, "reporting must not delete anything"

    applied = prune_never_sent_senders(apply=True)
    assert applied["applied"] is True
    assert applied["accounts"] == 1
    conn = om.get_conn()
    try:
        remaining = [r["email"] for r in conn.execute(
            "SELECT email FROM sender_accounts").fetchall()]
    finally:
        conn.close()
    assert remaining == ["real@acmemail.com"]


def test_bulk_scan_accepts_a_selection_not_just_one_or_all():
    from pipeline_sender_accounts import get_sender_domains_for_scan

    picked = get_sender_domains_for_scan(["a.test", "b.test", "c.test"])
    assert [d["domain"] for d in picked] == ["a.test", "b.test", "c.test"]
    assert [d["domain"] for d in get_sender_domains_for_scan("solo.test")] == ["solo.test"]


def test_bounces_group_by_recipient_domain():
    """Whose server is rejecting us, not which of our domains is rejected.

    One university bouncing 400 sends is a list problem; four hundred domains
    bouncing one each is a reputation problem, and grouping by the SENDING
    domain cannot tell those apart.
    """
    leads = {addr: om.add_lead(f"Person {i}", email=addr)["id"]
             for i, addr in enumerate(("a@bigco.test", "b@bigco.test", "c@other.test"))}
    conn = om.get_conn()
    try:
        for i, (lead_email, sender) in enumerate((
            ("a@bigco.test", "rep@acmemail.com"),
            ("b@bigco.test", "rep@acmemail.com"),
            ("c@other.test", "rep@acmemail.com"),
        )):
            conn.execute(
                """INSERT INTO bounce_events
                       (id, org_id, lead_id, lead_email, sender_email,
                        bounce_message, bounce_type, platform, occurrence_count,
                        first_seen_at, last_seen_at, workspace_id)
                   VALUES (?, ?, ?, ?, ?, 'mailbox unavailable', 'hard',
                           'plusvibe', 1, datetime('now'), datetime('now'), ?)""",
                (f"be_{i}", om.DEFAULT_ORG_ID, leads[lead_email], lead_email, sender, WS["alpha"]))
        conn.commit()
        report = rq.bounce_explorer(
            conn, WS["alpha"], since="all", group_by="recipient_domain")
        by_key = {g["key"]: g["bounces"] for g in report["groups"]}
    finally:
        conn.close()
    assert by_key.get("bigco.test") == 2
    assert by_key.get("other.test") == 1
