"""import_profiles() must reuse one shared connection instead of one per row.

Regression coverage for the slow-import fix: a fresh (no lead_id) import used to
open+commit+close a brand-new sqlite3 connection for every row (and again per
personalized_* field), which is what made multi-thousand-row imports take over
an hour on the real production DB.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _rows(n: int, *, personalize: bool = False) -> list[dict]:
    out = []
    for i in range(n):
        row = {"email": f"user{i}@acme.com", "name": f"User {i}", "company": "Acme"}
        if personalize:
            row["personalized_first_name"] = f"User{i}"
        out.append(row)
    return out


def test_import_profiles_uses_single_shared_connection(monkeypatch):
    _reset_db()
    real_get_conn = om.get_conn
    opened: list[sqlite3.Connection] = []

    def counting_get_conn():
        conn = real_get_conn()
        opened.append(conn)
        return conn

    monkeypatch.setattr(om, "get_conn", counting_get_conn)
    summary = om.import_profiles(_rows(25))
    assert summary["processed"] == 25
    assert summary["created"] == 25
    assert len(opened) == 1


def test_import_profiles_personalization_reuses_shared_connection(monkeypatch):
    _reset_db()
    real_get_conn = om.get_conn
    opened: list[sqlite3.Connection] = []

    def counting_get_conn():
        conn = real_get_conn()
        opened.append(conn)
        return conn

    monkeypatch.setattr(om, "get_conn", counting_get_conn)
    summary = om.import_profiles(_rows(10, personalize=True))
    assert summary["processed"] == 10
    assert summary["personalized"] == 10
    assert len(opened) == 1

    conn = om.get_conn()
    row = conn.execute(
        "SELECT field_value FROM lead_personalization WHERE field_name = 'first_name' "
        "AND lead_id = (SELECT id FROM leads WHERE email = 'user0@acme.com')"
    ).fetchone()
    conn.close()
    assert row["field_value"] == "User0"


class _CommitCountingConn:
    """Transparent proxy that counts commit() calls on the wrapped connection."""

    def __init__(self, real: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "commit_calls", 0)

    def commit(self) -> None:
        object.__setattr__(self, "commit_calls", self.commit_calls + 1)
        self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_import_profiles_chunked_commit_and_progress(monkeypatch, capsys):
    _reset_db()
    monkeypatch.setattr(om, "IMPORT_CHUNK_SIZE", 5)
    real_get_conn = om.get_conn
    wrapped_conns: list[_CommitCountingConn] = []

    def wrapping_get_conn():
        proxy = _CommitCountingConn(real_get_conn())
        wrapped_conns.append(proxy)
        return proxy

    monkeypatch.setattr(om, "get_conn", wrapping_get_conn)
    summary = om.import_profiles(_rows(12))
    assert summary["processed"] == 12
    assert len(wrapped_conns) == 1
    # Chunk-boundary commits fire at i=5 and i=10, plus the final commit
    # from end_bulk_pull_session.
    assert wrapped_conns[0].commit_calls >= 3

    captured = capsys.readouterr()
    assert "import-profiles: 5/12" in captured.err
    assert "import-profiles: 10/12" in captured.err
