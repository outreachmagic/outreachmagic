"""Regression test: merge_leads() must leave the survivor pending re-sync.

Bug: relay_bump_explained_clause treats a lead's updated_at bump as relay data
being echoed back (not a genuine local change) whenever relay_ingested already
has an entry at or after that updated_at. A full pull followed immediately by
a dedup merge can land relay_ingested.ingested_at and the merge's updated_at
bump in the same second (or, as reproduced here, with ingested_at explicitly
ahead) -- silently suppressing the survivor's pending re-sync and losing the
merged-in secondary email if the local DB is ever rebuilt from a fresh pull.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_update import set_last_sync, get_last_sync  # noqa: E402
from relay_ingest import relay_bump_explained_clause, unsynced_lead_clause  # noqa: E402


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_merge_survivor_pending_resync_despite_stale_relay_ingested_entry():
    _reset_db()
    keep = om.resolve_lead(name="Keep Lead", email="keep@example.com")
    other = om.resolve_lead(name="Merge Lead", email="merge@example.com")
    keep_id = int(keep["id"])
    merge_id = int(other["id"])

    conn = om.get_conn()
    # Simulate a full pull that ingested both leads with ingested_at *ahead* of
    # whatever the merge's updated_at bump will be -- the same shape of race
    # that happens in production when a pull and a dedup merge land in the
    # same wall-clock second.
    conn.execute(
        "INSERT INTO relay_ingested (dedupe_key, lead_id, ingested_at) VALUES (?, ?, ?)",
        ("relay:keep-test", keep_id, "2099-01-01 00:00:00"),
    )
    conn.execute(
        "INSERT INTO relay_ingested (dedupe_key, lead_id, ingested_at) VALUES (?, ?, ?)",
        ("relay:merge-test", merge_id, "2099-01-01 00:00:00"),
    )
    conn.commit()
    conn.close()

    set_last_sync("2000-01-01 00:00:00")

    conn = om.get_conn()
    om.merge_leads(keep_id, merge_id, reason="dedup", conn=conn)
    conn.commit()
    conn.close()

    conn = om.get_conn()
    secondary_emails = [
        row["email"]
        for row in conn.execute(
            "SELECT email FROM lead_emails WHERE lead_id = ? AND is_primary = 0", (keep_id,)
        ).fetchall()
    ]
    ingested_at, updated_at = conn.execute(
        """SELECT (SELECT ingested_at FROM relay_ingested WHERE lead_id = ?),
                  (SELECT updated_at FROM leads WHERE id = ?)""",
        (keep_id, keep_id),
    ).fetchone()
    conn.close()

    assert "merge@example.com" in secondary_emails

    # The merge must leave relay_ingested strictly behind the survivor's
    # updated_at, or relay_bump_explained_clause wrongly suppresses the pending
    # re-sync (this is the exact invariant the fix establishes).
    assert ingested_at < updated_at, (
        f"relay_ingested.ingested_at ({ingested_at}) must be before "
        f"leads.updated_at ({updated_at}) after a merge"
    )

    # Same predicate get_sync_status() uses for "leads_pending" -- exercised
    # directly here to avoid needing a live cloud token in tests.
    last_sync = get_last_sync()
    conn = om.get_conn()
    pending_lead_core_count = conn.execute(
        f"""SELECT COUNT(*) AS n FROM leads l
            WHERE (l.updated_at > ? AND NOT {relay_bump_explained_clause('l.id', 'l.updated_at')})
               OR {unsynced_lead_clause('l')}""",
        (last_sync,),
    ).fetchone()["n"]
    conn.close()
    assert pending_lead_core_count >= 1, (
        "survivor with a newly-merged secondary email must be pending re-sync"
    )


if __name__ == "__main__":
    test_merge_survivor_pending_resync_despite_stale_relay_ingested_entry()
    print("OK")
