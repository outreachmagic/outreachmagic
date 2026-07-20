"""company_domain_email_stats() and company_domain_stats_report() (Stage D6):
empirical found/attempted counts per domain, computed on demand from
lead_provider_attempts joined through leads.company_id -- and their effect
on rank_company_domains()'s ordering."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_provider_attempts import record_provider_attempt  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _lead_at_company(conn, company_id, name):
    lead = om.resolve_lead(name=name, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (company_id, lead["id"]))
    return lead["id"]


def test_stats_counts_found_and_attempted_per_domain():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    lead1 = _lead_at_company(conn, cid, "Jane")
    lead2 = _lead_at_company(conn, cid, "John")
    conn.commit()

    record_provider_attempt(conn, lead1, "trykitt", status="found", domain="mail.acme.com")
    record_provider_attempt(conn, lead2, "trykitt", status="not_found", domain="mail.acme.com")
    record_provider_attempt(conn, lead1, "icypeas", status="not_found", domain="acme.com")
    conn.commit()

    stats = om.company_domain_email_stats(conn, cid)
    assert stats["mail.acme.com"] == {"found": 1, "attempted": 2}
    assert stats["acme.com"] == {"found": 0, "attempted": 1}
    conn.close()


def test_found_count_outranks_role_in_rank_company_domains():
    """User-requested: prioritize whichever domain has the most emails
    associated with it -- a domain with real found history outranks a
    role='email' domain with zero found history."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email')",
        (cid,),
    )
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized) "
        "VALUES ('default', ?, 'domain', 'coe.acme.com')",
        (cid,),
    )
    lead = _lead_at_company(conn, cid, "Jane")
    conn.commit()
    # coe.acme.com has no role at all, but 3 real found emails.
    for i in range(3):
        record_provider_attempt(conn, lead, f"trykitt{i}", status="found", domain="coe.acme.com")
    conn.commit()

    ranked = om.rank_company_domains(conn, cid)
    assert ranked[0] == "coe.acme.com", "found-count must outrank role when found > 0"
    conn.close()


def test_untried_domains_fall_back_to_role_ordering_unchanged():
    """Two domains both at found=0 (the common case, nothing tried yet)
    must not be penalized relative to each other -- today's role/verified/
    recency ordering still applies."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email')",
        (cid,),
    )
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized) "
        "VALUES ('default', ?, 'domain', 'coe.acme.com')",
        (cid,),
    )
    conn.commit()

    ranked = om.rank_company_domains(conn, cid)
    assert ranked[0] == "mail.acme.com", "role='email' must still win when both are untried"
    conn.close()


def test_domain_stats_report_shape():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role, label) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email', 'Mail Server')",
        (cid,),
    )
    lead = _lead_at_company(conn, cid, "Jane")
    conn.commit()
    record_provider_attempt(conn, lead, "trykitt", status="found", domain="mail.acme.com")
    conn.commit()

    report = om.company_domain_stats_report(conn, cid)
    assert report["company_name"] == "Acme"
    domains = {d["domain"]: d for d in report["domains"]}
    assert domains["mail.acme.com"]["found"] == 1
    assert domains["mail.acme.com"]["role"] == "email"
    assert domains["mail.acme.com"]["label"] == "Mail Server"
    assert domains["mail.acme.com"]["rank"] == 1
    assert domains["acme.com"]["found"] == 0
    conn.close()


def test_domain_stats_report_unknown_company_returns_none():
    conn = om.get_conn()
    assert om.company_domain_stats_report(conn, 999999) is None
    conn.close()
