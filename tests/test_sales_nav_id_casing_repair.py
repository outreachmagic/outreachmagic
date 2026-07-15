"""Sales-nav ID casing: the split-pair merger + opportunistic case upgrade.

Model:
- `lead_identities.identity_value_normalized` = lowercase (match key; enforced
  at write in upsert_all_identities / upsert_identity_alias)
- `leads.linkedin_sales_nav_id` = canonical mixed case where we have it
- Aliases in the outbound payload use the leads column, so they carry the
  canonical case; the identities-loop skips sales-nav to avoid double-emit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import workspace_routing as wr  # noqa: E402
from lead_sync import build_lead_core_sync_payload  # noqa: E402
from pipeline_migration import _repair_sales_nav_id_casing  # noqa: E402


MIXED = "ACwAAAdePicBNJam85-7o1AHdAggDPtCigKhWTs"
LOWER = MIXED.lower()


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _seed_case_split_pair(conn, *, keep_case: str = "mixed") -> tuple[int, int]:
    """Two leads for the same sales-nav id, one mixed-case, one lower.
    `keep_case` controls which order they're inserted -- test both directions."""
    mixed_first = keep_case == "mixed"
    first_val, second_val = (MIXED, LOWER) if mixed_first else (LOWER, MIXED)
    cur_a = conn.execute(
        """INSERT INTO leads (name, email, stage, linkedin_sales_nav_id)
           VALUES ('Alpha One', 'alpha@example.com', 'prospecting', ?)""",
        (first_val,),
    )
    a_id = int(cur_a.lastrowid)
    cur_b = conn.execute(
        """INSERT INTO leads (name, email, stage, linkedin_sales_nav_id)
           VALUES ('Beta One', 'beta@example.com', 'prospecting', ?)""",
        (second_val,),
    )
    b_id = int(cur_b.lastrowid)
    conn.execute(
        """INSERT INTO lead_identities
              (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?)""",
        (wr.DEFAULT_ORG_ID, a_id, first_val),
    )
    conn.execute(
        """INSERT INTO lead_identities
              (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?)""",
        (wr.DEFAULT_ORG_ID, b_id, second_val),
    )
    conn.commit()
    return a_id, b_id


def _run_repair(conn) -> None:
    conn.execute(
        "DELETE FROM migration_flags WHERE name = 'sales_nav_id_casing_repair'"
    )
    conn.commit()
    _repair_sales_nav_id_casing(conn)
    conn.commit()


def test_new_writes_preserve_canonical_case_on_leads_column():
    _reset_db()
    om.resolve_lead(
        email="fresh@example.com",
        name="Fresh Lead",
        identities=[("linkedin_sales_nav_id", MIXED), ("email", "fresh@example.com")],
    )
    conn = om.get_conn()
    lead = conn.execute(
        "SELECT id, linkedin_sales_nav_id FROM leads WHERE email = 'fresh@example.com'"
    ).fetchone()
    identity = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
            WHERE lead_id = ? AND identity_type = 'linkedin_sales_nav_id'""",
        (lead["id"],),
    ).fetchone()
    conn.close()
    assert lead["linkedin_sales_nav_id"] == MIXED, "leads column keeps canonical case"
    assert identity["identity_value_normalized"] == LOWER, "identity is folded for match"


def test_resolve_lead_matches_legacy_lowercase_and_upgrades_case():
    """End-to-end: a lead left over from before 664d4f5 (both the identity row
    and the leads column stored lowercase) must still be found by a fresh
    inbound payload carrying the properly-cased id -- find_lead_by_identity's
    LOWER(...) = LOWER(?) comparison is what makes that match possible -- and
    the match must opportunistically upgrade the display column to the
    canonical case rather than leave it lowercase forever."""
    _reset_db()
    conn = om.get_conn()
    cur = conn.execute(
        """INSERT INTO leads (name, email, linkedin_sales_nav_id)
           VALUES ('Legacy Lead', 'legacy@example.com', ?)""",
        (LOWER,),
    )
    legacy_lead_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO lead_identities
               (org_id, lead_id, identity_type, identity_value_normalized, source, created_at)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?, 'sales_navigator', datetime('now'))""",
        (wr.DEFAULT_ORG_ID, legacy_lead_id, LOWER),
    )
    conn.commit()
    conn.close()

    result = om.resolve_lead(
        name="Legacy Lead",
        identities=[("linkedin_sales_nav_id", MIXED)],
    )

    conn = om.get_conn()
    lead = conn.execute(
        "SELECT id, linkedin_sales_nav_id FROM leads WHERE id = ?", (legacy_lead_id,),
    ).fetchone()
    conn.close()
    assert result["id"] == legacy_lead_id, "must match the existing lead, not create a duplicate"
    assert lead["linkedin_sales_nav_id"] == MIXED, "match must upgrade the lowercase column to canonical case"


def test_upsert_identity_alias_upgrades_lowercase_to_mixed():
    _reset_db()
    conn = om.get_conn()
    cur = conn.execute(
        """INSERT INTO leads (name, email, linkedin_sales_nav_id)
           VALUES ('T', 't@e.com', ?)""",
        (LOWER,),
    )
    lead_id = int(cur.lastrowid)
    conn.commit()
    wr.upsert_identity_alias(
        conn, wr.DEFAULT_ORG_ID, lead_id, "linkedin_sales_nav_id", MIXED,
    )
    conn.commit()
    val = conn.execute(
        "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["linkedin_sales_nav_id"]
    conn.close()
    assert val == MIXED, "lowercase-stored lead should have been upgraded"


def test_upsert_does_not_downgrade_mixed_to_lowercase():
    _reset_db()
    conn = om.get_conn()
    cur = conn.execute(
        """INSERT INTO leads (name, email, linkedin_sales_nav_id)
           VALUES ('T', 't@e.com', ?)""",
        (MIXED,),
    )
    lead_id = int(cur.lastrowid)
    conn.commit()
    wr.upsert_identity_alias(
        conn, wr.DEFAULT_ORG_ID, lead_id, "linkedin_sales_nav_id", LOWER,
    )
    conn.commit()
    val = conn.execute(
        "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["linkedin_sales_nav_id"]
    conn.close()
    assert val == MIXED, "a lowercase write must not overwrite the canonical case"


def test_repair_merges_and_prefers_mixed_case_survivor():
    for keep_order in ("mixed", "lower"):
        _reset_db()
        conn = om.get_conn()
        a_id, b_id = _seed_case_split_pair(conn, keep_case=keep_order)
        _run_repair(conn)

        remaining = conn.execute(
            "SELECT id FROM leads WHERE id IN (?, ?)", (a_id, b_id),
        ).fetchall()
        assert len(remaining) == 1, f"({keep_order}) case-split should collapse to one"
        survivor_id = int(remaining[0]["id"])
        row = conn.execute(
            "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (survivor_id,),
        ).fetchone()
        assert row["linkedin_sales_nav_id"] == MIXED, (
            f"({keep_order}) survivor should carry the canonical mixed case"
        )
        identity = conn.execute(
            """SELECT identity_value_normalized FROM lead_identities
                WHERE identity_type = 'linkedin_sales_nav_id' AND lead_id = ?""",
            (survivor_id,),
        ).fetchall()
        assert len(identity) == 1
        assert identity[0]["identity_value_normalized"] == LOWER, (
            "identity_value_normalized is the match key -- lowercase"
        )
        conn.close()


def test_repair_is_idempotent():
    _reset_db()
    conn = om.get_conn()
    _seed_case_split_pair(conn)
    _run_repair(conn)
    _repair_sales_nav_id_casing(conn)  # second call short-circuits on the flag
    conn.close()


def test_outbound_alias_uses_canonical_case_and_is_not_duplicated():
    _reset_db()
    conn = om.get_conn()
    cur = conn.execute(
        """INSERT INTO leads (name, email, linkedin_sales_nav_id)
           VALUES ('T', 't@e.com', ?)""",
        (MIXED,),
    )
    lead_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO lead_identities
              (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_sales_nav_id', ?)""",
        (wr.DEFAULT_ORG_ID, lead_id, LOWER),
    )
    conn.commit()
    payload = build_lead_core_sync_payload(conn, wr.DEFAULT_ORG_ID, lead_id)
    conn.close()
    sn_aliases = [a for a in (payload.get("aliases") or []) if "linkedin_sales_nav_id" in a]
    assert sn_aliases == [f"linkedin_sales_nav_id:{MIXED}"], (
        "one alias, in canonical case; identity-row lowercase must not double-ship"
    )


def test_field_conflict_matches_case_insensitively():
    _reset_db()
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO leads (name, email, linkedin_sales_nav_id)
           VALUES ('Owner', 'owner@e.com', ?)""",
        (MIXED,),
    )
    owner_id = int(conn.execute("SELECT id FROM leads WHERE email = 'owner@e.com'").fetchone()["id"])
    conn.execute(
        "INSERT INTO leads (name, email) VALUES ('Other', 'other@e.com')"
    )
    other_id = int(conn.execute("SELECT id FROM leads WHERE email = 'other@e.com'").fetchone()["id"])
    # `other` tries to claim the lowercase form of the same id
    conflict = wr.linkedin_sales_nav_id_field_conflict(conn, other_id, LOWER)
    conn.close()
    assert conflict is not None
    assert conflict["existing_lead_id"] == owner_id


if __name__ == "__main__":
    test_new_writes_preserve_canonical_case_on_leads_column()
    test_upsert_identity_alias_upgrades_lowercase_to_mixed()
    test_upsert_does_not_downgrade_mixed_to_lowercase()
    test_repair_merges_and_prefers_mixed_case_survivor()
    test_repair_is_idempotent()
    test_outbound_alias_uses_canonical_case_and_is_not_duplicated()
    test_field_conflict_matches_case_insensitively()
    print("OK")
