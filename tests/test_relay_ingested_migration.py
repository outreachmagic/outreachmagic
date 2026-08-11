#!/usr/bin/env python3
"""The pre-hash relay_ingested table must migrate, not crash the next pull.

relay_ingested stored its dedupe key as text until the hashed shape landed, and
no migration shipped with that change -- so any database created before it died
on `pull` with "no such column: dedupe_hash". These tests pin the rebuild.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

from pipeline_migration import _migrate_relay_ingested_to_hash  # noqa: E402
from relay_ingest import relay_dedupe_hash  # noqa: E402


def _old_shape_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE relay_ingested (
            dedupe_key      TEXT PRIMARY KEY,
            lead_id         INTEGER,
            ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return conn


def test_old_text_key_table_is_rebuilt_as_hashed():
    conn = _old_shape_db()
    conn.executemany(
        "INSERT INTO relay_ingested (dedupe_key, lead_id, ingested_at) VALUES (?, ?, ?)",
        [("agent:acme:lead-1:sent:1", 7, "2026-07-01T00:00:00Z"),
         ("agent:acme:lead-2:sent:2", None, "2026-07-02T00:00:00Z")],
    )

    result = _migrate_relay_ingested_to_hash(conn)

    assert result["status"] == "migrated"
    assert result["rows"] == 2
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(relay_ingested)").fetchall()}
    assert "dedupe_hash" in cols
    assert "dedupe_key" not in cols
    row = conn.execute(
        "SELECT lead_id FROM relay_ingested WHERE dedupe_hash = ?",
        (relay_dedupe_hash("agent:acme:lead-1:sent:1"),),
    ).fetchone()
    assert row["lead_id"] == 7


def test_migration_is_a_no_op_on_an_already_hashed_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE relay_ingested (
            dedupe_hash     BLOB PRIMARY KEY,
            lead_id         INTEGER,
            ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("INSERT INTO relay_ingested (dedupe_hash) VALUES (?)", (relay_dedupe_hash("k"),))

    assert _migrate_relay_ingested_to_hash(conn)["status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) c FROM relay_ingested").fetchone()["c"] == 1


def test_prefetch_works_after_migrating_an_old_database():
    """The exact crash from the bug report: prefetch on a pre-hash table."""
    from relay_ingest import prefetch_relay_ingested

    conn = _old_shape_db()
    conn.execute(
        "INSERT INTO relay_ingested (dedupe_key) VALUES (?)", ("agent:acme:lead-1:sent:1",))

    _migrate_relay_ingested_to_hash(conn)
    seen = prefetch_relay_ingested(
        ["agent:acme:lead-1:sent:1", "agent:acme:lead-9:sent:9"], conn)

    assert "agent:acme:lead-1:sent:1" in seen
    assert "agent:acme:lead-9:sent:9" not in seen
