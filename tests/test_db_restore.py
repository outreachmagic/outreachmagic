"""Bug 11: database restore and atomic refresh."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import pipeline as om
from db_conn import database_has_schema, format_database_recovery_message
from om_paths import set_db_path_override


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACHMAGIC_DATA_ROOT", str(tmp_path))
    set_db_path_override(None)
    yield
    set_db_path_override(None)


def _seed_live_db() -> Path:
    om.init_db()
    om.create_workspace("PopCam", "popcam", sync=False)
    db_path = om.get_db_path()
    conn = om.get_conn()
    conn.execute(
        "INSERT INTO leads (name, email, channel, stage) VALUES (?, ?, 'email', 'prospecting')",
        ("Keep Me", "keep@example.com"),
    )
    conn.commit()
    conn.close()
    return db_path


def _mock_refresh_prereqs(monkeypatch):
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **k: True)
    monkeypatch.setattr(om, "maybe_sync_agent_secrets_from_cloud", lambda **k: None)
    monkeypatch.setattr(om, "sync_all", lambda **k: {"status": "ok"})
    monkeypatch.setattr(om, "get_sync_status", lambda org_id: {"pending_total": 0})
    monkeypatch.setattr(om, "get_agent_key", lambda: "om_agent_test")
    monkeypatch.setattr(
        om.routing_cloud,
        "cloud_routing_enabled",
        lambda cfg, tok: True,
    )


def test_database_has_schema_false_for_missing_file():
    path = Path("/tmp/does-not-exist-outreachmagic-test.db")
    assert database_has_schema(path) is False


def test_format_database_recovery_message_mentions_restore():
    msg = format_database_recovery_message()
    assert "restore --latest" in msg


def test_list_database_backups_newest_first(tmp_path):
    db_dir = tmp_path / "databases"
    db_dir.mkdir()
    older = db_dir / "outreachmagic.backup-20260101T120000Z.db"
    newer = db_dir / "outreachmagic.backup-20260201T120000Z.db"
    older.write_bytes(b"sqlite")
    newer.write_bytes(b"sqlite")
    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    backups = om.list_database_backups(db_dir)
    assert backups[0] == newer


def test_restore_latest_replaces_broken_db(monkeypatch):
    live = _seed_live_db()
    backup_path = live.with_suffix(".backup-test.db")
    import shutil

    shutil.copy2(live, backup_path)

    live.unlink()
    live.write_bytes(b"")

    result = om.restore_local_database(source=str(backup_path), yes=True)
    assert result["status"] == "ok"
    assert database_has_schema()
    conn = om.get_conn()
    row = conn.execute("SELECT email FROM leads WHERE email = ?", ("keep@example.com",)).fetchone()
    conn.close()
    assert row is not None


def test_refresh_keeps_live_db_when_pull_fails(monkeypatch):
    live = _seed_live_db()
    _mock_refresh_prereqs(monkeypatch)

    def _fail_pull(*args, **kwargs):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(om, "sync_from_relay_org", _fail_pull)

    result = om.refresh_local_database(yes=True, quiet=True)
    assert result["status"] == "error"
    assert result["error"] == "pull_failed"
    assert database_has_schema(live)
    conn = om.get_conn()
    row = conn.execute("SELECT email FROM leads WHERE email = ?", ("keep@example.com",)).fetchone()
    conn.close()
    assert row is not None
    staging = om._refresh_staging_path(live)
    assert not staging.exists()


def test_refresh_atomic_swap_on_success(monkeypatch):
    live = _seed_live_db()
    _mock_refresh_prereqs(monkeypatch)
    monkeypatch.setattr(om, "sync_from_relay_org", lambda *a, **k: (5, 0))

    result = om.refresh_local_database(yes=True, quiet=True)
    assert result["status"] == "ok"
    assert "staging_swap" in result.get("steps", [])
    assert database_has_schema(live)


def test_progress_eta_seconds():
    eta = om._progress_eta_seconds(1000, 10000, 10.0)
    assert eta is not None
    assert 80 <= eta <= 120
