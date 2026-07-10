#!/usr/bin/env python3
"""Tests for Prosp duplicate webhook dedup (send_msg / linkedin_dm_message_sent
co-fire) and the PlusVibe message_id dedup key reorder.

See d1-webhook-dedup-fix-plan.md.
"""

import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import relay_ingest as ri  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


def _get_conn():
    from db_conn import get_conn
    return get_conn()


def _insert_lead(conn, email, name="Test Lead"):
    conn.execute("INSERT INTO leads (email, name) VALUES (?, ?)", (email, name))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_linkedin_event(conn, lead_id, *, created_at_override=None):
    created_at = created_at_override or om.utc_now_for_storage()
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, body_preview, metadata_json, created_at)
           VALUES (?, 'linkedin_message', 'outbound', 'linkedin', '', '{}', ?)""",
        (lead_id, created_at),
    )
    conn.commit()


def _ensure_workspace(conn):
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        ("ws_test", DEFAULT_ORG_ID, "Test Workspace", "test"),
    )
    conn.commit()


def _setup_ingest_env(conn):
    om.init_db()
    om.ensure_organization(conn)
    _ensure_workspace(conn)
    conn.commit()


def _patch_routing(monkeypatch):
    def _resolve(*_a, **_k):
        class R:
            workspace_id = "ws_test"
            mode = "single"
        return R
    monkeypatch.setattr(om, "resolve_workspace_for_ingest", _resolve)


def _build_prosp_event(relay_id, event_type, *, lead_email, extra_payload=None):
    payload = {
        "campaign_id": "campaign-1",
        "campaign_name": "acme_corp | nace",
        "sender": "linkedin.com/in/janedoe",
        "lead_email": lead_email,
    }
    payload.update(extra_payload or {})
    return {
        "relay_id": relay_id,
        "platform": "prosp",
        "event_type": event_type,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


# ── prosp_linkedin_dm_is_duplicate (unit) ────────────────────────────────

class TestProspLinkedinDmIsDuplicate:
    def test_within_window_is_duplicate(self):
        om.init_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "dm@example.com")
        _insert_linkedin_event(conn, lead_id)
        assert ri.prosp_linkedin_dm_is_duplicate(
            conn, lead_id=lead_id, event_at=om.utc_now_for_storage(),
        )
        conn.close()

    def test_outside_window_not_duplicate(self):
        om.init_db()
        conn = _get_conn()
        lead_id = _insert_lead(conn, "old-dm@example.com")
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_linkedin_event(conn, lead_id, created_at_override=old_time)
        assert not ri.prosp_linkedin_dm_is_duplicate(
            conn, lead_id=lead_id, event_at=om.utc_now_for_storage(), window_seconds=10,
        )
        conn.close()

    def test_different_lead_not_duplicate(self):
        om.init_db()
        conn = _get_conn()
        lead_a = _insert_lead(conn, "a-dm@example.com")
        _insert_lead(conn, "b-dm@example.com")
        _insert_linkedin_event(conn, lead_a)
        assert not ri.prosp_linkedin_dm_is_duplicate(
            conn, lead_id=lead_a + 1, event_at=om.utc_now_for_storage(),
        )
        conn.close()

    def test_no_lead_id_returns_false(self):
        om.init_db()
        conn = _get_conn()
        assert not ri.prosp_linkedin_dm_is_duplicate(
            conn, lead_id=None, event_at=om.utc_now_for_storage(),
        )
        conn.close()


# ── ingest_relay_event Prosp co-fire skipping (integration) ─────────────

class TestIngestRelayEventProspDedup:
    def test_linkedin_dm_message_sent_after_send_msg_skipped(self, monkeypatch):
        """send_msg then linkedin_dm_message_sent for the same DM -- the
        second (different relay_id, same lead+time window) must be skipped."""
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        r1 = ri.ingest_relay_event(
            _build_prosp_event(
                400001, "send_msg",
                lead_email="linkedin.com/in/johnsmith06",
                extra_payload={
                    "firstName": "John", "lastName": "Smith",
                    "linkedinUrl": "https://www.linkedin.com/in/johnsmith06",
                },
            ),
            quiet=True,
        )
        assert r1 is not None

        r2 = ri.ingest_relay_event(
            _build_prosp_event(
                400002, "linkedin_dm_message_sent",
                lead_email="john.smith@example.com",
                extra_payload={
                    "body_preview": "Hi John, Thanks for connecting!...",
                    "lead_linkedin": "linkedin.com/in/johnsmith06",
                },
            ),
            quiet=True,
        )
        assert r2 is None

    def test_first_send_msg_ingested(self, monkeypatch):
        _setup_ingest_env(_get_conn())
        _patch_routing(monkeypatch)
        result = ri.ingest_relay_event(
            _build_prosp_event(
                400010, "send_msg",
                lead_email="linkedin.com/in/soloevent",
                extra_payload={
                    "firstName": "Solo", "lastName": "Event",
                    "linkedinUrl": "https://www.linkedin.com/in/soloevent",
                },
            ),
            quiet=True,
        )
        assert result is not None


# ── relay_dedupe_key Fix C: message_id before relay_id for PlusVibe ─────

def test_plusvibe_dedupe_key_prioritizes_message_id():
    event_a = {
        "relay_id": 111,
        "platform": "plusvibe",
        "payload": {"webhook_id": "wh-a", "message_id": "shared-msg-id"},
    }
    event_b = {
        "relay_id": 222,
        "platform": "plusvibe",
        "payload": {"webhook_id": "wh-b", "message_id": "shared-msg-id"},
    }
    key_a = ri.relay_dedupe_key(event_a)
    key_b = ri.relay_dedupe_key(event_b)
    assert key_a == key_b == "msg:shared-msg-id"


def test_non_plusvibe_dedupe_key_still_uses_relay_id():
    event = {"relay_id": 333, "platform": "prosp", "payload": {"message_id": "irrelevant"}}
    assert ri.relay_dedupe_key(event) == "relay:333"
