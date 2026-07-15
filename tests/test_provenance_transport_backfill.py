"""Stage 8: legacy "agent_sync"/"relay_sync"/"relay" values in provenance
columns are NULL-ed on migrate, and the abort trigger keeps them out."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_migration import (  # noqa: E402
    _install_provenance_transport_guard,
    _repair_provenance_transport_strings,
)


def _fresh_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_backfill_nulls_legacy_transport_values():
    _fresh_db()
    conn = om.get_conn()

    # Drop the guard so we can plant the exact pre-migration mess this backfill
    # exists to clean up. Post-migration the guard would abort the setup.
    for col in ("original_source", "latest_source", "original_source_platform", "latest_source_platform"):
        for op in ("insert", "update"):
            conn.execute(f"DROP TRIGGER IF EXISTS trg_leads_{col}_transport_guard_{op}")
    conn.execute(
        "DELETE FROM migration_flags WHERE name = 'provenance_transport_backfill'"
    )
    conn.execute(
        """INSERT INTO leads (name, email, original_source, latest_source,
                              original_source_platform, latest_source_platform)
           VALUES ('T', 't@e.com', 'agent_sync', 'relay_sync', 'relay', 'relay')"""
    )
    lead_id = int(conn.execute("SELECT id FROM leads WHERE email = 't@e.com'").fetchone()["id"])

    _repair_provenance_transport_strings(conn)
    conn.commit()

    row = conn.execute(
        """SELECT original_source, latest_source,
                  original_source_platform, latest_source_platform
             FROM leads WHERE id = ?""",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["original_source"] is None
    assert row["latest_source"] is None
    assert row["original_source_platform"] is None
    assert row["latest_source_platform"] is None


def test_guard_aborts_transport_string_writes():
    _fresh_db()
    conn = om.get_conn()
    for col in ("original_source", "latest_source", "original_source_platform", "latest_source_platform"):
        try:
            conn.execute(
                f"INSERT INTO leads (name, email, {col}) VALUES ('T', 'x@e.com', 'agent_sync')"
            )
        except sqlite3.IntegrityError as exc:
            assert "transport string" in str(exc)
            continue
        raise AssertionError(f"leads.{col} = 'agent_sync' should have been rejected")
    conn.close()


def test_guard_allows_real_provenance_values():
    _fresh_db()
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO leads (name, email, original_source, latest_source,
                              original_source_platform, latest_source_platform)
           VALUES ('T', 't@e.com', 'plusvibe', 'plusvibe', 'plusvibe', 'plusvibe')"""
    )
    conn.commit()
    row = conn.execute("SELECT original_source FROM leads WHERE email = 't@e.com'").fetchone()
    conn.close()
    assert row["original_source"] == "plusvibe"


def test_resolve_lead_no_longer_scrubs_transport_source():
    """Stage 10d: resolve_lead's own scrub_provenance_transport() call was
    redundant with the DB-level abort trigger (every real writer was already
    fixed to pass the actual inbound platform, not a transport string) and has
    been removed. The trigger is now the sole enforcement point -- a caller
    that still hands us a transport string gets a loud abort, not a silent
    NULL that could paper over a real bug in a new caller."""
    _fresh_db()
    try:
        om.resolve_lead(email="a@example.com", name="A", source="relay_sync", source_platform="relay")
    except sqlite3.IntegrityError as exc:
        assert "transport string" in str(exc)
    else:
        raise AssertionError("a transport-string source should have been rejected by the abort trigger")


def test_guard_is_idempotent_across_reinstalls():
    _fresh_db()
    conn = om.get_conn()
    _install_provenance_transport_guard(conn)
    _install_provenance_transport_guard(conn)
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO leads (name, email, original_source) VALUES ('T', 'z@e.com', 'relay')"
        )
    except sqlite3.IntegrityError:
        conn.close()
        return
    conn.close()
    raise AssertionError("guard should still abort after reinstall")


if __name__ == "__main__":
    test_scrub_transport_returns_none_for_transport_strings()
    test_backfill_nulls_legacy_transport_values()
    test_guard_aborts_transport_string_writes()
    test_guard_allows_real_provenance_values()
    test_resolve_lead_scrubs_caller_supplied_transport_source()
    test_guard_is_idempotent_across_reinstalls()
    print("OK")
