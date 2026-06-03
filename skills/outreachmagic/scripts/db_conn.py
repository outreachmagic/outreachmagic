"""SQLite connection helper for outreachmagic local database."""

from __future__ import annotations

import sqlite3

from om_paths import get_db_path

# Saved when apply_bulk_pull_pragmas runs (restore on end_bulk_pull_session).
_BULK_PULL_PRAGMA_SAVES: dict[int, tuple] = {}


def get_conn() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_bulk_pull_pragmas(conn: sqlite3.Connection) -> None:
    """Tune SQLite for large batched pull pages (caller commits once per page)."""
    cid = id(conn)
    if cid in _BULK_PULL_PRAGMA_SAVES:
        return
    sync_row = conn.execute("PRAGMA synchronous").fetchone()
    cache_row = conn.execute("PRAGMA cache_size").fetchone()
    temp_row = conn.execute("PRAGMA temp_store").fetchone()
    _BULK_PULL_PRAGMA_SAVES[cid] = (
        sync_row[0] if sync_row else 2,
        cache_row[0] if cache_row else -2000,
        temp_row[0] if temp_row else 0,
    )
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")


def end_bulk_pull_session(conn: sqlite3.Connection) -> None:
    """Restore pragmas after a multi-page pull session."""
    saved = _BULK_PULL_PRAGMA_SAVES.pop(id(conn), None)
    if not saved:
        return
    try:
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    sync_val, cache_val, temp_val = saved
    conn.execute(f"PRAGMA synchronous={sync_val}")
    conn.execute(f"PRAGMA cache_size={cache_val}")
    conn.execute(f"PRAGMA temp_store={temp_val}")
