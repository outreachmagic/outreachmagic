"""Tests for Sales Nav hash vs public LinkedIn URL handling."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    build_import_identities,
    is_sales_nav_hash_slug,
    linkedin_url_field_conflict,
    linkedin_url_is_hash,
    normalize_linkedin,
    parse_linkedin_value,
    promote_linkedin_url_from_identities,
    upsert_identity_alias,
)
import import_formats as imf  # noqa: E402

SALES = "ACwAABAK84YBQ6cs16Ta-YfqZidA8SX2ywuCxhI"


def test_hash_slug_detection():
    assert is_sales_nav_hash_slug(SALES)
    assert is_sales_nav_hash_slug(SALES.lower())
    assert not is_sales_nav_hash_slug("sam-nav-handle")


def test_parse_rejects_hash_as_public_url():
    parsed = dict(parse_linkedin_value(f"linkedin.com/in/{SALES}"))
    assert parsed.get("linkedin_sales_nav_id") == SALES
    assert "linkedin_url" not in parsed


SALES_PEOPLE_URL = f"https://www.linkedin.com/sales/people/{SALES},NAME_SEARCH,kOmY"
SALES_LEAD_URL = f"https://www.linkedin.com/sales/lead/{SALES},NAME_SEARCH,kOmY"


def test_sales_people_url_detected_as_hash():
    """linkedin_in_slug() only matches /in/<slug> -- a bare Apify
    salesNavigatorUrl (linkedin.com/sales/people/<token>,...) has no /in/
    segment, so it used to fall through linkedin_url_is_hash() as "not a
    hash" and leak into linkedin_url verbatim. See
    OM-IMPORT-FIELD-MAPPING-DESIGN.md, Bonus Bug."""
    assert linkedin_url_is_hash(normalize_linkedin(SALES_PEOPLE_URL))
    assert linkedin_url_is_hash(normalize_linkedin(SALES_LEAD_URL))


def test_sales_people_url_not_picked_as_public_linkedin_url():
    row = {"linkedin": SALES_PEOPLE_URL}
    assert om._best_linkedin_from_row(row) is None


def test_sales_people_url_still_yields_sales_nav_id_for_dedup():
    """Rejecting the URL from linkedin_url must not cost the identity
    system its dedup signal -- the Sales Nav ID extraction is a separate
    code path from linkedin_url_is_hash() and must keep working."""
    parsed = dict(parse_linkedin_value(SALES_PEOPLE_URL))
    assert parsed.get("linkedin_sales_nav_id") == SALES
    assert "linkedin_url" not in parsed


def test_sales_nav_id_extracted_end_to_end_when_it_is_the_only_linkedin_column():
    """Regression: profile["linkedin"] is (correctly) hash/Sales-Nav filtered
    for the leads.linkedin_url column, but build_import_identities() used to
    read that SAME filtered field for Sales Nav ID extraction. When the
    Sales Nav URL arrives ONLY through the linkedin/LinkedInUrl column (no
    separate "member linkedin sales nav id" column) -- Modern Storefront's
    actual CSV shape -- linkedin_sales_nav_id was silently never extracted,
    and dedup fell all the way to import_key (worse than the pre-fix
    name_company fallback). profile["linkedin_raw"] (unfiltered) must cover
    this. See OM-IMPORT-FIELD-MAPPING-DESIGN.md."""
    raw = {"Name": "Jane", "LinkedInUrl": SALES_PEOPLE_URL}
    row = imf.normalize_import_row(raw)
    profile = om.normalize_profile_row(row)
    assert profile.get("linkedin") is None  # still correctly rejected for the DB column

    identities = dict(build_import_identities(profile, {}))
    assert identities.get("linkedin_sales_nav_id") == SALES


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
