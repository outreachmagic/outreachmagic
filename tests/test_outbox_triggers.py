"""Stage 5: dirtiness is recorded at write time, in the database.

The bug these triggers kill: dirtiness used to be *derived* at push time from
`leads.updated_at`. That failed three separate ways --

  * record_provider_attempt() bumps no parent timestamp, so a provider attempt
    never marked its lead dirty at all;
  * lead_email_verification was likewise never reflected in the parent;
  * relay_bump_explained_clause suppressed the push for any lead that received a
    webhook after the local write, which is what ate tags;

and underneath all three, the cursor they depend on is corrupt (40.7% of leads
have updated_at older than created_at).

The point of the trigger approach is that the *writers do not change*. Each test
below calls the real writer and asserts an outbox row appears anyway.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _outbox(conn, entity_type=None):
    sql = "SELECT entity_type, entity_id, op, entity_key FROM outbox"
    params = ()
    if entity_type:
        sql += " WHERE entity_type = ?"
        params = (entity_type,)
    return conn.execute(sql, params).fetchall()


def _clear_outbox(conn):
    conn.execute("DELETE FROM outbox")
    conn.commit()


def _mk_lead(conn, email="a@example.com"):
    cur = conn.execute(
        "INSERT INTO leads (name, email, company) VALUES ('A', ?, 'Acme')", (email,)
    )
    conn.commit()
    return cur.lastrowid


def _mk_membership(conn, lead_id):
    """workspace_leads has a TEXT pk and a NOT NULL org_id."""
    ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()["id"]
    org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id) VALUES (?, ?, ?, ?)",
        (f"{ws}:{lead_id}", org, ws, lead_id),
    )
    conn.commit()
    return ws


def test_insert_lead_marks_lead_core_dirty():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    rows = _outbox(conn, "lead_core")
    assert (("lead_core", str(lead_id), "upsert")) in [
        (r["entity_type"], str(r["entity_id"]), r["op"]) for r in rows
    ]
    conn.close()


def test_update_lead_marks_dirty_after_clear():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _clear_outbox(conn)
    conn.execute("UPDATE leads SET company = 'Beta' WHERE id = ?", (lead_id,))
    conn.commit()
    assert len(_outbox(conn, "lead_core")) == 1
    conn.close()


def test_provider_attempt_marks_lead_dirty_without_touching_the_writer():
    """The headline bug: record_provider_attempt() bumps no parent timestamp."""
    from pipeline_provider_attempts import record_provider_attempt

    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _clear_outbox(conn)

    record_provider_attempt(conn, lead_id, "trykitt", status="pending")
    conn.commit()

    rows = _outbox(conn, "lead_core")
    assert len(rows) == 1, f"provider attempt did not mark lead dirty: {rows}"
    assert rows[0]["op"] == "upsert"
    conn.close()


def test_email_verification_marks_lead_dirty():
    """lead_email_verification had no wire contract and no parent bump at all."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _clear_outbox(conn)
    conn.execute(
        "INSERT INTO lead_email_verification "
        "(org_id, lead_id, email, status, source, verified_at) "
        "VALUES ('default', ?, 'a@example.com', 'valid', 'millionverifier', datetime('now'))",
        (lead_id,),
    )
    conn.commit()
    rows = _outbox(conn, "lead_core")
    assert len(rows) == 1, f"verification did not mark lead dirty: {rows}"
    conn.close()


def test_tag_write_marks_lead_workspace_dirty():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    ws = _mk_membership(conn, lead_id)
    _clear_outbox(conn)

    conn.execute(
        "INSERT INTO workspace_lead_tags (workspace_id, lead_id, tag) VALUES (?, ?, 'x')",
        (ws, lead_id),
    )
    conn.commit()
    rows = _outbox(conn, "lead_workspace")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == f"{lead_id}:{ws}"
    conn.close()


def test_tag_removal_is_an_upsert_not_a_tombstone():
    """Removing a tag is a content change to a living parent -- the old code's
    `if added:` guard meant tag_set([]) pushed nothing at all."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    ws = _mk_membership(conn, lead_id)
    conn.execute(
        "INSERT INTO workspace_lead_tags (workspace_id, lead_id, tag) VALUES (?, ?, 'x')",
        (ws, lead_id),
    )
    conn.commit()
    _clear_outbox(conn)

    conn.execute(
        "DELETE FROM workspace_lead_tags WHERE lead_id = ? AND tag = 'x'", (lead_id,)
    )
    conn.commit()
    rows = _outbox(conn, "lead_workspace")
    assert len(rows) == 1
    assert rows[0]["op"] == "upsert", "clearing a tag must still push the parent"
    conn.close()


def test_deleting_a_lead_tombstones_and_leaves_no_stale_upsert():
    """The cascade trap: deleting a lead cascades to its children, whose DELETE
    triggers would re-queue an 'upsert' for the entity we just tombstoned --
    and pushing a snapshot for a deleted row is a guaranteed relay error."""
    from pipeline_provider_attempts import record_provider_attempt

    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    ws = _mk_membership(conn, lead_id)
    conn.execute(
        "INSERT INTO workspace_lead_tags (workspace_id, lead_id, tag) VALUES (?, ?, 'x')",
        (ws, lead_id),
    )
    conn.commit()
    uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]

    record_provider_attempt(conn, lead_id, "trykitt", status="pending")
    conn.commit()
    _clear_outbox(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()

    core = _outbox(conn, "lead_core")
    assert [r["op"] for r in core] == ["delete"], f"expected one tombstone, got {core}"
    assert core[0]["entity_key"] == uid, "tombstone must carry the immutable uid"
    conn.close()


def test_backfill_queues_every_entity():
    """The cutover seed. It only ever runs once, on a live DB, so a syntax error
    here is found in production or not at all."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _mk_membership(conn, lead_id)
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Acme', 'acme.com')")
    conn.commit()
    _clear_outbox(conn)
    conn.close()

    dry = om.backfill_outbox(dry_run=True)
    assert dry["queued"]["lead_core"] == 1
    assert dry["queued"]["lead_workspace"] == 1
    assert dry["queued"]["company"] == 1
    assert dry["total"] >= 3

    conn = om.get_conn()
    assert _outbox(conn) == [], "dry run must write nothing"
    conn.close()

    real = om.backfill_outbox()
    assert real["total"] == dry["total"]

    conn = om.get_conn()
    kinds = {r["entity_type"] for r in _outbox(conn)}
    assert {"lead_core", "lead_workspace", "company"} <= kinds
    conn.close()

    # Idempotent: running it twice must not duplicate or reset anything.
    om.backfill_outbox()
    conn = om.get_conn()
    assert len(_outbox(conn, "lead_core")) == 1
    conn.close()


def test_uid_captured_before_row_disappears():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
    assert uid
    _clear_outbox(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    row = conn.execute(
        "SELECT entity_key FROM outbox WHERE op = 'delete' AND entity_type = 'lead_core'"
    ).fetchone()
    assert row["entity_key"] == uid
    conn.close()
