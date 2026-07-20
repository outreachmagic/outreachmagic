"""CLI surface for company entity-resolution (Stage C5): `company
dedup-audit`, `company backfill-candidates`, `company merge-review
list/approve/reject`, `company merge --keep --merge`."""

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_cli  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _run(*extra_args):
    stdout = io.StringIO()
    argv = ["pipeline_cli.py", "company", *extra_args]
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(stdout):
            try:
                pipeline_cli.main()
            except SystemExit:
                pass
    return json.loads(stdout.getvalue())


def test_dedup_audit_reports_duplicate_name_groups():
    conn = om.get_conn()
    a = om.ensure_company(conn, name="Acme Services", domain="a.com")
    lead = om.resolve_lead(email="j@other-domain.com", name="Jane", conn=conn)
    conn.execute("UPDATE leads SET company_id = ?, email_domain = 'other-domain.com' WHERE id = ?", (a, lead["id"]))
    conn.commit()
    # Inserted directly (not via ensure_company): simulates a duplicate-name
    # pair that already exists in the database, independent of the live
    # matching behavior under test elsewhere.
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Acme Services', 'unrelated-b.com')")
    conn.commit()
    conn.close()

    out = _run("dedup-audit")
    assert out["status"] == "ok"
    groups = [g for g in out["groups"] if g["name"] == "Acme Services"]
    assert len(groups) == 1
    assert groups[0]["company_count"] == 2
    assert groups[0]["likely_bad_merge"] is True


def test_backfill_candidates_then_merge_review_list_approve():
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="Acme Services")
    conn.commit()
    new_id = om.ensure_company(conn, name="Acme Services", domain="unrelated.com")
    conn.commit()
    conn.close()

    listed = _run("merge-review", "list", "--reason", "name_only_domain_attach")
    assert listed["count"] == 1
    candidate_id = listed["candidates"][0]["id"]
    assert listed["candidates"][0]["existing_company_id"] == existing_id
    assert listed["candidates"][0]["candidate_company_id"] == new_id

    approved = _run("merge-review", "approve", "--id", candidate_id)
    assert approved["merge_result"]["status"] == "merged"

    conn = om.get_conn()
    assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (new_id,)).fetchone() is None
    status = conn.execute(
        "SELECT status FROM company_merge_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()["status"]
    assert status == "resolved"
    conn.close()


def test_merge_review_reject():
    conn = om.get_conn()
    om.ensure_company(conn, name="Acme Services")
    conn.commit()
    om.ensure_company(conn, name="Acme Services", domain="unrelated.com")
    conn.commit()
    conn.close()

    listed = _run("merge-review", "list")
    candidate_id = listed["candidates"][0]["id"]
    rejected = _run("merge-review", "reject", "--id", candidate_id, "--note", "different companies")
    assert rejected["dismissed"] is True

    conn = om.get_conn()
    row = conn.execute(
        "SELECT status, payload_json FROM company_merge_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    assert row["status"] == "dismissed"
    assert "different companies" in row["payload_json"]
    conn.close()


def test_direct_merge_command():
    conn = om.get_conn()
    keep_id = om.ensure_company(conn, name="Acme", domain="acme.com")
    merge_id = om.ensure_company(conn, name="Acme Corp")
    conn.commit()
    conn.close()

    out = _run("merge", "--keep", str(keep_id), "--merge", str(merge_id))
    assert out["status"] == "merged"
    conn = om.get_conn()
    assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (merge_id,)).fetchone() is None
    conn.close()


def test_merge_review_list_min_confidence_filters_out_low():
    conn = om.get_conn()
    # HIGH-confidence candidate: existing "Acme" has no primary domain but
    # DOES have a known company_identities domain sharing a registrable
    # domain with the incoming one.
    acme_id = om.ensure_company(conn, name="Acme")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com')",
        (acme_id,),
    )
    conn.commit()
    om.ensure_company(conn, name="Acme", domain="sub.acme.com")
    conn.commit()

    # LOW-confidence candidate: no known domains at all to compare against.
    om.ensure_company(conn, name="Beta")
    conn.commit()
    om.ensure_company(conn, name="Beta", domain="totallyunrelated.com")
    conn.commit()
    conn.close()

    all_listed = _run("merge-review", "list")
    assert all_listed["count"] == 2

    high_only = _run("merge-review", "list", "--min-confidence", "HIGH")
    assert high_only["count"] == 1
    payload = high_only["candidates"][0]["payload"]
    assert payload["confidence"] == "HIGH"
    assert payload["existing_name"] == "Acme"


def test_merge_review_list_min_confidence_recomputes_for_legacy_payload():
    """A candidate row queued before Stage D3 added "confidence" to the
    payload must still filter correctly via the read-time recompute
    fallback, not be silently excluded or crash."""
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="WVU", domain="wvu.edu")
    candidate_id = om.ensure_company(conn, name="WVU", domain="mail.wvu.edu")
    conn.commit()
    # Simulate a pre-Stage-D3 candidate: no "confidence" key in payload_json.
    conn.execute(
        """INSERT INTO company_merge_candidates
               (id, org_id, existing_company_id, candidate_company_id, reason, status, payload_json)
           VALUES ('cmc_legacy', 'default', ?, ?, 'backfill_audit', 'pending',
                   '{"a": {"domain": "wvu.edu"}, "b": {"domain": "mail.wvu.edu"}}')""",
        (existing_id, candidate_id),
    )
    conn.commit()
    conn.close()

    high_only = _run("merge-review", "list", "--reason", "backfill_audit", "--min-confidence", "HIGH")
    assert high_only["count"] == 1
    assert high_only["candidates"][0]["id"] == "cmc_legacy"
    assert "confidence" not in (high_only["candidates"][0]["payload"] or {})


def test_backfill_candidates_command_queues_from_audit():
    conn = om.get_conn()
    a = om.ensure_company(conn, name="Dup Co", domain="a.com")
    lead = om.resolve_lead(email="j@conflict.com", name="Jane", conn=conn)
    conn.execute("UPDATE leads SET company_id = ?, email_domain = 'conflict.com' WHERE id = ?", (a, lead["id"]))
    conn.commit()
    # Second row created directly (bypassing ensure_company's own fallback
    # logging) so this exercises the *backfill* path, not the live one.
    conn.execute("INSERT INTO companies (name, domain) VALUES ('Dup Co', 'b.com')")
    conn.commit()
    conn.close()

    out = _run("backfill-candidates")
    assert out["status"] == "ok"
    assert out["queued"] >= 1

    listed = _run("merge-review", "list", "--reason", "backfill_audit")
    assert listed["count"] >= 1
