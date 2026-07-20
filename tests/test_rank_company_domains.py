"""rank_company_domains() (Stage C2b): best-first domain candidates for
email-finding. A real company can send mail from a different domain than its
website, or from several per-branch domains -- this ranks role='email' over
'website'/'branch'/unknown, then verified_mx, then recency, falling back to
the legacy single companies.domain column when no company_identities domain
rows exist yet (true for most pre-migration companies)."""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _add_domain(conn, company_id, value, *, role=None, verified_mx=None, source=None):
    conn.execute(
        """INSERT INTO company_identities
               (org_id, company_id, identity_type, identity_value_normalized, role, verified_mx, source)
           VALUES (?, ?, 'domain', ?, ?, ?, ?)""",
        (DEFAULT_ORG_ID, company_id, value, role, verified_mx, source),
    )


def test_falls_back_to_legacy_companies_domain_when_no_identity_rows():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    assert om.rank_company_domains(conn, cid) == ["acme.com"]
    conn.close()


def test_empty_list_when_no_domain_anywhere():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    conn.commit()
    assert om.rank_company_domains(conn, cid) == []
    conn.close()


def test_email_role_ranks_above_website_and_unknown():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    _add_domain(conn, cid, "acme.com", role="website")
    _add_domain(conn, cid, "acme-mail.io", role="email")
    _add_domain(conn, cid, "acme-branch.com", role=None)
    conn.commit()
    ranked = om.rank_company_domains(conn, cid)
    assert ranked[0] == "acme-mail.io"
    assert set(ranked) == {"acme.com", "acme-mail.io", "acme-branch.com"}
    conn.close()


def test_verified_mx_breaks_ties_within_same_role():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    _add_domain(conn, cid, "acme-unverified.com", role="website", verified_mx=None)
    _add_domain(conn, cid, "acme-verified.com", role="website", verified_mx=1)
    conn.commit()
    ranked = om.rank_company_domains(conn, cid)
    assert ranked[0] == "acme-verified.com"
    conn.close()


def test_most_recent_breaks_remaining_ties():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    _add_domain(conn, cid, "acme-older.com", role="branch")
    time.sleep(1.1)
    _add_domain(conn, cid, "acme-newer.com", role="branch")
    conn.commit()
    ranked = om.rank_company_domains(conn, cid)
    assert ranked[0] == "acme-newer.com"
    conn.close()
