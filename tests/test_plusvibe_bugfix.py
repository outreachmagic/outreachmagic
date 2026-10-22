#!/usr/bin/env python3
"""Regression tests for PlusVibe dedup, mappings, and stage advancement."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import relay_ingest as ri  # noqa: E402
from platform_registry import PLATFORM_SPECS, resolve_event  # noqa: E402
from relay_extractors import extract_relay_identity  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    ws = om.create_workspace("PlusVibe Test", slug="pv-test")
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO campaign_workspace_map
           (id, org_id, source_platform, campaign_platform_id, campaign_name_normalized, workspace_id)
           VALUES ('map1', ?, 'plusvibe', 'camp-1', 'test campaign', ?)""",
        (om.DEFAULT_ORG_ID, f"ws_{ws['slug']}"),
    )
    conn.commit()
    conn.close()
    yield


def _reply_event(*, event_type: str, relay_id: int, body: str = "Yes, let's talk") -> dict:
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "lead": "lead@example.com",
        "received_at": "2026-06-10T12:00:01Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "text_body": body,
            "body": body,
        },
    }


def _status_event(event_type: str, relay_id: int) -> dict:
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "lead": "lead@example.com",
        "received_at": "2026-06-10T12:00:05Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
        },
    }


def test_all_positive_replies_skipped_after_all_email_replies():
    first = _reply_event(event_type="all_email_replies", relay_id=101)
    dup = _reply_event(event_type="all_positive_replies", relay_id=102)
    assert ri.ingest_relay_event(first, force_workspace_id="ws_pv-test") == 1
    assert ri.ingest_relay_event(dup, force_workspace_id="ws_pv-test") is None
    conn = om.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'email_reply'",
    ).fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.parametrize(
    "event_type,expected_type,expected_stage",
    [
        ("lead_marked_as_meeting_booked", "meeting_booked", "interested"),
        ("lead_marked_as_meeting_completed", "meeting_completed", "interested"),
        ("lead_marked_as_qc_interested", "lead_status_updated", "interested"),
        ("lead_marked_as_qc_crm_only", "lead_disposition", "interested"),
        ("lead_marked_as_wrong_person", "lead_status_updated", "not_interested"),
        ("lead_marked_as_closed", "lead_status_updated", "not_interested"),
    ],
)
def test_plusvibe_status_events_map_and_advance_stage(event_type, expected_type, expected_stage):
    resolved = resolve_event("plusvibe", event_type, {})
    assert resolved.local_type == expected_type
    assert resolved.target_stage == expected_stage

    event = _status_event(event_type, relay_id=200)
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    row = conn.execute(
        """SELECT wl.status FROM workspace_leads wl
           JOIN leads l ON l.id = wl.lead_id
           WHERE l.email = ?""",
        ("lead@example.com",),
    ).fetchone()
    evt = conn.execute(
        "SELECT event_type FROM events WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == expected_stage
    assert evt["event_type"] == expected_type


def test_not_interested_is_not_absorbing_for_later_positive_reclassification():
    """Regression: an AI auto-classification of not_interested must not block
    a later human QC reclassification with positive sentiment from advancing
    the stage to interested. Previously, furthest_stage() ranked
    'not_interested' (which sorts after 'won' in PIPELINE_STAGES) as more
    advanced than 'interested', so the override was silently discarded and
    the lead stayed stuck at not_interested despite the positive relabel."""
    not_interested = _status_event("lead_marked_as_not_interested", relay_id=300)
    lead_id = ri.ingest_relay_event(not_interested, force_workspace_id="ws_pv-test")
    assert lead_id is not None

    conn = om.get_conn()
    row = conn.execute("SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["status"] == "not_interested"

    reclassified = _status_event("lead_marked_as_qc_interested", relay_id=301)
    lead_id_2 = ri.ingest_relay_event(reclassified, force_workspace_id="ws_pv-test")
    assert lead_id_2 == lead_id

    conn = om.get_conn()
    row = conn.execute("SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["status"] == "interested"


def test_ingest_does_not_call_ensure_default_org_workspace_per_event(monkeypatch):
    """Regression: get_org_routing_config() already ensures the default org/
    workspace exists and returns it as cfg.default_workspace_id -- calling
    ensure_default_org_workspace() again per-event (single-workspace mode)
    was 4 redundant SQL statements x every event in a bulk pull."""
    from workspace_routing import OrgRoutingConfig, WORKSPACE_ROUTING_SINGLE

    calls = []
    monkeypatch.setattr(
        om, "ensure_default_org_workspace", lambda *a, **k: calls.append(1),
    )
    routing_config = OrgRoutingConfig(mode=WORKSPACE_ROUTING_SINGLE, default_workspace_id="ws_pv-test")

    event = _status_event("lead_marked_as_qc_interested", relay_id=400)
    lead_id = ri.ingest_relay_event(
        event, force_workspace_id="ws_pv-test", routing_config=routing_config,
    )
    assert lead_id is not None
    assert calls == []


def test_workspace_lead_events_stores_no_event_content():
    """workspace_lead_events indexes events into a workspace; it stores none of
    their content.

    It used to keep a payload_json copy of the whole of events.metadata_json --
    body included -- which nothing read and which cost 91 MB on a 783 MB database.
    It now carries an event_id instead. The content must be reachable through that
    FK, and must exist nowhere else.
    """
    event = {
        "relay_id": 500,
        "platform": "plusvibe",
        "event_type": "all_email_replies",
        "lead": "dup-payload@example.com",
        "received_at": "2026-06-10T12:00:01Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "subject": "Re: hello there",
            "text_body": "Sure, let's talk",
            "body": "Sure, let's talk",
        },
    }
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None

    conn = om.get_conn()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(workspace_lead_events)")}
    assert "payload_json" not in cols
    assert not cols & {"subject", "body", "body_preview", "metadata_json"}

    row = conn.execute(
        """SELECT wle.event_id, e.subject, e.metadata_json
             FROM workspace_lead_events wle
             JOIN events e ON e.id = wle.event_id
            WHERE wle.lead_id = ?""",
        (lead_id,),
    ).fetchone()
    conn.close()

    # The FK resolves, and the body is intact on the one row that owns it.
    assert row is not None and row["event_id"]
    assert row["subject"] == "Re: hello there"
    assert json.loads(row["metadata_json"])["body"] == "Sure, let's talk"


def test_auto_merge_safe_identity_types_membership():
    """Sanity check the allowlist boundary itself: only email + LinkedIn's
    own unique identifiers are solid enough to trigger an automatic merge;
    external_id/phone/provider_id are not, even though external_id/phone are
    still fine as additive aliases on the current lead."""
    from workspace_routing import AUTO_MERGE_SAFE_IDENTITY_TYPES

    assert AUTO_MERGE_SAFE_IDENTITY_TYPES == {
        "email", "linkedin_url", "linkedin_sales_nav_id", "linkedin_member_id",
    }


def test_shared_provider_id_does_not_queue_merge_forwarded_thread_scenario():
    """Regression: PlusVibe assigns the same provider_id (its internal
    thread/conversation id) to both parties on a forwarded-email reply --
    two genuinely different people must not get queued for merge."""
    forwarder = {
        "relay_id": 600,
        "platform": "plusvibe",
        "event_type": "all_email_replies",
        "lead": "forwarder@example.com",
        "received_at": "2026-06-10T12:00:01Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "lead_id": "conv-thread-1",
            "text_body": "Forwarding this to a colleague",
            "body": "Forwarding this to a colleague",
        },
    }
    forwardee = {
        "relay_id": 601,
        "platform": "plusvibe",
        "event_type": "all_email_replies",
        "lead": "forwardee@example.com",
        "received_at": "2026-06-10T12:00:05Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "lead_id": "conv-thread-1",
            "text_body": "Thanks for forwarding, I'm interested",
            "body": "Thanks for forwarding, I'm interested",
        },
    }
    lead_a = ri.ingest_relay_event(forwarder, force_workspace_id="ws_pv-test")
    lead_b = ri.ingest_relay_event(forwardee, force_workspace_id="ws_pv-test")
    assert lead_a is not None and lead_b is not None
    assert lead_a != lead_b

    conn = om.get_conn()
    count = conn.execute("SELECT COUNT(*) FROM lead_merge_jobs").fetchone()[0]
    conn.close()
    assert count == 0


def test_shared_external_id_does_not_queue_automatic_merge():
    """external_id's safety depends on the source provider's own guarantees
    (not verifiable here) -- it's fine as an additive alias but must not
    trigger an automatic merge on conflict."""
    first = {
        "relay_id": 610,
        "platform": "plusvibe",
        "event_type": "all_email_replies",
        "lead": "ext-a@example.com",
        "received_at": "2026-06-10T12:00:01Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "external_id": "crm-shared-id-1",
            "text_body": "Hello",
            "body": "Hello",
        },
    }
    second = {
        "relay_id": 611,
        "platform": "plusvibe",
        "event_type": "all_email_replies",
        "lead": "ext-b@example.com",
        "received_at": "2026-06-10T12:00:05Z",
        "payload": {
            "campaign_name": "Test Campaign",
            "campaign_id": "camp-1",
            "external_id": "crm-shared-id-1",
            "text_body": "Hi there",
            "body": "Hi there",
        },
    }
    lead_a = ri.ingest_relay_event(first, force_workspace_id="ws_pv-test")
    lead_b = ri.ingest_relay_event(second, force_workspace_id="ws_pv-test")
    assert lead_a is not None and lead_b is not None
    assert lead_a != lead_b

    conn = om.get_conn()
    count = conn.execute("SELECT COUNT(*) FROM lead_merge_jobs").fetchone()[0]
    conn.close()
    assert count == 0


def test_email_conflict_still_queues_automatic_merge():
    """Proves the allowlist isn't over-broad: a genuine conflict on a safe
    identity type (email) still queues a merge job for review."""
    from workspace_routing import (
        AUTO_MERGE_SAFE_IDENTITY_TYPES,
        enqueue_identity_conflict_merge,
        upsert_identity_alias,
    )

    lead_a = om.resolve_lead(email="lead-a-conflict@example.com", name="A")["id"]
    lead_b = om.resolve_lead(email="lead-b-conflict@example.com", name="B")["id"]

    conn = om.get_conn()
    try:
        upsert_identity_alias(
            conn, om.DEFAULT_ORG_ID, lead_b, "email",
            "lead-a-conflict@example.com", source="plusvibe",
        )
        conflict_raised = False
    except ValueError:
        conflict_raised = True
        assert "email" in AUTO_MERGE_SAFE_IDENTITY_TYPES
        enqueue_identity_conflict_merge(
            conn, om.DEFAULT_ORG_ID, lead_b, "email",
            "lead-a-conflict@example.com", source="plusvibe",
        )
    conn.commit()
    assert conflict_raised

    count = conn.execute("SELECT COUNT(*) FROM lead_merge_jobs").fetchone()[0]
    conn.close()
    assert count == 1


def test_migrate_db_backfill_uses_shared_connection():
    conn = om.get_conn()
    conn.execute("INSERT INTO leads (name, email) VALUES ('No Camp', 'nocamp@example.com')")
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_sent', 'outbound', ?, NULL)""",
        (lead_id, json.dumps({"platform": "smartlead", "relay_id": 88001})),
    )
    conn.commit()
    cfg = om.load_config()
    cfg.pop("null_campaign_backfill_at", None)
    om.save_config(cfg)
    om.migrate_db(conn)
    conn.close()


# ── PlusVibe identity / Unibox (2026-09-24) ────────────────────────────────

UNIBOX_PAYLOAD = {
    "webhook_event": "UNIBOX_EMAIL_SENT",
    "direction": "OUT",
    "sent_via": "MANUAL_REPLY",
    "email": "angie@prospect.edu",
    "from_email": "mailbox@send.example",
    "to_email": "colleague@prospect.edu",
    "email_account_name": "mailbox@send.example",
    "actual_replied_from": "angie@prospect.edu",
    "first_name": "Angie",
    "label": "OUT_OF_OFFICE",
    "campaign_id": "camp-1",
    "campaign_name": "Test Campaign",
}

REPLY_ALIAS_PAYLOAD = {
    "webhook_event": "ALL_EMAIL_REPLIES",
    "direction": "IN",
    "email": "jane@acme.org",
    "from_email": "jane.doe@school.org",
    "to_email": "mailbox@send.example",
    "email_account_name": "mailbox@send.example",
    "actual_replied_from": "jane.doe@school.org",
    "first_name": "Jane",
    "label": "INTERESTED",
    "campaign_id": "camp-1",
    "campaign_name": "Test Campaign",
}

COLLEAGUE_REPLY_PAYLOAD = {
    "webhook_event": "ALL_EMAIL_REPLIES",
    "direction": "IN",
    "email": "jordan@acme.org",
    "from_email": "alex.rivera@acme.org",
    "from": "Alex Rivera <alex.rivera@acme.org>",
    "email_account_name": "mailbox@send.example",
    "first_name": "Jordan",
    "label": "INTERESTED",
    "campaign_id": "camp-1",
    "campaign_name": "Test Campaign",
}

EMAIL_SENT_PAYLOAD = {
    "webhook_event": "EMAIL_SENT",
    "lead_email": "lead@example.com",
    "email": "lead@example.com",
    "email_account_name": "mailbox@send.example",
    "campaign_id": "camp-1",
    "campaign_name": "Test Campaign",
}


def _pv_event(event_type: str, relay_id: int, payload: dict, *, envelope_lead: str) -> dict:
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "entity_key": envelope_lead,
        "lead": envelope_lead,
        "sender": payload.get("email_account_name") or "unknown",
        "received_at": "2026-09-10T12:00:00Z",
        "payload": dict(payload),
    }


def _lead_emails(conn, lead_id: int) -> set[str]:
    primary = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    extras = conn.execute(
        "SELECT email FROM lead_emails WHERE lead_id = ?", (lead_id,),
    ).fetchall()
    out = {primary["email"]} if primary and primary["email"] else set()
    out.update(r["email"] for r in extras)
    return {e.lower() for e in out}


def _insert_sender_account(email: str) -> None:
    conn = om.get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sender_accounts (org_id, email, channel) VALUES (?, ?, 'email')",
        (om.DEFAULT_ORG_ID, email.lower()),
    )
    conn.commit()
    conn.close()


def test_unibox_lands_as_outbound_email_sent_on_campaign_contact():
    _insert_sender_account("mailbox@send.example")
    event = _pv_event(
        "unibox_email_sent", 701, UNIBOX_PAYLOAD,
        envelope_lead="mailbox@send.example",
    )
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None

    conn = om.get_conn()
    lead = conn.execute("SELECT email, name FROM leads WHERE id = ?", (lead_id,)).fetchone()
    evt = conn.execute(
        """SELECT event_type, direction, sender, metadata_json FROM events
           WHERE lead_id = ? ORDER BY id DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()
    stage = conn.execute(
        "SELECT status FROM workspace_leads WHERE lead_id = ?", (lead_id,),
    ).fetchone()
    mailbox_leads = conn.execute(
        "SELECT id FROM leads WHERE email = ?", ("mailbox@send.example",),
    ).fetchall()
    conn.close()

    assert lead["email"] == "angie@prospect.edu"
    assert "Angie" in (lead["name"] or "")
    assert evt["event_type"] == "email_sent"
    assert evt["direction"] == "outbound"
    assert evt["sender"] == "mailbox@send.example"
    assert stage["status"] == "contacted"
    assert mailbox_leads == []
    meta = json.loads(evt["metadata_json"])
    assert meta["from_email"] == "mailbox@send.example"
    assert meta["to_email"] == "colleague@prospect.edu"
    assert meta["actual_replied_from"] == "angie@prospect.edu"
    assert meta.get("is_auto_reply") is None
    assert meta.get("lead_status_raw") in (None, "")


def test_unibox_identity_ignores_from_email_even_if_in_path(monkeypatch):
    spec = {
        **PLATFORM_SPECS["plusvibe"],
        "identity": {
            "email": ("lead_email", "from_email", "email"),
            "linkedin_url": ("linkedin_url",),
        },
    }
    monkeypatch.setitem(PLATFORM_SPECS, "plusvibe", spec)
    identity = extract_relay_identity(
        "plusvibe", UNIBOX_PAYLOAD, envelope_lead="mailbox@send.example",
    )
    assert identity["email"] == "angie@prospect.edu"


def test_unibox_same_message_id_dedupes():
    payload = dict(UNIBOX_PAYLOAD, message_id="msg-unibox-1")
    first = _pv_event("unibox_email_sent", 702, payload, envelope_lead="mailbox@send.example")
    dup = _pv_event("unibox_email_sent", 703, payload, envelope_lead="mailbox@send.example")
    assert ri.ingest_relay_event(first, force_workspace_id="ws_pv-test") is not None
    assert ri.ingest_relay_event(dup, force_workspace_id="ws_pv-test") is None
    conn = om.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'email_sent'",
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_unibox_after_sequence_send_is_second_email_sent():
    sent = _pv_event(
        "email_sent", 704,
        dict(EMAIL_SENT_PAYLOAD, email="angie@prospect.edu",
             lead_email="angie@prospect.edu", message_id="msg-seq-1"),
        envelope_lead="angie@prospect.edu",
    )
    unibox = _pv_event(
        "unibox_email_sent", 705,
        dict(UNIBOX_PAYLOAD, message_id="msg-unibox-2"),
        envelope_lead="mailbox@send.example",
    )
    lead_a = ri.ingest_relay_event(sent, force_workspace_id="ws_pv-test")
    lead_b = ri.ingest_relay_event(unibox, force_workspace_id="ws_pv-test")
    assert lead_a is not None and lead_b == lead_a
    conn = om.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE lead_id = ? AND event_type = 'email_sent'",
        (lead_a,),
    ).fetchone()[0]
    conn.close()
    assert count == 2


def test_email_sent_still_keys_on_lead_email_without_from_email():
    event = _pv_event(
        "email_sent", 706, EMAIL_SENT_PAYLOAD, envelope_lead="lead@example.com",
    )
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    lead = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    evt = conn.execute(
        "SELECT event_type, direction FROM events WHERE lead_id = ?", (lead_id,),
    ).fetchone()
    conn.close()
    assert lead["email"] == "lead@example.com"
    assert evt["event_type"] == "email_sent"
    assert evt["direction"] == "outbound"


def test_reply_alias_keys_campaign_email_not_smtp_from():
    event = _pv_event(
        "all_email_replies", 707, REPLY_ALIAS_PAYLOAD,
        envelope_lead="jane.doe@school.org",
    )
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    lead = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    evt = conn.execute(
        "SELECT event_type, direction, metadata_json FROM events WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    emails = _lead_emails(conn, lead_id)
    other = conn.execute(
        "SELECT id FROM leads WHERE email = ?", ("jane.doe@school.org",),
    ).fetchall()
    conn.close()
    assert lead["email"] == "jane@acme.org"
    assert evt["event_type"] == "email_reply"
    assert evt["direction"] == "inbound"
    meta = json.loads(evt["metadata_json"])
    assert meta["from_email"] == "jane.doe@school.org"
    assert "jane.doe@school.org" in emails
    assert other == []


def test_colleague_reply_does_not_attach_from_as_identity():
    event = _pv_event(
        "all_email_replies", 708, COLLEAGUE_REPLY_PAYLOAD,
        envelope_lead="alex.rivera@acme.org",
    )
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    lead = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    emails = _lead_emails(conn, lead_id)
    evt = conn.execute(
        "SELECT metadata_json FROM events WHERE lead_id = ?", (lead_id,),
    ).fetchone()
    colleague = conn.execute(
        "SELECT id FROM leads WHERE email = ?", ("alex.rivera@acme.org",),
    ).fetchall()
    conn.close()
    assert lead["email"] == "jordan@acme.org"
    assert "alex.rivera@acme.org" not in emails
    assert colleague == []
    meta = json.loads(evt["metadata_json"])
    assert meta["from_email"] == "alex.rivera@acme.org"


def test_reply_from_our_mailbox_stays_on_campaign_lead():
    _insert_sender_account("mailbox@send.example")
    payload = dict(REPLY_ALIAS_PAYLOAD)
    payload["from_email"] = "mailbox@send.example"
    payload["actual_replied_from"] = "mailbox@send.example"
    event = _pv_event(
        "all_email_replies", 709, payload, envelope_lead="mailbox@send.example",
    )
    lead_id = ri.ingest_relay_event(event, force_workspace_id="ws_pv-test")
    assert lead_id is not None
    conn = om.get_conn()
    lead = conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    emails = _lead_emails(conn, lead_id)
    mailbox_leads = conn.execute(
        "SELECT id FROM leads WHERE email = ?", ("mailbox@send.example",),
    ).fetchall()
    conn.close()
    assert lead["email"] == "jane@acme.org"
    assert "mailbox@send.example" not in emails
    assert mailbox_leads == []


def test_reprocess_event_type_filter_keeps_unibox_and_replies():
    from reprocess import _filter_replay_events, _parse_event_types

    events = [
        {"event_type": "email_sent", "relay_id": 1},
        {"event_type": "unibox_email_sent", "relay_id": 2},
        {"event_type": "all_email_replies", "relay_id": 3},
        {"event_type": "EMAIL_SENT", "relay_id": 4},
    ]
    types = _parse_event_types("unibox_email_sent,all_email_replies")
    out = _filter_replay_events(events, types)
    assert [e["relay_id"] for e in out] == [2, 3]
    assert _filter_replay_events(events, None) == events
    assert _parse_event_types("  ") is None
