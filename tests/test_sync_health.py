"""4.1: sync-health -- one local screen combining outbox backlog + shadow
legacy hygiene, replacing the raw sync_shadow total (which double-counts
legacy + uid keys for the same entity) as the thing an operator checks first.
Local-only: no relay/D1 call, so status only ever distinguishes BACKLOG /
SHADOW_STALE / IN_PARITY, never DRIFT.
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


def test_status_in_parity_when_clean():
    result = om.sync_health()
    assert result["status"] == "IN_PARITY"
    assert result["sync_shadow_total"] == 0
    assert result["legacy_shadow_total"] == 0


def test_status_backlog_when_outbox_has_upserts():
    conn = om.get_conn()
    _mk_lead_in_ws(conn)
    conn.close()

    result = om.sync_health()
    assert result["status"] == "BACKLOG"
    lead_core = next(e for e in result["entities"] if e["entity_type"] == "lead_core")
    assert lead_core["local"] == 1
    assert lead_core["outbox_upserts"] >= 1


def test_status_shadow_stale_when_only_legacy_shadow_rows_exist():
    conn = om.get_conn()
    _mk_lead_in_ws(conn)
    # Drain the outbox rows the insert queued, and seed a legacy shadow row --
    # simulates a fully-pushed lead whose pull-side shadow was seeded under a
    # pre-uid-migration natural key.
    conn.execute("DELETE FROM outbox WHERE entity_type IN ('lead_core', 'lead_workspace')")
    conn.execute(
        "INSERT INTO sync_shadow (entity_type, entity_key, workspace_slug, content_hash, synced_at) "
        "VALUES ('lead_core', 'a@example.com', '', 'h', datetime('now'))"
    )
    conn.commit()
    conn.close()

    result = om.sync_health()
    assert result["status"] == "SHADOW_STALE"
    lead_core = next(e for e in result["entities"] if e["entity_type"] == "lead_core")
    assert lead_core["outbox_upserts"] == 0
    assert lead_core["legacy_shadow"] == 1


def test_entity_local_counts_match_tables():
    conn = om.get_conn()
    for i in range(3):
        _mk_lead_in_ws(conn, email=f"e{i}@example.com")
    om.ensure_company(conn, name="Acme", domain="acme.example.com")
    conn.commit()
    conn.close()

    result = om.sync_health()
    by_type = {e["entity_type"]: e for e in result["entities"]}
    assert by_type["lead_core"]["local"] == 3
    assert by_type["lead_workspace"]["local"] == 3
    assert by_type["company"]["local"] == 1
