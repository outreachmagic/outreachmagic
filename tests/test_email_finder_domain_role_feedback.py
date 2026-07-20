"""Stage D5's "feed the win back" mechanism: when apply_email_find_results()
records a provider attempt with status='found' and a domain, that domain's
company_identities row gets its role bumped to 'email' (if not already set),
so the next lead at the same company tries it first via
rank_company_domains() without re-searching every known domain."""

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


def test_found_attempt_creates_identity_row_with_email_role():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    lead = om.resolve_lead(name="Jane", company="Acme", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    om.create_workspace("WS", slug="ws")
    conn.close()

    om.apply_email_find_results(
        [{
            "id": lead["id"],
            "email": "jane@mail.acme.com",
            "_provider_attempts": [
                {"provider": "trykitt", "status": "found", "domain": "mail.acme.com",
                 "result_email": "jane@mail.acme.com"},
            ],
        }],
        workspace="ws",
        source="trykitt",
    )

    conn = om.get_conn()
    row = conn.execute(
        """SELECT role FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain' AND identity_value_normalized = ?""",
        (cid, "mail.acme.com"),
    ).fetchone()
    assert row is not None
    assert row["role"] == "email"
    conn.close()


def test_not_found_attempt_does_not_create_or_bump_role():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    lead = om.resolve_lead(name="Jane", company="Acme", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    om.create_workspace("WS", slug="ws")
    conn.close()

    om.apply_email_find_results(
        [{
            "id": lead["id"],
            "_provider_attempts": [
                {"provider": "trykitt", "status": "not_found", "domain": "coe.acme.com"},
            ],
        }],
        workspace="ws",
        source="trykitt",
    )

    conn = om.get_conn()
    row = conn.execute(
        """SELECT role FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain' AND identity_value_normalized = ?""",
        (cid, "coe.acme.com"),
    ).fetchone()
    assert row is None
    conn.close()


def test_never_overwrites_an_existing_curated_role():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role) "
        "VALUES ('default', ?, 'domain', 'coe.acme.com', 'branch')",
        (cid,),
    )
    lead = om.resolve_lead(name="Jane", company="Acme", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    om.create_workspace("WS", slug="ws")
    conn.close()

    om.apply_email_find_results(
        [{
            "id": lead["id"],
            "email": "jane@coe.acme.com",
            "_provider_attempts": [
                {"provider": "trykitt", "status": "found", "domain": "coe.acme.com",
                 "result_email": "jane@coe.acme.com"},
            ],
        }],
        workspace="ws",
        source="trykitt",
    )

    conn = om.get_conn()
    row = conn.execute(
        """SELECT role FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain' AND identity_value_normalized = ?""",
        (cid, "coe.acme.com"),
    ).fetchone()
    assert row["role"] == "branch", "an already-curated role must never be silently overwritten"
    conn.close()
