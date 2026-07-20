"""set_company_domain_label() and the company_identities.label column
(Stage D7): human-curated branch/department labels, purely descriptive,
never used by matching/ranking/confidence logic."""

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


def test_label_on_existing_company_identities_row_persists():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized) "
        "VALUES ('default', ?, 'domain', 'coe.acme.com')",
        (cid,),
    )
    conn.commit()
    conn.close()

    result = om.set_company_domain_label(cid, "coe.acme.com", "College of Engineering")
    assert result["status"] == "ok"

    conn = om.get_conn()
    row = conn.execute(
        "SELECT label FROM company_identities WHERE company_id = ? AND identity_value_normalized = ?",
        (cid, "coe.acme.com"),
    ).fetchone()
    assert row["label"] == "College of Engineering"
    conn.close()


def test_label_on_legacy_primary_domain_creates_identity_row():
    """Labeling companies.domain itself (no company_identities row for it
    yet) must create one rather than error, so the label has somewhere to
    live."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    conn.close()

    result = om.set_company_domain_label(cid, "acme.com", "HQ")
    assert result["status"] == "ok"

    conn = om.get_conn()
    row = conn.execute(
        "SELECT label FROM company_identities WHERE company_id = ? AND identity_value_normalized = ?",
        (cid, "acme.com"),
    ).fetchone()
    assert row is not None
    assert row["label"] == "HQ"
    conn.close()


def test_label_unknown_domain_for_company_errors():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    conn.close()

    result = om.set_company_domain_label(cid, "totallyunrelated.com", "Nope")
    assert result["status"] == "error"


def test_label_unknown_company_errors():
    result = om.set_company_domain_label(999999, "acme.com", "HQ")
    assert result["status"] == "error"


def test_no_label_returns_none_and_never_blocks_matching():
    """A domain with no label set must not affect ensure_company() matching
    or rank_company_domains() ordering."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    again = om.ensure_company(conn, name="Something Else", domain="acme.com")
    assert again == cid
    report = om.company_domain_stats_report(conn, cid)
    assert report["domains"][0]["label"] is None
    conn.close()


def test_migration_alter_table_is_idempotent():
    """Running migrate_db() twice (as happens on every CLI invocation) must
    not error on the label column already existing."""
    conn = om.get_conn()
    om.migrate_db(conn)
    om.migrate_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_identities)").fetchall()}
    assert "label" in cols
    conn.close()
