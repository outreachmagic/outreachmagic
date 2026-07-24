"""P3-2: sync_shadow legacy-key hygiene.

D1 is uid-only for lead_core/lead_workspace/company; legacy natural-key shadow
rows for those types are orphaned local metadata that inflate sync_shadow
totals without ever reflecting what the relay actually holds. sender_account/
sender_domain are legacy-keys-only by design (no uid migration on shadow yet)
and must never be swept up by the prune.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import outbox  # noqa: E402
import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed_shadow(conn, entity_type, entity_key, workspace_slug=""):
    conn.execute(
        "INSERT INTO sync_shadow (entity_type, entity_key, workspace_slug, content_hash, synced_at) "
        "VALUES (?, ?, ?, 'h', datetime('now'))",
        (entity_type, entity_key, workspace_slug),
    )


def test_legacy_shadow_counts_only_counts_governed_types_without_uid_prefix():
    conn = om.get_conn()
    _seed_shadow(conn, "lead_core", "a@example.com")
    _seed_shadow(conn, "lead_core", "uid:abc123")
    _seed_shadow(conn, "lead_workspace", "a@example.com", "default")
    _seed_shadow(conn, "company", "example.com")
    _seed_shadow(conn, "company", "uid:def456")
    _seed_shadow(conn, "sender_account", "42")
    conn.commit()

    counts = outbox.legacy_shadow_counts(conn)
    conn.close()

    assert counts == {"lead_core": 1, "lead_workspace": 1, "company": 1}


def test_prune_legacy_dry_run_deletes_nothing():
    conn = om.get_conn()
    _seed_shadow(conn, "lead_core", "a@example.com")
    _seed_shadow(conn, "lead_core", "uid:abc123")
    conn.commit()

    result = outbox.prune_legacy_shadow(conn, dry_run=True)
    total = conn.execute("SELECT COUNT(*) AS n FROM sync_shadow").fetchone()["n"]
    conn.close()

    assert result == {"dry_run": True, "by_type": {"lead_core": 1}, "total": 1}
    assert total == 2, "dry-run must not delete anything"


def test_prune_legacy_execute_deletes_only_legacy_governed_rows():
    conn = om.get_conn()
    _seed_shadow(conn, "lead_core", "a@example.com")
    _seed_shadow(conn, "lead_core", "uid:abc123")
    _seed_shadow(conn, "lead_workspace", "a@example.com", "default")
    _seed_shadow(conn, "lead_workspace", "uid:abc123", "default")
    _seed_shadow(conn, "company", "example.com")
    _seed_shadow(conn, "company", "uid:def456")
    _seed_shadow(conn, "sender_account", "42")
    conn.commit()

    result = outbox.prune_legacy_shadow(conn, dry_run=False)
    remaining = {
        r["entity_type"]: r["entity_key"]
        for r in conn.execute("SELECT entity_type, entity_key FROM sync_shadow").fetchall()
    }
    conn.close()

    assert result["dry_run"] is False
    assert result["total"] == 3
    assert result["by_type"] == {"lead_core": 1, "lead_workspace": 1, "company": 1}
    assert remaining == {
        "lead_core": "uid:abc123",
        "lead_workspace": "uid:abc123",
        "company": "uid:def456",
        "sender_account": "42",
    }


def test_prune_legacy_noop_when_nothing_to_prune():
    conn = om.get_conn()
    _seed_shadow(conn, "lead_core", "uid:abc123")
    conn.commit()

    result = outbox.prune_legacy_shadow(conn, dry_run=False)
    total = conn.execute("SELECT COUNT(*) AS n FROM sync_shadow").fetchone()["n"]
    conn.close()

    assert result["total"] == 0
    assert total == 1
