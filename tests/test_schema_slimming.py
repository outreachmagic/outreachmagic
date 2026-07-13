"""Guards for the schema-slimming invariants.

The local DB had grown to 783 MB, roughly a third of it either stored twice or
never read. Each of these tests pins one of the properties that fixed it, because
each is easy to silently undo.
"""

from __future__ import annotations

import pipeline as om
import relay_ingest as ri
from db_conn import get_conn
from pipeline_sender_accounts import sender_domain_activity


def _event(relay_id, event_type, sender, lead, at):
    return {
        "platform": "plusvibe",
        "relay_id": relay_id,
        "event_type": event_type,
        "lead": lead,
        "received_at": at,
        "payload": {
            "campaign_name": "C1", "campaign_id": "c1", "sender": sender,
            "subject": "s", "text_body": "b",
        },
    }


def _setup():
    om.init_db()
    om.set_workspace_routing("single")


def test_relay_event_cannot_be_ingested_twice():
    """One relay event, one events row -- even if the dedupe ledger is lost.

    relay_ingested is the primary dedupe, but it's a separate table. When it was
    reset while events survived, the next pull silently re-ingested everything and
    duplicated 17,363 events -- inflating every send/reply count downstream.
    idx_events_relay_unique is the guard that makes that impossible.
    """
    _setup()
    ri.ingest_relay_event(_event(1, "email_sent", "ava@acme-example.com",
                                 "a@example.com", "2026-06-01T10:00:00Z"))
    conn = get_conn()
    before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()

    # Wipe the dedupe ledger, exactly as a restore/rebuild would, then replay.
    conn = get_conn()
    conn.execute("DELETE FROM relay_ingested")
    conn.commit()
    conn.close()
    ri.ingest_relay_event(_event(1, "email_sent", "ava@acme-example.com",
                                 "a@example.com", "2026-06-01T10:00:00Z"))

    conn = get_conn()
    after = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    dupes = conn.execute(
        "SELECT count(*) FROM (SELECT relay_id FROM events "
        "WHERE relay_id IS NOT NULL GROUP BY relay_id HAVING count(*) > 1)"
    ).fetchone()[0]
    conn.close()
    assert after == before
    assert dupes == 0


def test_relay_ingested_stores_a_hash_not_the_key():
    """The dedupe keys average 129 bytes; only a 16-byte digest is kept."""
    _setup()
    ri.ingest_relay_event(_event(2, "email_sent", "ava@acme-example.com",
                                 "b@example.com", "2026-06-01T10:00:00Z"))
    conn = get_conn()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(relay_ingested)")}
    row = conn.execute("SELECT dedupe_hash FROM relay_ingested LIMIT 1").fetchone()
    conn.close()
    assert "dedupe_key" not in cols
    assert isinstance(row["dedupe_hash"], bytes)
    assert len(row["dedupe_hash"]) == 16
    # Round-trips: the same key must always hash to the same stored value.
    assert ri.relay_dedupe_hash("relay:2") == ri.relay_dedupe_hash("relay:2")
    assert ri.relay_dedupe_hash("relay:2") != ri.relay_dedupe_hash("relay:3")


def test_agent_replay_normalizes_event_type_aliases():
    """'email_bounced' and 'email_bounce' are the same thing.

    The webhook path normalized aliases; the agent-replay path took event_type
    straight off the payload, so both spellings ended up in events.event_type and
    every bounce query had to remember to check for both.
    """
    from platform_registry import normalize_local_event_type

    assert normalize_local_event_type("email_bounced") == "email_bounce"
    assert normalize_local_event_type("email.bounced") == "email_bounce"
    assert normalize_local_event_type("email_bounce") == "email_bounce"
    # Unknown types pass through untouched.
    assert normalize_local_event_type("lead_status_updated") == "lead_status_updated"


def test_sender_activity_tracks_both_directions_and_never_goes_backwards():
    """last_outbound_at / last_inbound_at, and the domain rollup over them.

    events.sender is always one of our own mailboxes -- on an inbound reply it's
    the seat that *received* it -- so direction alone picks the column. A pull
    replays history in relay order, not chronological order, so the write must only
    ever move the timestamp forward.
    """
    _setup()
    ri.ingest_relay_event(_event(10, "email_sent", "ava@acme-example.com",
                                 "a@example.com", "2026-06-01T10:00:00Z"))
    ri.ingest_relay_event(_event(11, "all_email_replies", "ava@acme-example.com",
                                 "a@example.com", "2026-06-05T10:00:00Z"))
    # Out-of-order replay of a much older send: must NOT clobber the newer stamp.
    ri.ingest_relay_event(_event(12, "email_sent", "ava@acme-example.com",
                                 "c@example.com", "2026-01-01T10:00:00Z"))
    # A second domain, outbound only.
    ri.ingest_relay_event(_event(13, "email_sent", "bo@other-example.com",
                                 "d@example.com", "2026-06-03T10:00:00Z"))

    conn = get_conn()
    ava = conn.execute(
        "SELECT last_outbound_at, last_inbound_at FROM sender_accounts WHERE email = ?",
        ("ava@acme-example.com",),
    ).fetchone()
    assert ava["last_outbound_at"].startswith("2026-06-01")
    assert ava["last_inbound_at"].startswith("2026-06-05")

    by_domain = {d["domain"]: d for d in sender_domain_activity(conn)}
    conn.close()
    assert by_domain["acme-example.com"]["last_outbound_at"].startswith("2026-06-01")
    assert by_domain["acme-example.com"]["last_inbound_at"].startswith("2026-06-05")
    # A domain that has sent but never heard back reads as exactly that.
    assert by_domain["other-example.com"]["last_outbound_at"].startswith("2026-06-03")
    assert by_domain["other-example.com"]["last_inbound_at"] is None


def test_sender_identity_key_is_normalized_on_every_path():
    """One normalization rule for (org_id, email).

    entity_key lowercased it, ensure_sender_account only stripped it, and
    find_by_email forced lowercase -- so a sender arriving with any uppercase made
    a second row that the third path could never find.
    """
    from pipeline_sender_accounts import (
        ensure_sender_account,
        find_sender_account_id_by_email,
        infer_sender_channel,
    )

    _setup()
    conn = get_conn()
    a = ensure_sender_account(conn, "  Ava@Acme-Example.COM ")
    b = ensure_sender_account(conn, "ava@acme-example.com")
    assert a == b, "case/whitespace variants must resolve to one row"
    assert find_sender_account_id_by_email(conn, "AVA@acme-example.com") == a
    n = conn.execute(
        "SELECT count(*) FROM sender_accounts WHERE email LIKE '%acme-example.com'"
    ).fetchone()[0]
    conn.close()
    assert n == 1

    # A LinkedIn seat has no address, so it must not be labelled an email seat.
    assert infer_sender_channel("linkedin.com/in/someone") == "linkedin"
    assert infer_sender_channel("ava@acme-example.com") == "email"
