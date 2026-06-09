"""relay_ingested FK safety when leads merge during a pull page."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from relay_ingest import mark_relay_ingested_many  # noqa: E402


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_mark_relay_ingested_many_after_merge_in_transaction():
    _reset_db()
    r1 = om.resolve_lead(name="Keep Lead", email="keep@example.com")
    r2 = om.resolve_lead(name="Merge Lead", email="merge@example.com")
    keep_id = int(r1["id"])
    merge_id = int(r2["id"])

    conn = om.get_conn()
    om.apply_bulk_pull_pragmas(conn)
    try:
        om.merge_leads(keep_id, merge_id, reason="test", conn=conn)
        mark_relay_ingested_many(
            [("relay:merge-test", merge_id), ("relay:keep-test", keep_id)],
            conn=conn,
            commit=True,
        )
        rows = conn.execute(
            "SELECT dedupe_key, lead_id FROM relay_ingested ORDER BY dedupe_key",
        ).fetchall()
    finally:
        conn.close()

    by_key = {row["dedupe_key"]: row["lead_id"] for row in rows}
    assert by_key["relay:keep-test"] == keep_id
    assert by_key["relay:merge-test"] == keep_id


if __name__ == "__main__":
    test_mark_relay_ingested_many_after_merge_in_transaction()
    print("OK")
