"""Tests for Sales Nav hash vs public LinkedIn URL handling."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    is_sales_nav_hash_slug,
    linkedin_url_field_conflict,
    linkedin_url_is_hash,
    normalize_linkedin,
    parse_linkedin_value,
    promote_linkedin_url_from_identities,
    upsert_identity_alias,
)

SALES = "ACwAABAK84YBQ6cs16Ta-YfqZidA8SX2ywuCxhI"


def test_hash_slug_detection():
    assert is_sales_nav_hash_slug(SALES)
    assert is_sales_nav_hash_slug(SALES.lower())
    assert not is_sales_nav_hash_slug("sam-nav-handle")


def test_parse_rejects_hash_as_public_url():
    parsed = dict(parse_linkedin_value(f"linkedin.com/in/{SALES}"))
    assert parsed.get("linkedin_sales_nav_id") == SALES
    assert "linkedin_url" not in parsed


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_import_prefers_public_url_over_hash():
    _reset_db()
    row = {
        "name": "Import Test",
        "email": "import.sn@test.com",
        "company": "SN Co",
        "linkedin": f"https://www.linkedin.com/in/{SALES.lower()}",
        "linkedin url": "https://www.linkedin.com/in/real-handle",
        "member linkedin sales nav id": f"urn:li:fs_salesProfile:({SALES},NAME_SEARCH,x)",
    }
    summary = om.import_profiles([row], source="sales_navigator")
    lead_id = int(summary["results"][0]["id"])
    conn = om.get_conn()
    url = conn.execute(
        "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["linkedin_url"]
    conn.close()
    assert url == "linkedin.com/in/real-handle"
    assert not linkedin_url_is_hash(url)


def test_promote_from_identity():
    _reset_db()
    r = om.resolve_lead(
        name="Promote Test",
        email="promote@test.com",
        linkedin_url=f"linkedin.com/in/{SALES.lower()}",
    )
    lead_id = int(r["id"])
    conn = om.get_conn()
    upsert_identity_alias(
        conn, om.DEFAULT_ORG_ID, lead_id,
        "linkedin_url", "linkedin.com/in/promoted-handle",
        source="csv",
    )
    conn.execute(
        "UPDATE leads SET linkedin_url = ? WHERE id = ?",
        (f"linkedin.com/in/{SALES.lower()}", lead_id),
    )
    conn.commit()
    assert promote_linkedin_url_from_identities(conn, om.DEFAULT_ORG_ID, lead_id) is None
    url = conn.execute(
        "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["linkedin_url"]
    conn.close()
    assert url == "linkedin.com/in/promoted-handle"


def test_linkedin_url_field_conflict_detected():
    _reset_db()
    r1 = om.resolve_lead(
        name="Owner",
        email="owner@test.com",
        linkedin_url="https://www.linkedin.com/in/shared-handle",
    )
    owner_id = int(r1["id"])
    r2 = om.resolve_lead(name="Other Person", email="dup@test.com", company="Dup Co")
    dup_id = int(r2["id"])
    conn = om.get_conn()
    conflict = linkedin_url_field_conflict(conn, dup_id, "linkedin.com/in/shared-handle")
    conn.close()
    assert conflict is not None
    assert conflict["existing_lead_id"] == owner_id
    assert "message" in conflict


def test_upsert_reports_linkedin_url_conflict_in_import_summary():
    _reset_db()
    om.resolve_lead(
        name="Owner",
        email="owner@test.com",
        linkedin_url="https://www.linkedin.com/in/shared-handle",
    )
    om.resolve_lead(name="Other Person", email="dup@test.com", company="Dup Co")
    result = om.resolve_lead(
        email="dup@test.com",
        linkedin_url="https://www.linkedin.com/in/shared-handle",
        auto_merge=False,
    )
    assert result["linkedin_url_conflicts"]
    assert result["linkedin_url_conflicts"][0]["existing_lead_id"] == 1


if __name__ == "__main__":
    test_hash_slug_detection()
    test_parse_rejects_hash_as_public_url()
    test_import_prefers_public_url_over_hash()
    test_promote_from_identity()
    test_linkedin_url_field_conflict_detected()
    test_upsert_reports_linkedin_url_conflict_in_import_summary()
    print("OK")
