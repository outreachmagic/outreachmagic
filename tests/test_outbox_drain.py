"""Stage 5: the push loop drains the outbox instead of guessing from updated_at.

These assert the *push*, not just the dirty mark. test_outbox_triggers proves an
outbox row appears; this proves it actually reaches the relay -- and that the
three writes which used to be silently dropped now do.
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
from sync_audit import content_hash  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_lead_in_ws(conn, email="a@example.com"):
    cur = conn.execute(
        "INSERT INTO leads (name, email, company) VALUES ('A', ?, 'Acme')", (email,)
    )
    lead_id = cur.lastrowid
    ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()["id"]
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

    def actions(self):
        return [e["action"] for e in self.entries]


def test_provider_attempt_actually_reaches_the_relay():
    """The bug: record_provider_attempt() bumped no parent timestamp, so the
    updated_at cursor never selected the lead and the attempt never shipped."""
    from pipeline_provider_attempts import record_provider_attempt

    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.close()

    # Drain whatever the lead's creation queued, so we isolate the attempt.
    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")

    conn = om.get_conn()
    record_provider_attempt(conn, lead_id, "trykitt", status="pending")
    conn.commit()
    conn.close()

    cap2 = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap2):
        res = om._push_pending_lead_snapshots("om_agent_test")

    assert "lead_core_update" in cap2.actions(), (
        "a provider attempt must push its lead's core snapshot"
    )
    assert res["pushed"] >= 1


def test_echo_is_dropped_by_content_not_pushed_again():
    """A pulled snapshot writes locally, which fires the triggers. Without the
    hash compare, every pull would immediately push back what it just pulled."""
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")
    assert cap.entries, "first push should ship the new lead"

    # sync_shadow now records what the relay holds. Re-dirty the row without
    # changing any content -- exactly what applying a pulled snapshot does.
    conn = om.get_conn()
    conn.execute("UPDATE leads SET company = 'Acme' WHERE id = ?", (lead_id,))
    conn.commit()
    assert outbox.count_dirty(conn).get("lead_core:upsert", 0) >= 1
    conn.close()

    cap2 = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap2):
        om._push_pending_lead_snapshots("om_agent_test")

    assert "lead_core_update" not in cap2.actions(), (
        "unchanged content must not be pushed a second time"
    )
    conn = om.get_conn()
    assert outbox.count_dirty(conn).get("lead_core:upsert", 0) == 0, (
        "the echo must also leave the outbox, or it rebuilds forever"
    )
    conn.close()


def test_real_change_after_a_sync_is_pushed():
    """The flip side of the echo test: a genuine edit must still go.

    Uses `name`, which is on the wire. Note `leads.company` deliberately is not
    in the lead_core payload -- editing it produces a byte-identical payload and
    is (correctly) dropped as an echo. That asymmetry is what Stage 6's contract
    test exists to make explicit rather than surprising.
    """
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.close()

    with mock.patch.object(om, "_relay_push_batches", side_effect=_Capture()):
        om._push_pending_lead_snapshots("om_agent_test")

    conn = om.get_conn()
    conn.execute("UPDATE leads SET name = 'Renamed' WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")

    assert "lead_core_update" in cap.actions()
    payloads = [e["payload"] for e in cap.entries if e["action"] == "lead_core_update"]
    assert any(p.get("name") == "Renamed" for p in payloads)


def test_provider_attempt_payload_actually_carries_the_attempt():
    """Marking the lead dirty is worthless if the attempt is not serialized.
    (lead_email_verification is *not* on the wire yet -- that is Stage 7.)"""
    from pipeline_provider_attempts import record_provider_attempt

    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    conn.close()

    with mock.patch.object(om, "_relay_push_batches", side_effect=_Capture()):
        om._push_pending_lead_snapshots("om_agent_test")

    conn = om.get_conn()
    record_provider_attempt(
        conn, lead_id, "trykitt", status="completed", result_email="a@example.com"
    )
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")

    core = [e for e in cap.entries if e["action"] == "lead_core_update"]
    assert core, "provider attempt must trigger a core push"
    assert any("provider_attempts" in e["payload"] for e in core), (
        "the attempt must be on the wire, not merely marked dirty"
    )


def test_successful_push_clears_outbox_and_records_shadow():
    conn = om.get_conn()
    _mk_lead_in_ws(conn)
    conn.close()

    with mock.patch.object(om, "_relay_push_batches", side_effect=_Capture()):
        om._push_pending_lead_snapshots("om_agent_test")

    conn = om.get_conn()
    assert outbox.count_dirty(conn).get("lead_core:upsert", 0) == 0
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_shadow WHERE entity_type = 'lead_core'"
    ).fetchone()["n"]
    assert n == 1, "the relay's content must be remembered, or the next push re-sends it"
    conn.close()


def test_failed_push_keeps_the_row_dirty():
    """A network blip must not silently eat a local change."""
    conn = om.get_conn()
    _mk_lead_in_ws(conn)
    conn.close()

    cap = _Capture(error="relay 503")
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")

    conn = om.get_conn()
    row = conn.execute(
        "SELECT attempts, last_error FROM outbox "
        "WHERE entity_type = 'lead_core' AND op = 'upsert'"
    ).fetchone()
    assert row is not None, "a failed push must leave the row dirty"
    assert row["attempts"] == 1
    assert "503" in row["last_error"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM sync_shadow"
    ).fetchone()["n"] == 0, "never record a shadow for content the relay did not ack"
    conn.close()


def test_pulled_snapshot_does_not_bounce_straight_back():
    """Applying a pulled snapshot writes locally, which fires the triggers. If
    the pull did not seed sync_shadow, every pull would push its entire contents
    straight back to the relay, forever.

    The payload used here is the one the relay actually holds -- i.e. the one we
    pushed. That distinction matters: applying a snapshot *enriches* the lead
    (build_lead_core_sync_payload derives company_domain from the email address),
    so a payload that predates that enrichment legitimately differs from what we
    would rebuild, and pushing it back is convergence rather than an echo. Once
    the relay holds the enriched content the hashes agree and the traffic stops.
    """
    from pipeline_sync import ingest_agent_entry

    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    entity_key = om.lead_entity_key(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()

    def push_pull_round():
        """One full round trip: drain to the relay, then have the relay hand the
        same content back. Returns how many core snapshots we pushed."""
        cap = _Capture()
        with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
            om._push_pending_lead_snapshots("om_agent_test")
        core = [e for e in cap.entries if e["action"] == "lead_core_update"]
        for e in core:
            ingest_agent_entry({
                "action": "lead_core_update",
                "entity_key": entity_key,
                "client_id": "some-other-client",
                "payload": e["payload"],
                "timestamp": "2026-07-14T00:00:00Z",
            }, quiet=True)
        return len(core)

    # Round 1 ships the lead. Round 2 ships the enrichment the apply path added
    # (company_domain, derived from the email) -- genuinely new content, so it is
    # convergence, not an echo.
    push_pull_round()
    push_pull_round()

    conn = om.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM sync_shadow WHERE entity_type = 'lead_core'"
    ).fetchone()["n"] >= 1, "the pull must record what the relay holds"
    conn.close()

    # Now it must be quiet. If the pull did not seed sync_shadow, this would push
    # forever -- every pull re-dirtying exactly what it just delivered.
    assert push_pull_round() == 0, "pull/push never settled -- the echo is unbounded"
    assert push_pull_round() == 0


def test_push_timestamp_is_dirty_at_not_corrupt_updated_at():
    """0014 made the relay reject stale writes on source_updated_at_ms, and 40.7%
    of leads have updated_at older than their own created_at. Sending updated_at
    would get the write correctly rejected as stale."""
    conn = om.get_conn()
    lead_id, _ = _mk_lead_in_ws(conn)
    # Backdate updated_at the way the corrupt rows look.
    conn.execute(
        "UPDATE leads SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (lead_id,)
    )
    conn.commit()
    conn.close()

    cap = _Capture()
    with mock.patch.object(om, "_relay_push_batches", side_effect=cap):
        om._push_pending_lead_snapshots("om_agent_test")

    core = [e for e in cap.entries if e["action"] == "lead_core_update"]
    assert core, "expected a core push"
    assert not core[0]["timestamp"].startswith("2020"), (
        f"pushed the corrupt updated_at: {core[0]['timestamp']}"
    )
