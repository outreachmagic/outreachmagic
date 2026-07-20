"""find_company_by_identity() resolution order (Stage C3): exact
company_identities lookup first, then a legacy companies.domain fallback for
'domain' (most of the 60k+ existing companies predate company_identities and
were never backfilled into it). linkedin_company_id/linkedin_company_url/
name_normalized are brand-new types with no legacy fallback."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID, find_company_by_identity  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def test_domain_exact_match_via_company_identities():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        """INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'domain', ?)""",
        (DEFAULT_ORG_ID, cid, "acme-branch.com"),
    )
    conn.commit()
    found = find_company_by_identity(conn, DEFAULT_ORG_ID, "domain", "acme-branch.com")
    assert found == cid
    conn.close()


def test_domain_falls_back_to_legacy_companies_column():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    # No company_identities row exists for this company (pre-migration company) --
    # must still resolve via the legacy companies.domain column.
    found = find_company_by_identity(conn, DEFAULT_ORG_ID, "domain", "acme.com")
    assert found == cid
    conn.close()


def test_unknown_domain_returns_none():
    conn = om.get_conn()
    assert find_company_by_identity(conn, DEFAULT_ORG_ID, "domain", "nowhere.example") is None
    conn.close()


def test_linkedin_company_id_has_no_legacy_fallback():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        """INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'linkedin_company_id', ?)""",
        (DEFAULT_ORG_ID, cid, "123456"),
    )
    conn.commit()
    assert find_company_by_identity(conn, DEFAULT_ORG_ID, "linkedin_company_id", "123456") == cid
    assert find_company_by_identity(conn, DEFAULT_ORG_ID, "linkedin_company_id", "999999") is None
    conn.close()


def test_name_normalized_never_auto_matches_via_this_function_without_a_row():
    """name_normalized is a WEAK identity type -- it only resolves anything
    when a row was explicitly written for it (never as an implicit fallback
    like domain has)."""
    conn = om.get_conn()
    om.ensure_company(conn, name="Acme")
    conn.commit()
    assert find_company_by_identity(conn, DEFAULT_ORG_ID, "name_normalized", "acme") is None
    conn.close()
