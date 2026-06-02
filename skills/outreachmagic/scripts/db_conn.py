"""SQLite connection helper for outreachmagic local database."""

from __future__ import annotations

import sqlite3

from om_paths import get_db_path


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn
