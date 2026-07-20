"""company_dedup_baseline_audit() (Stage C0) and
company_merge_candidates_backfill() (Stage C7): read-only report + idempotent
queueing of pre-existing duplicate-name groups, using conflicting lead
email-domains as the fingerprint of a likely pre-existing silent-merge."""

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


def test_audit_ignores_unique_names():
    conn = om.get_conn()
    om.ensure_company(conn, name="Only One", domain="only.com")
    conn.commit()
    report = om.company_dedup_baseline_audit(conn)
    assert report == []
    conn.close()


def test_audit_flags_conflicting_email_domains_as_likely_bad_merge():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Dup Co", domain="dup.com")
    lead = om.resolve_lead(email="jane@conflict.example", name="Jane", conn=conn)
    conn.execute(
        "UPDATE leads SET company_id = ?, email_domain = 'conflict.example' WHERE id = ?",
        (cid, lead["id"]),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert len(report) == 1
    group = report[0]
    assert group["name"] == "Dup Co"
    assert group["company_count"] == 2
    assert group["likely_bad_merge"] is True
    member = next(m for m in group["members"] if m["id"] == cid)
    assert member["conflicting_lead_domains"] == ["conflict.example"]
    conn.close()


def test_audit_does_not_flag_agreeing_email_domains():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Dup Co", domain="dup.com")
    lead = om.resolve_lead(email="jane@dup.com", name="Jane", conn=conn)
    conn.execute(
        "UPDATE leads SET company_id = ?, email_domain = 'dup.com' WHERE id = ?", (cid, lead["id"]),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert report[0]["likely_bad_merge"] is False
    conn.close()


def test_audit_respects_limit():
    conn = om.get_conn()
    for i in range(3):
        conn.execute("INSERT INTO companies (name) VALUES (?)", (f"Group{i}",))
        conn.execute("INSERT INTO companies (name) VALUES (?)", (f"Group{i}",))
    conn.commit()
    report = om.company_dedup_baseline_audit(conn, limit=2)
    assert len(report) == 2
    conn.close()


def test_backfill_is_idempotent():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Dup Co", domain="dup.com")
    lead = om.resolve_lead(email="jane@conflict.example", name="Jane", conn=conn)
    conn.execute(
        "UPDATE leads SET company_id = ?, email_domain = 'conflict.example' WHERE id = ?",
        (cid, lead["id"]),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    first = om.company_merge_candidates_backfill(conn)
    conn.commit()
    assert first["queued"] == 1
    assert first["skipped"] == 0

    second = om.company_merge_candidates_backfill(conn)
    conn.commit()
    assert second["queued"] == 0
    assert second["skipped"] == 1

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM company_merge_candidates WHERE reason = 'backfill_audit'"
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_backfill_never_auto_merges():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Dup Co", domain="dup.com")
    # Insert the lead directly rather than via resolve_lead(): resolve_lead()
    # always resolves/creates a company from the email's domain internally
    # (regardless of whether company= text is given), which is a real,
    # independent side effect this test doesn't want -- it only wants a lead
    # attached to `cid` with a conflicting email domain, nothing more.
    lead_id = conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@conflict.example", "conflict.example", cid),
    ).lastrowid
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()
    result = om.company_merge_candidates_backfill(conn)
    conn.commit()
    assert result["queued"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 2
    row = conn.execute("SELECT status FROM company_merge_candidates").fetchone()
    assert row["status"] == "pending"
    conn.close()


def test_domain_less_member_with_only_personal_email_leads_is_not_flagged():
    """Stage D1 fix: a company row with domain=NULL has nothing to compare
    its leads' personal email domains against -- it must never itself
    trigger conflicting_lead_domains/likely_bad_merge, and the informational
    personal_email_leads_only field should be True instead."""
    conn = om.get_conn()
    domainless_id = conn.execute("INSERT INTO companies (name) VALUES ('Dup Co')").lastrowid
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Caleb", "caleb.arnold1193@gmail.com", "gmail.com", domainless_id),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert len(report) == 1
    member = next(m for m in report[0]["members"] if m["id"] == domainless_id)
    assert member["conflicting_lead_domains"] == []
    assert member["personal_email_leads_only"] is True
    assert report[0]["likely_bad_merge"] is False
    conn.close()


def test_domain_less_member_with_non_personal_email_is_not_marked_personal_only():
    conn = om.get_conn()
    domainless_id = conn.execute("INSERT INTO companies (name) VALUES ('Dup Co')").lastrowid
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@somecompany.com", "somecompany.com", domainless_id),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    member = next(m for m in report[0]["members"] if m["id"] == domainless_id)
    assert member["personal_email_leads_only"] is False
    conn.close()


def test_audit_confidence_high_for_same_registrable_domain():
    conn = om.get_conn()
    a = om.ensure_company(conn, name="WVU", domain="mail.wvu.edu")
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@conflict.example", "conflict.example", a),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('WVU', 'wvu.edu')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert report[0]["likely_bad_merge"] is True
    assert report[0]["confidence"] == "HIGH"
    conn.close()


def test_audit_confidence_medium_for_different_registrable_domain():
    conn = om.get_conn()
    a = om.ensure_company(conn, name="WVU", domain="wvu.edu")
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@conflict.example", "conflict.example", a),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('WVU', 'wvup.edu')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert report[0]["likely_bad_merge"] is True
    assert report[0]["confidence"] == "MEDIUM"
    conn.close()


def test_audit_confidence_none_when_not_flagged():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Dup Co", domain="dup.com")
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@dup.com", "dup.com", cid),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'other.com')")
    conn.commit()

    report = om.company_dedup_baseline_audit(conn)
    assert report[0]["likely_bad_merge"] is False
    assert report[0]["confidence"] is None
    conn.close()


def test_backfill_queues_confidence_in_payload():
    conn = om.get_conn()
    a = om.ensure_company(conn, name="WVU", domain="mail.wvu.edu")
    conn.execute(
        "INSERT INTO leads (name, email, email_domain, company_id) VALUES (?, ?, ?, ?)",
        ("Jane", "jane@conflict.example", "conflict.example", a),
    )
    conn.execute("INSERT INTO companies (name, domain) VALUES ('WVU', 'wvu.edu')")
    conn.commit()

    om.company_merge_candidates_backfill(conn)
    conn.commit()

    row = conn.execute(
        "SELECT payload_json FROM company_merge_candidates WHERE reason = 'backfill_audit'"
    ).fetchone()
    import json
    payload = json.loads(row["payload_json"])
    assert payload["confidence"] == "HIGH"
    conn.close()
