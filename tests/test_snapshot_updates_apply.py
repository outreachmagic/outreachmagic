"""An UPDATED snapshot from the relay must actually apply.

This is the inbound half of the round trip, and it was completely dead.

The dedupe key was client_id:entity_key:action:timestamp -- and for a snapshot the
timestamp is bound from the lead's created_at, which is constant across every version
of that entity. So the key never changed, and any genuinely-updated snapshot was
discarded on arrival as a "duplicate".

A live pull demonstrated it exactly: 87,073 workspace snapshots fetched, every page
reporting "+0 new, 1000 dupes", zero applied. The local mirror's tags and verification
had been frozen since the day it was seeded, because structurally nothing could ever
refresh them.

Snapshots now dedupe on content_hash, so unchanged content is still skipped (cheap)
while changed content always applies.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_sync import agent_entry_dedupe_key, ingest_agent_entry  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    om.resolve_workspace_identity(conn, "default")
    conn.commit()
    conn.close()


def _ws_snapshot(entity_key, tags, *, created_at="2026-01-01T00:00:00Z"):
    """A workspace snapshot as the relay delivers it. `timestamp` is the lead's
    created_at -- deliberately CONSTANT across versions, which is what broke dedupe."""
    return {
        "platform": "agent",
        "entity_key": entity_key,
        "payload": {
            "action": "lead_workspace_update",
            "client_id": "other-client",
            "workspace": "default",
            "timestamp": created_at,
            "data": {"tags": tags},
        },
    }


def _tags(lead_id):
    conn = om.get_conn()
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE lead_id = ?", (lead_id,)
            )
        }
    finally:
        conn.close()


def test_changed_snapshot_is_not_deduped_away():
    """The headline regression. Same entity, same timestamp, DIFFERENT tags."""
    lead_id = om.resolve_lead(name="Tagged", email="tagged@acme.com")["id"]
    conn = om.get_conn()
    key = om.lead_entity_key(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()

    first = _ws_snapshot(key, ["alpha"])
    second = _ws_snapshot(key, ["alpha", "beta"])  # relay-side change; timestamp UNCHANGED

    assert agent_entry_dedupe_key(first) != agent_entry_dedupe_key(second), (
        "a changed snapshot must not share a dedupe key with the old one -- keying on "
        "the timestamp made every version of an entity look identical"
    )

    ingest_agent_entry(first, quiet=True)
    assert _tags(lead_id) == {"alpha"}

    ingest_agent_entry(second, quiet=True)
    assert _tags(lead_id) == {"alpha", "beta"}, (
        "the updated snapshot was discarded as a duplicate -- this is the bug that "
        "made 87,073 pulled snapshots apply zero changes"
    )


def test_unchanged_snapshot_is_still_deduped():
    """Re-delivering identical content must still be cheap."""
    lead_id = om.resolve_lead(name="Same", email="same@acme.com")["id"]
    conn = om.get_conn()
    key = om.lead_entity_key(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()

    snap = _ws_snapshot(key, ["alpha"])
    assert agent_entry_dedupe_key(snap) == agent_entry_dedupe_key(_ws_snapshot(key, ["alpha"]))
    ingest_agent_entry(snap, quiet=True)
    assert ingest_agent_entry(_ws_snapshot(key, ["alpha"]), quiet=True) is None


def test_relay_supplied_content_hash_is_used():
    """The relay projects content_hash on each row; prefer it over rehashing."""
    snap = _ws_snapshot("uid:abc", ["x"])
    snap["content_hash"] = "deadbeef"
    assert "deadbeef" in agent_entry_dedupe_key(snap)


def test_flat_envelope_shape_is_understood():
    """Flat is the MAJORITY shape in production -- 141,100 of 166,854 core snapshots."""
    flat = {
        "platform": "agent",
        "entity_key": "uid:abc",
        "action": "lead_workspace_update",
        "client_id": "other-client",
        "workspace": "default",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {"tags": ["alpha"]},
    }
    key = agent_entry_dedupe_key(flat)
    assert key is not None, "a reader blind to the flat shape drops 85% of snapshots"
    assert "lead_workspace_update" in key
