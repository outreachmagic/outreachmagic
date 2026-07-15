"""Bulk-tagging by identity value (e.g. linkedin_sales_nav_id), not just by
already-known lead id.

A fresh Sales Nav import's only stable identity is linkedin_sales_nav_id --
there was previously no way to bulk-tag those leads without first resolving
ids yourself, which for a few thousand values meant writing raw SQL and
hitting SQLite's SQLITE_LIMIT_COMPOUND_SELECT (500 terms) on a compound
SELECT. tag_bulk_by_identity resolves one lookup at a time via
find_lead_by_identity, so there's no compound SELECT to hit that limit.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_lead_with_sales_nav_id(conn, sales_nav_id, email):
    lead_id = om.resolve_lead(
        email=email, name="Test Lead", company="Acme",
        identities=[("linkedin_sales_nav_id", sales_nav_id)],
    )["id"]
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()
    return lead_id, ws_row["id"]


def test_tag_bulk_by_identity_resolves_and_tags():
    conn = om.get_conn()
    lead1, ws_id = _mk_lead_with_sales_nav_id(conn, "ACwAAAdePicBNJam85-7o1AHdAggDPtCigKhWTs", "a@example.com")
    lead2, _ = _mk_lead_with_sales_nav_id(conn, "ACwAAA0OiSwB9n_y_CcUuyV5f-1hBYf_E8IIxXU", "b@example.com")
    conn.close()

    result = om.tag_bulk_by_identity(
        ws_id, "linkedin_sales_nav_id",
        ["ACwAAAdePicBNJam85-7o1AHdAggDPtCigKhWTs", "ACwAAA0OiSwB9n_y_CcUuyV5f-1hBYf_E8IIxXU"],
        ["nace"],
    )
    assert result["resolved"] == 2
    assert result["unresolved"] == []
    assert result["changed"] == 2

    conn = om.get_conn()
    tags = {
        r["lead_id"] for r in conn.execute(
            "SELECT lead_id FROM workspace_lead_tags WHERE workspace_id = ? AND tag = 'nace'", (ws_id,),
        ).fetchall()
    }
    conn.close()
    assert tags == {lead1, lead2}


def test_tag_bulk_by_identity_matches_mixed_case_sales_nav_id():
    """Sales-nav ids in lead_identities are stored lowercase (the match key);
    a batch of values in canonical mixed case (as they'd appear in a fresh
    Sales Nav export) must still resolve."""
    conn = om.get_conn()
    lead_id, ws_id = _mk_lead_with_sales_nav_id(conn, "acwaaadepicbnjam85-7o1ahdaggdptcigkhwts", "c@example.com")
    conn.close()

    result = om.tag_bulk_by_identity(
        ws_id, "linkedin_sales_nav_id", ["ACwAAAdePicBNJam85-7o1AHdAggDPtCigKhWTs"], ["nace"],
    )
    assert result["resolved"] == 1
    assert result["unresolved"] == []


def test_tag_bulk_by_identity_reports_unresolved_values():
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    conn.close()

    result = om.tag_bulk_by_identity(
        ws_row["id"], "linkedin_sales_nav_id", ["ACwAA_does_not_exist"], ["nace"],
    )
    assert result["resolved"] == 0
    assert result["unresolved"] == ["ACwAA_does_not_exist"]
    assert result["changed"] == 0


def test_tag_bulk_by_identity_email():
    conn = om.get_conn()
    lead_id = om.resolve_lead(email="find-me@example.com", name="Find Me", company="Acme")["id"]
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()
    conn.close()

    result = om.tag_bulk_by_identity(ws_row["id"], "email", ["FIND-ME@EXAMPLE.COM"], ["nace"])
    assert result["resolved"] == 1
    assert result["unresolved"] == []
