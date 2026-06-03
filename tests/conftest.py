"""Pytest hooks: isolated SQLite data root per test (avoids database is locked)."""

from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _unlink_sqlite_files(db_path: Path) -> None:
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()


@pytest.fixture(autouse=True)
def isolated_outreachmagic_data_root(tmp_path):
    """Each test gets its own data root; teardown removes DB + WAL sidecars."""
    from om_paths import set_data_root_override

    set_data_root_override(tmp_path)
    yield
    set_data_root_override(None)
    gc.collect()
    db_path = tmp_path / "skills" / "outreachmagic" / "databases" / "outreachmagic.db"
    _unlink_sqlite_files(db_path)
