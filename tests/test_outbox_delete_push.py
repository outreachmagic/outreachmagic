"""Locally-deleted leads/workspace-memberships/companies must tell the relay.

Before this, only merges pushed a delete (via lead_merges/company_merges);
any other delete (a direct delete, a workspace removal) left the outbox
op='delete' tombstone unconsumed forever and the relay holding a permanent
ghost copy. lead_workspace deletes had no push path at all.

The BEFORE DELETE triggers capture the *raw* uid column into the outbox
row's entity_key, not the `uid:`-prefixed form every other push path (and
the relay's stored entity_key) actually uses -- that mismatch is the crux of
why this needs its own coverage, not just "does a push happen."
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import outbox  # noqa: E402
import pipeline as om  # noqa: E402
import pipeline_workspace  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_lead_in_ws(conn, email="a@example.com", slug="default"):
    cur = conn.execute(
        "INSERT INTO leads (name, email, company) VALUES ('A', ?, 'Acme')", (email,)
    )
    lead_id = cur.lastrowid
    ws = conn.execute("SELECT id FROM workspaces WHERE slug = ?", (slug,)).fetchone()["id"]
    org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id) VALUES (?, ?, ?, ?)",
        (f"{ws}:{lead_id}", org, ws, lead_id),
    )
    conn.commit()
    return lead_id, ws


class _Capture:
    """Stand-in for the relay: records what was pushed, acks it."""

    def __init__(self, error=None):
        self.entries = []
        self.error = error

    def __call__(self, agent_key, entries, client_id, **kw):
        self.entries.extend(entries)
        if self.error:
            return {"pushed": 0, "error": self.error, "throttled": False}
        return {"pushed": len(entries), "error": None, "throttled": False}


def test_lead_core_delete_entry_gets_uid_prefix_not_raw_uid():
    """The critical bug: the trigger stores the bare uid, but the relay's
    entity_key is uid:<uid>. Sending the bare form would match zero rows on
    the relay -- a silent, invisible no-op."""
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    raw_uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()

    captured_key = conn.execute(
        "SELECT entity_key FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
    ).fetchone()["entity_key"]
    conn.close()
    assert captured_key == raw_uid, "trigger is expected to capture the bare uid (this is the bug)"

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        result = om._push_pending_lead_core_outbox_deletes("om_agent_test")

    assert result["pushed"] == 1
    assert len(cap.entries) == 1
    entry = cap.entries[0]
    assert entry["action"] == "lead_core_delete"
    assert entry["entity_key"] == f"uid:{raw_uid}", "must be prefixed to match the relay's real key"


def test_lead_workspace_delete_carries_workspace_field():
    conn = om.get_conn()
    lead_id, ws_id = _mk_lead_in_ws(conn)
    raw_uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
    conn.execute("DELETE FROM workspace_leads WHERE lead_id = ? AND workspace_id = ?", (lead_id, ws_id))
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        result = om._push_pending_lead_workspace_outbox_deletes("om_agent_test")

    assert result["pushed"] == 1
    entry = cap.entries[0]
    assert entry["action"] == "lead_workspace_delete"
    assert entry["entity_key"] == f"uid:{raw_uid}"
    assert entry["workspace"] == "default"


def test_company_delete_outside_merge_is_pushed():
    conn = om.get_conn()
    company_id = om.ensure_company(conn, name="Acme", domain="acme.example.com")
    conn.commit()
    raw_uid = conn.execute("SELECT uid FROM companies WHERE id = ?", (company_id,)).fetchone()["uid"]
    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        result = om._push_pending_company_outbox_deletes("om_agent_test")

    assert result["pushed"] == 1
    entry = cap.entries[0]
    assert entry["action"] == "company_core_delete"
    assert entry["entity_key"] == f"uid:{raw_uid}"


def test_sender_domain_delete_uses_sender_domain_prefix_not_uid():
    # sender_domains are keyed sender_domain:<domain>, never uid:. The trigger
    # captures the bare OLD.domain, so the push must apply the sender_domain
    # transform (and strip stray quotes) rather than the blanket uid: prefix.
    import pipeline_sender_accounts as psa

    psa.set_sender_domain_cost("deleteme.example.com", purpose="branch")
    conn = om.get_conn()
    conn.execute("DELETE FROM sender_domains WHERE domain = ?", ("deleteme.example.com",))
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        result = om._push_pending_sender_domain_outbox_deletes("om_agent_test")

    assert result["pushed"] == 1
    entry = cap.entries[0]
    assert entry["action"] == "sender_domain_delete"
    assert entry["entity_key"] == "sender_domain:deleteme.example.com"
    assert not entry["entity_key"].startswith("uid:")


def test_successful_delete_push_clears_outbox_and_stale_shadow():
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    raw_uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
    # A pre-existing shadow row for this key (from before it was deleted) --
    # must be cleaned up too, since there's no content left to compare against.
    conn.execute(
        "INSERT INTO sync_shadow (entity_type, entity_key, workspace_slug, content_hash, synced_at) "
        "VALUES ('lead_core', ?, '', 'oldhash', datetime('now'))",
        (f"uid:{raw_uid}",),
    )
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()

    with mock.patch.object(om, "_relay_push_batches", side_effect=_Capture()):
        om._push_pending_lead_core_outbox_deletes("om_agent_test")

    conn = om.get_conn()
    remaining_outbox = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
    ).fetchone()["n"]
    remaining_shadow = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_shadow WHERE entity_type = 'lead_core' AND entity_key = ?",
        (f"uid:{raw_uid}",),
    ).fetchone()["n"]
    conn.close()
    assert remaining_outbox == 0, "the tombstone must clear once the relay acks the delete"
    assert remaining_shadow == 0, "a shadow row for a deleted entity is stale and must go too"


def test_failed_delete_push_keeps_the_row_dirty_with_backoff():
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()

    cap = _Capture(error="relay 503")
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_core_outbox_deletes("om_agent_test")

    conn = om.get_conn()
    row = conn.execute(
        "SELECT attempts, last_error FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
    ).fetchone()
    conn.close()
    assert row is not None, "a failed delete push must leave the tombstone dirty"
    assert row["attempts"] == 1
    assert "503" in row["last_error"]


def test_record_failure_upsert_default_unaffected_by_op_parameter():
    """outbox.record_failure's new `op` kwarg must default to 'upsert' so
    every existing (unmodified) caller keeps working."""
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.close()

    conn = om.get_conn()
    outbox.record_failure(conn, "lead_core", [str(lead_id)], "boom")
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM outbox WHERE entity_type = 'lead_core' AND entity_id = ? AND op = 'upsert'",
        (str(lead_id),),
    ).fetchone()
    conn.close()
    assert row["attempts"] == 1


def test_dry_run_does_not_touch_anything():
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()

    result = om._push_pending_lead_core_outbox_deletes("om_agent_test", dry_run=True)
    assert result["pushed"] == 0
    assert result["total_pending"] == 1
    assert len(result["sample_entries"]) == 1

    conn = om.get_conn()
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
    ).fetchone()["n"]
    conn.close()
    assert remaining == 1, "dry-run must not clear anything"


def test_sync_all_pushes_workspace_delete_and_clears_it(monkeypatch):
    """sync_all() must actually invoke the new lead_workspace delete push --
    the whole gap this fixes is that nothing did, ever."""
    import routing_cloud

    conn = om.get_conn()
    lead_id, ws_id = _mk_lead_in_ws(conn)
    conn.execute("DELETE FROM workspace_leads WHERE lead_id = ? AND workspace_id = ?", (lead_id, ws_id))
    conn.commit()
    conn.close()

    monkeypatch.setattr(om, "get_agent_key", lambda: "fake-agent-key")
    monkeypatch.setattr(routing_cloud, "cloud_routing_enabled", lambda *a, **k: True)
    monkeypatch.setattr(om, "get_sync_status", lambda org_id=None: {
        "can_sync": True,
        "synced": False,
        "leads_pending": 0,
        "workspace_leads_pending": 0,
        "pending_workspaces": [],
        "pending_rules": [],
        "local_agent_events": 0,
        "recommended_mode": "push",
    })

    def _noop_push(*args, **kwargs):
        return {}

    for fn_name in (
        "_push_agent_events_to_relay",
        "_push_pending_company_updates",
        "_push_pending_lead_snapshots",
        "_push_pending_merge_deletes",
        "_push_pending_company_merge_deletes",
        "_push_pending_lead_core_outbox_deletes",
        "_push_pending_company_outbox_deletes",
        "_push_pending_quarantine_resolutions",
        "_push_pending_sender_account_updates",
        "_push_pending_sender_domain_updates",
    ):
        monkeypatch.setattr(om, fn_name, _noop_push)

    with mock.patch.object(om, "_relay_push_batches", side_effect=_Capture()):
        result = pipeline_workspace.sync_all(no_health_report=True)

    assert result["status"] == "ok"
    assert result["lead_workspace_outbox_deletes_pushed"] == 1

    conn = om.get_conn()
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_workspace' AND op = 'delete'"
    ).fetchone()["n"]
    conn.close()
    assert remaining == 0


def test_preview_sync_reports_pending_deletes_without_pushing(monkeypatch):
    import routing_cloud

    conn = om.get_conn()
    lead_id, ws_id = _mk_lead_in_ws(conn)
    conn.execute("DELETE FROM workspace_leads WHERE lead_id = ? AND workspace_id = ?", (lead_id, ws_id))
    conn.commit()
    conn.close()

    monkeypatch.setattr(om, "get_agent_key", lambda: "fake-agent-key")
    monkeypatch.setattr(routing_cloud, "cloud_routing_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        routing_cloud, "fetch_routing_bundle",
        lambda *a, **k: {"workspaces": [], "campaignMaps": []},
    )

    result = om.preview_sync(sample_size=3)
    assert result["status"] == "dry_run"
    assert result["totals"]["lead_workspace_outbox_deletes_pending"] == 1
    assert len(result["samples"]["lead_workspace_delete"]) == 1

    conn = om.get_conn()
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_workspace' AND op = 'delete'"
    ).fetchone()["n"]
    conn.close()
    assert remaining == 1, "preview must not push or clear anything"
