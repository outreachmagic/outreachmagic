"""merge_companies() (Stage C6): FK reconciliation for leads.company_id and
company_personalization (including the composite-PK collision case),
company_identities merge producing a real multi-domain survivor, the
company_merges audit row, and rollback-on-error."""

import sys
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


def test_noop_when_keep_equals_merge():
    result = om.merge_companies(1, 1)
    assert result == {"status": "noop", "keep_id": 1}


def test_error_when_company_not_found():
    result = om.merge_companies(999999, 999998)
    assert result["status"] == "error"


def test_leads_repointed_to_keep_id():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp")
    lead = om.resolve_lead(email="jane@acme.com", name="Jane", company="Acme", conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (merge_id, lead["id"]))
    conn.commit()

    result = om.merge_companies(keep_id, merge_id, reason="test")
    assert result["status"] == "merged"

    row = conn.execute("SELECT company_id FROM leads WHERE id = ?", (lead["id"],)).fetchone()
    assert row["company_id"] == keep_id
    assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (merge_id,)).fetchone() is None
    conn.close()


def test_company_personalization_moves_without_collision():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp")
    conn.execute(
        "INSERT INTO company_personalization (company_id, field_name, field_value) VALUES (?, 'tagline', 'Only Here')",
        (merge_id,),
    )
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()

    row = conn.execute(
        "SELECT field_value FROM company_personalization WHERE company_id = ? AND field_name = 'tagline'",
        (keep_id,),
    ).fetchone()
    assert row["field_value"] == "Only Here"
    conn.close()


def test_company_personalization_keep_wins_on_field_name_collision():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp")
    conn.execute(
        "INSERT INTO company_personalization (company_id, field_name, field_value) VALUES (?, 'tagline', 'Keep Wins')",
        (keep_id,),
    )
    conn.execute(
        "INSERT INTO company_personalization (company_id, field_name, field_value) VALUES (?, 'tagline', 'Should Be Dropped')",
        (merge_id,),
    )
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()

    rows = conn.execute(
        "SELECT field_value FROM company_personalization WHERE company_id = ? AND field_name = 'tagline'",
        (keep_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["field_value"] == "Keep Wins"
    conn.close()


def test_company_identities_merge_produces_multi_domain_survivor():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp", domain="acme-branch.com")
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()

    ranked = om.rank_company_domains(conn, keep_id)
    assert set(ranked) == {"acme.com", "acme-branch.com"}
    assert conn.execute(
        "SELECT 1 FROM company_identities WHERE company_id = ?", (merge_id,)
    ).fetchone() is None
    conn.close()


def test_company_merges_audit_row_written():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp")
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test_reason", conn=conn)
    conn.commit()

    row = conn.execute(
        "SELECT * FROM company_merges WHERE keep_id = ? AND merge_id = ?", (keep_id, merge_id),
    ).fetchone()
    assert row is not None
    assert row["reason"] == "test_reason"
    assert row["relay_delete_pushed"] == 0
    conn.close()


def test_merge_field_reconciliation_prefers_keep_falls_back_to_other():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp", industry="Widgets", headcount="11-50")
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()

    row = conn.execute("SELECT * FROM companies WHERE id = ?", (keep_id,)).fetchone()
    assert row["domain"] == "acme.com"
    assert row["industry"] == "Widgets"
    assert row["headcount"] == "11-50"
    conn.close()


def test_pick_keep_company_prefers_richer_identity():
    import pipeline_dedup

    a = {"id": 1, "name": "Acme", "domain": None, "linkedin_company_id": None}
    b = {"id": 2, "name": "Acme Corporation", "domain": "acme.com", "linkedin_company_id": None}
    assert pipeline_dedup.pick_keep_company([a, b])["id"] == 2


def test_domain_email_stats_reflect_merge_automatically():
    """Stage D6: found-count stats are computed on demand from
    lead_provider_attempts joined through leads.company_id -- after a merge
    moves the losing company's leads onto the keep id, their attempt history
    is picked up automatically with no merge-time counter-summing logic
    needed."""
    from pipeline_provider_attempts import record_provider_attempt

    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp", domain="acme-branch.com")
    keep_lead = om.resolve_lead(name="Jane", source="csv", allow_weak_identity=True, conn=conn)
    merge_lead = om.resolve_lead(name="John", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (keep_id, keep_lead["id"]))
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (merge_id, merge_lead["id"]))
    conn.commit()
    record_provider_attempt(conn, keep_lead["id"], "trykitt", status="found", domain="acme.com")
    record_provider_attempt(conn, merge_lead["id"], "trykitt", status="found", domain="acme-branch.com")
    conn.commit()

    om.merge_companies(keep_id, merge_id, reason="test", conn=conn)
    conn.commit()

    stats = om.company_domain_email_stats(conn, keep_id)
    assert stats["acme.com"]["found"] == 1
    assert stats["acme-branch.com"]["found"] == 1
    conn.close()
