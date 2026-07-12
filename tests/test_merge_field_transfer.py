"""Regression test: merge_leads() must transfer all fields, not just 8 of ~26.

Bug: only email, email_domain, linkedin_url, company_id, company, title,
industry, headcount, stage survived a merge. If the keep lead had a NULL/empty
field and the merge target had data, that data was silently dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_merge_transfers_all_disjoint_fields_onto_survivor():
    _reset_db()
    keep = om.resolve_lead(name="Keep Lead", email="keep@example.com")
    other = om.resolve_lead(name="Merge Lead", linkedin_url="https://linkedin.com/in/merge-lead")
    keep_id = int(keep["id"])
    merge_id = int(other["id"])

    conn = om.get_conn()
    # resolve_lead() always stamps latest_source/_detail/_platform/_at at
    # creation time, so null them out on keep to exercise the "keep is
    # empty" fill-if-empty path these COALESCE fields are meant to cover.
    conn.execute(
        """UPDATE leads SET
           latest_source = NULL, latest_source_detail = NULL,
           latest_source_platform = NULL, latest_source_at = NULL
           WHERE id = ?""",
        (keep_id,),
    )
    # Merge target carries data the keep lead is missing on every one of the
    # 18 previously-dropped fields.
    conn.execute(
        """UPDATE leads SET
           linkedin_sales_nav_id = 'ACwAAsalesnav123',
           headcount_numeric = 250,
           location_city = 'Austin',
           location_state = 'TX',
           location_country = 'US',
           linkedin_headline = 'VP of Sales at Acme',
           linkedin_bio = 'Long bio text here',
           notes = 'Sourced from Sales Nav list 3',
           email_verification_status = 'valid',
           email_verified_at = '2026-06-01 00:00:00',
           latest_source = 'sales_nav_csv',
           latest_source_detail = 'career services - sales nav 1 of 3',
           latest_source_platform = 'csv',
           latest_source_at = '2026-06-01 00:00:00',
           last_contact_at = '2026-06-02 00:00:00',
           next_action = 'follow up call',
           latest_sender = 'rep@ourcompany.com',
           latest_sender_platform = 'smartlead'
           WHERE id = ?""",
        (merge_id,),
    )
    conn.commit()
    conn.close()

    conn = om.get_conn()
    om.merge_leads(keep_id, merge_id, reason="dedup", conn=conn)
    conn.commit()
    conn.close()

    conn = om.get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (keep_id,)).fetchone()
    conn.close()

    assert row["linkedin_sales_nav_id"] == "ACwAAsalesnav123"
    assert row["headcount_numeric"] == 250
    assert row["location_city"] == "Austin"
    assert row["location_state"] == "TX"
    assert row["location_country"] == "US"
    assert row["linkedin_headline"] == "VP of Sales at Acme"
    assert row["linkedin_bio"] == "Long bio text here"
    assert row["notes"] == "Sourced from Sales Nav list 3"
    assert row["email_verification_status"] == "valid"
    assert row["email_verified_at"] == "2026-06-01 00:00:00"
    assert row["latest_source"] == "sales_nav_csv"
    assert row["latest_source_detail"] == "career services - sales nav 1 of 3"
    assert row["latest_source_platform"] == "csv"
    assert row["latest_source_at"] == "2026-06-01 00:00:00"
    assert row["last_contact_at"] == "2026-06-02 00:00:00"
    assert row["next_action"] == "follow up call"
    assert row["latest_sender"] == "rep@ourcompany.com"
    assert row["latest_sender_platform"] == "smartlead"


def test_merge_keeps_survivor_field_when_both_sides_populated():
    _reset_db()
    keep = om.resolve_lead(name="Keep Lead", email="keep2@example.com")
    other = om.resolve_lead(name="Merge Lead", linkedin_url="https://linkedin.com/in/merge-lead-2")
    keep_id = int(keep["id"])
    merge_id = int(other["id"])

    conn = om.get_conn()
    conn.execute(
        "UPDATE leads SET notes = 'keep notes', last_contact_at = '2026-01-01 00:00:00' WHERE id = ?",
        (keep_id,),
    )
    conn.execute(
        "UPDATE leads SET notes = 'other notes', last_contact_at = '2026-02-01 00:00:00' WHERE id = ?",
        (merge_id,),
    )
    conn.commit()
    conn.close()

    conn = om.get_conn()
    om.merge_leads(keep_id, merge_id, reason="dedup", conn=conn)
    conn.commit()
    conn.close()

    conn = om.get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (keep_id,)).fetchone()
    conn.close()

    # Survivor's existing non-null values are preserved, not overwritten.
    assert row["notes"] == "keep notes"
    assert row["last_contact_at"] == "2026-01-01 00:00:00"


if __name__ == "__main__":
    test_merge_transfers_all_disjoint_fields_onto_survivor()
    test_merge_keeps_survivor_field_when_both_sides_populated()
    print("OK")
