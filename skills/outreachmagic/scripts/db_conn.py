"""SQLite connection helper for outreachmagic local database."""

from __future__ import annotations

import sqlite3
import sys

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


def database_has_schema(path: sqlite3.Connection | str | None = None) -> bool:
    """True when the database file exists and has core outreachmagic tables."""
    if path is None:
        db_path = get_db_path()
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return False
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
        except sqlite3.Error:
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads' LIMIT 1"
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()
    conn = path if isinstance(path, sqlite3.Connection) else sqlite3.connect(str(path), timeout=5.0)
    own = not isinstance(path, sqlite3.Connection)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads' LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        if own:
            conn.close()


def format_database_recovery_message(db_path=None) -> str:
    p = db_path or get_db_path()
    return (
        f"Database at {p} is missing or has no schema (no leads table).\n"
        "To restore from your most recent automatic backup:\n"
        "  pipeline.py restore --latest\n"
        "To list backups:\n"
        "  pipeline.py restore --list\n"
        "If backups are unavailable, run login then pull --full (or refresh --yes)."
    )


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
    try:
        conn.execute(f"PRAGMA synchronous={sync_val}")
        conn.execute(f"PRAGMA cache_size={cache_val}")
        conn.execute(f"PRAGMA temp_store={temp_val}")
    except sqlite3.OperationalError as exc:
        if "disk I/O error" in str(exc).lower() or "i/o error" in str(exc).lower():
            print(
                "[outreachmagic] Warning: disk I/O error while finalizing pull session. "
                "Your data may be intact — run: pipeline.py db-health",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError(
                "Disk I/O error while finalizing pull. "
                "Run: pipeline.py restore --latest  (or pipeline.py db-health)"
            ) from exc
        raise
