"""Stage 9: junk-lead cleanup.

The predicate must select exactly the pre-Stage-1 weak-identity rows and
nothing else. These tests seed one row of each contamination class next to a
canonical junk row and assert the contamination stays and the junk goes.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import junk_cleanup  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_junk(conn, *, name="Unknown", source_detail="csv:weak-import"):
    """Canonical junk lead: no email, no linkedin, no sales-nav, name Unknown."""
    cur = conn.execute(
        "INSERT INTO leads (name, email, linkedin_url, linkedin_sales_nav_id, "
        "original_source_detail) VALUES (?, NULL, NULL, NULL, ?)",
        (name, source_detail),
    )
    conn.commit()
    return cur.lastrowid


def _mk_real_lead(conn, email="real@example.com"):
    """A lead with recoverable information -- must never be selected."""
    cur = conn.execute(
        "INSERT INTO leads (name, email) VALUES ('Real Person', ?)", (email,)
    )
    conn.commit()
    return cur.lastrowid


def _outbox_deletes_for(conn, lead_ids):
    q = ",".join("?" for _ in lead_ids)
    return conn.execute(
        f"SELECT entity_id FROM outbox WHERE entity_type = 'lead_core' "
        f"AND op = 'delete' AND entity_id IN ({q})",
        [str(x) for x in lead_ids],
    ).fetchall()


def test_predicate_selects_pure_junk():
    conn = om.get_conn()
    junk_id = _mk_junk(conn)
    real_id = _mk_real_lead(conn)
    conn.close()

    result = junk_cleanup.cleanup_junk_leads(dry_run=True)
    assert result["selected"] == 1

    conn = om.get_conn()
    ids = [r["id"] for r in conn.execute(junk_cleanup._junk_ids_sql()).fetchall()]
    conn.close()
    assert ids == [junk_id]
    assert real_id not in ids


@pytest.mark.parametrize(
    "child_setup",
    [
        # (table, INSERT sql, params factory taking lead_id and returning params)
        pytest.param(
            (
                "lead_identities",
                "INSERT INTO lead_identities (org_id, lead_id, identity_type, "
                "identity_value_normalized) VALUES ('default', ?, 'email', 'x@y.com')",
                lambda lid: (lid,),
            ),
            id="lead_identities",
        ),
        pytest.param(
            (
                "events",
                "INSERT INTO events (lead_id, event_type) VALUES (?, 'email_sent')",
                lambda lid: (lid,),
            ),
            id="events",
        ),
        pytest.param(
            (
                "workspace_lead_events",
                "INSERT INTO workspace_lead_events "
                "(org_id, workspace_id, lead_id, event_type, event_at, idempotency_key) "
                "VALUES ('default', 'ws', ?, 'email_sent', datetime('now'), ?)",
                lambda lid: (lid, f"idem-{lid}"),
            ),
            id="workspace_lead_events",
        ),
        pytest.param(
            (
                "lead_personalization",
                "INSERT INTO lead_personalization (lead_id, field_name, field_value) "
                "VALUES (?, 'hook', 'x')",
                lambda lid: (lid,),
            ),
            id="lead_personalization",
        ),
        pytest.param(
            (
                "bounce_events",
                "INSERT INTO bounce_events (id, org_id, lead_id, platform, "
                "sender_email, lead_email, first_seen_at, last_seen_at) "
                "VALUES (?, 'default', ?, 'smtp', 's@x.com', 'l@x.com', "
                "datetime('now'), datetime('now'))",
                lambda lid: (f"bev-{lid}", lid),
            ),
            id="bounce_events",
        ),
        pytest.param(
            (
                "crm_entity_map",
                "INSERT INTO crm_entity_map (workspace_id, lead_id, platform) "
                "VALUES ((SELECT id FROM workspaces LIMIT 1), ?, 'hubspot')",
                lambda lid: (lid,),
            ),
            id="crm_entity_map",
        ),
    ],
)
def test_predicate_excludes_junk_looking_lead_with_any_child_row(child_setup):
    table, insert_sql, params_fn = child_setup
    conn = om.get_conn()
    contaminated = _mk_junk(conn, source_detail=f"contam:{table}")
    conn.execute(insert_sql, params_fn(contaminated))
    conn.commit()
    # A pure junk lead alongside so the query returns something to filter against.
    pure = _mk_junk(conn)
    conn.close()

    result = junk_cleanup.cleanup_junk_leads(dry_run=True)
    assert result["selected"] == 1, (
        f"expected only the pure junk row; the {table} contamination leaked"
    )
    conn = om.get_conn()
    ids = [r["id"] for r in conn.execute(junk_cleanup._junk_ids_sql()).fetchall()]
    conn.close()
    assert contaminated not in ids
    assert pure in ids


def test_dry_run_writes_nothing():
    conn = om.get_conn()
    _mk_junk(conn)
    _mk_junk(conn, source_detail="csv:other-import")
    lead_count_before = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    quar_count_before = conn.execute(
        "SELECT COUNT(*) AS n FROM leads_junk_quarantine"
    ).fetchone()["n"]
    outbox_before = conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
    conn.close()

    result = junk_cleanup.cleanup_junk_leads(dry_run=True)
    assert result["selected"] == 2
    assert result["quarantined"] == 0
    assert result["deleted"] == 0

    conn = om.get_conn()
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
        == lead_count_before
    )
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM leads_junk_quarantine").fetchone()["n"]
        == quar_count_before
    )
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
        == outbox_before
    )
    conn.close()


def test_yes_required_for_destructive_run():
    conn = om.get_conn()
    _mk_junk(conn)
    conn.close()

    with pytest.raises(RuntimeError, match="confirm=True"):
        junk_cleanup.cleanup_junk_leads(dry_run=False, confirm=False)


def test_confirmed_run_quarantines_and_deletes():
    conn = om.get_conn()
    junk_ids = [
        _mk_junk(conn, source_detail=f"csv:import-{i}") for i in range(3)
    ]
    real_id = _mk_real_lead(conn)
    conn.close()

    result = junk_cleanup.cleanup_junk_leads(dry_run=False, confirm=True)
    assert result["selected"] == 3
    assert result["quarantined"] == 3
    assert result["deleted"] == 3

    conn = om.get_conn()
    # Every quarantined lead survives with its original_source_detail preserved.
    quar = conn.execute(
        "SELECT lead_id, original_source_detail FROM leads_junk_quarantine "
        "ORDER BY lead_id"
    ).fetchall()
    assert [r["lead_id"] for r in quar] == junk_ids
    assert [r["original_source_detail"] for r in quar] == [
        f"csv:import-{i}" for i in range(3)
    ]
    # The leads themselves are gone.
    remaining = conn.execute(
        "SELECT id FROM leads ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in remaining] == [real_id]
    conn.close()


def test_tombstones_dropped_for_deleted_leads():
    conn = om.get_conn()
    junk_ids = [_mk_junk(conn) for _ in range(2)]
    conn.close()

    junk_cleanup.cleanup_junk_leads(dry_run=False, confirm=True)

    conn = om.get_conn()
    tombstones = _outbox_deletes_for(conn, junk_ids)
    conn.close()
    assert tombstones == [], (
        "the Stage 5 delete trigger fires on the cascade; those rows were never "
        "pushed, so their tombstones must be dropped client-side"
    )


def test_real_lead_untouched_by_confirmed_run():
    conn = om.get_conn()
    real_id = _mk_real_lead(conn)
    _mk_junk(conn)
    conn.close()

    junk_cleanup.cleanup_junk_leads(dry_run=False, confirm=True)

    conn = om.get_conn()
    ids = [r["id"] for r in conn.execute("SELECT id FROM leads").fetchall()]
    conn.close()
    assert real_id in ids
