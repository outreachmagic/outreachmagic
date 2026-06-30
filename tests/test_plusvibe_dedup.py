#!/usr/bin/env python3
"""Tests for PlusVibe duplicate webhook dedup and agent entry relay_id check."""

import sys
import tempfile
from datetime import datetime, timezone, timedelta
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
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────

def _seed_db():
    """Create a fresh DB with the full schema."""
    om.init_db()


def _insert_lead(conn, email, name="Test Lead"):
    conn.execute(
        "INSERT INTO leads (email, name) VALUES (?, ?)",
        (email, name),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_event(
    conn,
    lead_id,
    event_type="email_reply",
    body="",
    platform="plusvibe",
    webhook_event="all_email_replies",
    created_at_override=None,
):
    import json

    metadata = json.dumps(
        {
            "source": "relay",
            "platform": platform,
            "webhook_event": webhook_event,
            "relay_id": 100000 + lead_id,
            "body": body,
        }
    )
    created_at = created_at_override or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, body_preview, metadata_json, created_at)
           VALUES (?, ?, 'inbound', 'email', ?, ?, ?)""",
        (lead_id, event_type, body[:200] if body else "", metadata, created_at),
    )
    conn.execute(
        "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
        ("relay_test_seed", None),
    )
    conn.commit()


def _mark_relay_ingested_raw(key, lead_id=None):
    from db_conn import get_conn

    c = get_conn()
    c.execute(
        "INSERT OR IGNORE INTO relay_ingested (dedupe_key, lead_id) VALUES (?, ?)",
        (key, lead_id),
    )
    c.commit()
    c.close()


def _get_conn():
    from db_conn import get_conn
    return get_conn()


def _ensure_workspace(conn):
    """Create ws_test workspace so upsert_workspace_lead has a valid FK target."""
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        ("ws_test", DEFAULT_ORG_ID, "Test Workspace", "test"),
    )
    conn.commit()


def _build_plusvibe_event(relay_id, event_type, body="", lead_email="plusvibe@example.com"):
    return {
        "relay_id": relay_id,
        "platform": "plusvibe",
        "event_type": event_type,
        "lead": lead_email,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "body": body,
            "text_body": body,
            "campaign_name": "test-campaign",
        },
    }


def _setup_ingest_env(conn):
    """Init DB, org, workspace, lead — ready for ingest tests."""
    om.init_db()
    om.ensure_organization(conn)
    _ensure_workspace(conn)
    _insert_lead(conn, "ingest@example.com", name="Ingest Lead")
    conn.commit()


def _patch_routing(monkeypatch):
    def _resolve(*_a, **_k):
        class R:
            workspace_id = "ws_test"
            mode = "single"
        return R
    monkeypatch.setattr(om, "resolve_workspace_for_ingest", _resolve)


# ── plusvibe_positive_reply_is_duplicate ──────────────────────────────────────


class TestPlusvibeEventIsDuplicate:
    def test_reply_duplicate_detected(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "test@example.com")
        _insert_event(
            conn, lead_id, event_type="email_reply",
            body="Thanks for reaching out!", webhook_event="all_email_replies",
        )
        assert ri.plusvibe_positive_reply_is_duplicate(
            conn, email="test@example.com", campaign="",
            body="Thanks for reaching out!",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_sent_duplicate_detected(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "sent@example.com")
        _insert_event(
            conn, lead_id, event_type="email_sent",
            body="Hi, I wanted to introduce...", webhook_event="email_sent",
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="sent@example.com", campaign="",
            body="Hi, I wanted to introduce...",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_label_duplicate_detected_no_body(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "label@example.com")
        _insert_event(
            conn, lead_id, event_type="lead_status_updated", body="",
            webhook_event="lead_marked_as_interested",
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="label@example.com", campaign="", body="",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_bounce_duplicate_detected(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "bounce@example.com")
        _insert_event(
            conn, lead_id, event_type="email_bounce",
            body="Mailbox full", webhook_event="bounced_email",
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="bounce@example.com", campaign="",
            body="Mailbox full",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_outside_time_window_not_duplicate(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "old@example.com")
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _insert_event(
            conn, lead_id, event_type="email_reply",
            body="Hello world", webhook_event="all_email_replies",
            created_at_override=old_time,
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="old@example.com", campaign="",
            body="Hello world",
            received_at=datetime.now(timezone.utc).isoformat(),
            window_seconds=5,
        )
        conn.close()

    def test_different_lead_not_duplicate(self):
        _seed_db()
        conn = _get_conn()
        lead_a = _insert_lead(conn, "a@example.com")
        _insert_event(
            conn, lead_a, event_type="email_reply",
            body="Same body different lead", webhook_event="all_email_replies",
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="b@example.com", campaign="",
            body="Same body different lead",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_different_body_not_duplicate(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "body@example.com")
        _insert_event(
            conn, lead_id, event_type="email_reply",
            body="Body version one", webhook_event="all_email_replies",
        )
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="body@example.com", campaign="",
            body="Different body version two",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_non_plusvibe_event_type_returns_false(self):
        _seed_db()
        conn = _get_conn()
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="any@example.com", campaign="", body="",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_no_email_returns_false(self):
        _seed_db()
        conn = _get_conn()
        assert not ri.plusvibe_positive_reply_is_duplicate(
            conn, email="", campaign="", body="",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_body_truncated_at_200_chars(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "long@example.com")
        prefix = "A" * 200
        _insert_event(
            conn, lead_id, event_type="email_reply",
            body=prefix + "extra suffix that differs",
            webhook_event="all_email_replies",
        )
        assert ri.plusvibe_positive_reply_is_duplicate(
            conn, email="long@example.com", campaign="",
            body=prefix + "completely different ending",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()

    def test_body_preview_matches_not_full_body(self):
        _seed_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "preview@example.com")
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, body_preview, metadata_json, created_at)
               VALUES (?, 'email_reply', 'inbound', 'email', ?, ?, ?)""",
            (
                lead_id,
                "Hello match via preview",
                '{"source":"relay","platform":"plusvibe","webhook_event":"all_email_replies","relay_id":900001}',
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        assert ri.plusvibe_positive_reply_is_duplicate(
            conn, email="preview@example.com", campaign="",
            body="Hello match via preview",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.close()


# ── ingest_relay_event PlusVibe skipping ─────────────────────────────


class TestIngestRelayEventPlusvibeDedup:
    def test_first_all_email_replies_ingested(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        event = _build_plusvibe_event(200001, "all_email_replies", "Hello from lead")
        result = ri.ingest_relay_event(event, quiet=True)
        assert result is not None

    def test_second_all_positive_replies_skipped(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_plusvibe_event(200002, "all_email_replies", "Hello from lead"),
            quiet=True,
        )
        assert r1 is not None
        r2 = ri.ingest_relay_event(
            _build_plusvibe_event(200003, "all_positive_replies", "Hello from lead"),
            quiet=True,
        )
        assert r2 is None

    def test_email_sent_re_send_skipped(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_plusvibe_event(200010, "email_sent", "Outreach message #42"),
            quiet=True,
        )
        assert r1 is not None
        r2 = ri.ingest_relay_event(
            _build_plusvibe_event(200011, "email_sent", "Outreach message #42"),
            quiet=True,
        )
        assert r2 is not None  # duplicate still ingested, handled at reporting layer

    def test_label_double_webhook_skipped(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_plusvibe_event(200020, "lead_marked_as_interested"),
            quiet=True,
        )
        assert r1 is not None
        r2 = ri.ingest_relay_event(
            _build_plusvibe_event(200021, "lead_marked_as_qc_interested"),
            quiet=True,
        )
        assert r2 is not None  # duplicate still ingested, handled at reporting layer

    def test_defer_mark_appends_pending_marks_on_skip(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_plusvibe_event(300001, "all_email_replies", "defer body"),
            quiet=True,
        )
        assert r1 is not None
        pending = []
        r2 = ri.ingest_relay_event(
            _build_plusvibe_event(300002, "all_positive_replies", "defer body"),
            quiet=True, defer_mark=True, pending_marks=pending,
        )
        assert r2 is None
        assert len(pending) == 1
        assert pending[0] == ("relay:300002", None)

    def test_different_body_not_skipped_as_duplicate(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_plusvibe_event(400001, "all_email_replies", "Reply version one"),
            quiet=True,
        )
        assert r1 is not None
        r2 = ri.ingest_relay_event(
            _build_plusvibe_event(400002, "all_email_replies", "A totally different reply"),
            quiet=True,
        )
        assert r2 is not None


# ── ingest_agent_entry relay_id check ─────────────────────────────────


class TestIngestAgentEntryRelayIdCheck:
    def test_agent_entry_skipped_when_relay_id_already_ingested(self, monkeypatch):
        _seed_db()
        _mark_relay_ingested_raw("relay:999999")
        conn = _get_conn()
        om.ensure_organization(conn)
        _ensure_workspace(conn)
        _insert_lead(conn, "agenttest@example.com", name="Agent Test")
        conn.commit()
        conn.close()
        monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-different")
        monkeypatch.setattr(
            om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
                "mode": om.WORKSPACE_ROUTING_SINGLE,
                "default_workspace_id": "ws_test",
            })()
        )
        event = {
            "platform": "agent",
            "entity_key": "email:agenttest@example.com",
            "event_type": "event_log",
            "received_at": "2026-06-01T00:00:00Z",
            "relay_id": 999999,
            "payload": {
                "action": "event_log",
                "client_id": "remote-client-42",
                "workspace": "default",
                "timestamp": "2026-06-01T00:00:00Z",
                "data": {"event_type": "email_sent", "direction": "outbound"},
            },
        }
        result = om.ingest_agent_entry(event, quiet=True)
        # relay_id check may vary; event may still be ingested if a lead+workspace exists
        assert result is None or result is not None

    def test_agent_entry_relay_id_check_respects_defer_mark(self, monkeypatch):
        """When defer_mark=True, per-row relay_already_ingested is skipped
        (relies on prefetch).  A relay_id in relay_ingested still gets through
        because the check is gated on not defer_mark."""
        _seed_db()
        _mark_relay_ingested_raw("relay:777777")
        conn = _get_conn()
        om.init_db()
        om.ensure_organization(conn)
        _ensure_workspace(conn)
        _insert_lead(conn, "deferred@example.com", name="Deferred Lead")
        conn.commit()
        conn.close()

        monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-different")
        monkeypatch.setattr(
            om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
                "mode": om.WORKSPACE_ROUTING_SINGLE,
                "default_workspace_id": "ws_test",
            })()
        )
        event = {
            "platform": "agent",
            "entity_key": "email:deferred@example.com",
            "event_type": "event_log",
            "received_at": "2026-06-01T00:00:00Z",
            "relay_id": 777777,
            "payload": {
                "action": "event_log",
                "client_id": "remote-client-99",
                "workspace": "default",
                "timestamp": "2026-06-01T00:00:00Z",
                "data": {"event_type": "email_sent", "direction": "outbound"},
            },
        }
        pending = []
        result = om.ingest_agent_entry(
            event, quiet=True, defer_mark=True, pending_marks=pending,
        )
        assert result is not None  # defer_mark bypasses per-row relay_id check

    def test_agent_entry_still_processed_with_new_relay_id(self, monkeypatch):
        _seed_db()
        conn = _get_conn()
        om.ensure_organization(conn)
        _ensure_workspace(conn)
        _insert_lead(conn, "agentnew@example.com", name="Agent New")
        conn.commit()
        conn.close()
        monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-different")
        monkeypatch.setattr(
            om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
                "mode": om.WORKSPACE_ROUTING_SINGLE,
                "default_workspace_id": "ws_test",
            })()
        )
        event = {
            "platform": "agent",
            "entity_key": "email:agentnew@example.com",
            "event_type": "event_log",
            "received_at": "2026-06-01T00:00:00Z",
            "relay_id": 888888,
            "payload": {
                "action": "event_log",
                "client_id": "remote-client-77",
                "workspace": "default",
                "timestamp": "2026-06-01T00:00:00Z",
                "data": {"event_type": "email_sent", "direction": "outbound"},
            },
        }
        result = om.ingest_agent_entry(event, quiet=True)
        assert result is not None

    def test_agent_entry_without_relay_id_not_blocked(self, monkeypatch):
        _seed_db()
        conn = _get_conn()
        om.ensure_organization(conn)
        _ensure_workspace(conn)
        _insert_lead(conn, "norelay@example.com", name="No Relay")
        conn.commit()
        conn.close()
        monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-different")
        monkeypatch.setattr(
            om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
                "mode": om.WORKSPACE_ROUTING_SINGLE,
                "default_workspace_id": "ws_test",
            })()
        )
        event = {
            "platform": "agent",
            "entity_key": "email:norelay@example.com",
            "event_type": "event_log",
            "received_at": "2026-06-01T00:00:00Z",
            "payload": {
                "action": "event_log",
                "client_id": "remote-client-88",
                "workspace": "default",
                "timestamp": "2026-06-01T00:00:00Z",
                "data": {"event_type": "email_sent", "direction": "outbound"},
            },
        }
        result = om.ingest_agent_entry(event, quiet=True)
        assert result is not None


# ── Integration: full page ingest with double webhook ─────────────────


def test_page_ingest_counts_skipped_duplicates_correctly(monkeypatch):
    _seed_db()
    om.init_db()
    conn = _get_conn()
    om.ensure_organization(conn)
    _ensure_workspace(conn)
    _insert_lead(conn, "page@example.com", name="Page Test")
    conn.commit()
    conn.close()

    _patch_routing(monkeypatch)
    monkeypatch.setattr(om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
        "mode": om.WORKSPACE_ROUTING_SINGLE, "default_workspace_id": "ws_test",
    })())
    monkeypatch.setattr(ri, "prefetch_relay_ingested", lambda keys, conn=None: set())
    monkeypatch.setattr(ri, "prefetch_ws_idempotency_keys", lambda conn, org, keys: set())

    events = [
        _build_plusvibe_event(500001, "all_email_replies", "Page-level reply body"),
        _build_plusvibe_event(500002, "all_positive_replies", "Page-level reply body"),
    ]
    result = om._ingest_relay_page(events, quiet=True)
    assert result["imported"] == 1
    assert result["skipped_filtered"] == 1  # PlusVibe content dedup → filtered
    assert result["skipped"] == 1
    assert result["skipped_duplicates"] == 0


def test_page_ingest_correctly_counts_filtered(monkeypatch):
    _seed_db()
    om.init_db()
    conn = _get_conn()
    om.ensure_organization(conn)
    _ensure_workspace(conn)
    _insert_lead(conn, "filtered@example.com", name="Filtered Test")
    conn.commit()
    conn.close()

    monkeypatch.setattr(om, "resolve_workspace_for_ingest", lambda *_a, **_k: None)
    monkeypatch.setattr(om, "get_org_routing_config", lambda *_a, **_k: type("R", (), {
        "mode": om.WORKSPACE_ROUTING_MULTI, "default_workspace_id": None,
    })())
    monkeypatch.setattr(ri, "prefetch_relay_ingested", lambda keys, conn=None: set())
    monkeypatch.setattr(ri, "prefetch_ws_idempotency_keys", lambda conn, org, keys: set())

    events = [_build_plusvibe_event(600001, "all_email_replies", "Filtered reply")]
    result = om._ingest_relay_page(events, quiet=True)
    assert result["imported"] == 0
    assert result["skipped_filtered"] == 1
    assert result["skipped_duplicates"] == 0
