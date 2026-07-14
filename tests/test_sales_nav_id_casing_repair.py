"""Sales-nav ID casing: the migration merges case-split duplicates, and new
writes get lowercased at the door so the split cannot re-open."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import workspace_routing as wr  # noqa: E402
from pipeline_migration import _repair_sales_nav_id_casing  # noqa: E402


MIXED = "ACwAAAdePicBNJam85-7o1AHdAggDPtCigKhWTs"
LOWER = MIXED.lower()


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _seed_case_split_pair(conn) -> tuple[int, int]:
    """Create two leads for the same sales-nav id, one mixed-case, one lower.
    Writes lead_identities.identity_value_normalized directly to bypass the new
    case-fold and reproduce the pre-migration on-disk state."""
    cur_a = conn.execute(
        """INSERT INTO leads (name, email, stage, linkedin_sales_nav_id)
           VALUES ('Alpha One', 'alpha@example.com', 'prospecting', ?)""",
        (MIXED,),
    )
    a_id = int(cur_a.lastrowid)
    cur_b = conn.execute(
        """INSERT INTO leads (name, email, stage, linkedin_sales_nav_id)
           VALUES ('Beta One', 'beta@example.com', 'prospecting', ?)""",
        (LOWER,),
    )
    b_id = int(cur_b.lastrowid)

    conn.execute(
        """INSERT INTO lead_identities
              (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?)""",
        (wr.DEFAULT_ORG_ID, a_id, MIXED),
    )
    conn.execute(
        """INSERT INTO lead_identities
              (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?)""",
        (wr.DEFAULT_ORG_ID, b_id, LOWER),
    )
    conn.commit()
    return a_id, b_id


def test_new_writes_case_fold_at_the_door():
    _reset_db()
    assert wr.normalize_identity_value("linkedin_sales_nav_id", MIXED) == LOWER


def test_repair_merges_case_split_duplicates_and_lowercases():
    _reset_db()
    conn = om.get_conn()
    a_id, b_id = _seed_case_split_pair(conn)

    # Clear the migration flag so the repair runs -- init_db just executed it on
    # an empty database, so it's already marked done.
    conn.execute(
        "DELETE FROM migration_flags WHERE name = 'sales_nav_id_casing_repair'"
    )
    conn.commit()

    _repair_sales_nav_id_casing(conn)
    conn.commit()

    remaining = conn.execute(
        "SELECT id FROM leads WHERE id IN (?, ?)", (a_id, b_id),
    ).fetchall()
    assert len(remaining) == 1, "case-split leads should have collapsed to one"
    survivor_id = int(remaining[0]["id"])

    survivor_row = conn.execute(
        "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (survivor_id,),
    ).fetchone()
    assert survivor_row["linkedin_sales_nav_id"] == LOWER

    identity_rows = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
            WHERE identity_type = 'linkedin_sales_nav_id' AND lead_id = ?""",
        (survivor_id,),
    ).fetchall()
    assert len(identity_rows) == 1
    assert identity_rows[0]["identity_value_normalized"] == LOWER
    conn.close()


def test_repair_is_idempotent():
    _reset_db()
    conn = om.get_conn()
    _seed_case_split_pair(conn)
    conn.execute(
        "DELETE FROM migration_flags WHERE name = 'sales_nav_id_casing_repair'"
    )
    conn.commit()
    _repair_sales_nav_id_casing(conn)
    conn.commit()
    _repair_sales_nav_id_casing(conn)  # second call short-circuits on the flag
    conn.close()


if __name__ == "__main__":
    test_new_writes_case_fold_at_the_door()
    test_repair_merges_case_split_duplicates_and_lowercases()
    test_repair_is_idempotent()
    print("OK")
