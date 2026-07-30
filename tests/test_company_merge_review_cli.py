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


# ── choosing which record survives ───────────────────────────────────────────
#
# Approving used to keep existing_company_id unconditionally. "Existing" is only
# whichever row ingest happened to see first, and that is frequently the emptier
# of the two — so the survivor was being picked by arrival order rather than by
# which record was better.


def _queued_pair():
    """A queued name_only_domain_attach pair, returning (candidate_id, a, b)."""
    conn = om.get_conn()
    a = om.ensure_company(conn, name="Acme Services")
    conn.commit()
    b = om.ensure_company(conn, name="Acme Services", domain="unrelated.com")
    conn.commit()
    conn.close()
    listed = om.list_company_merge_candidates(reason="name_only_domain_attach")
    return listed["candidates"][0]["id"], a, b


def test_keep_id_chooses_the_surviving_record():
    candidate_id, a, b = _queued_pair()
    out = om.approve_company_merge_candidate(candidate_id, keep_id=b)
    assert out["merge_result"]["status"] == "merged"
    conn = om.get_conn()
    try:
        assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (a,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (b,)).fetchone() is not None
    finally:
        conn.close()


def test_the_survivor_inherits_what_it_was_missing():
    """The rule the UI promises: the record you keep wins field by field, and
    fills its blanks from the one merged away. It lives in merge_companies()."""
    candidate_id, a, b = _queued_pair()
    conn = om.get_conn()
    conn.execute("UPDATE companies SET industry = 'Events', headcount = '11-50' WHERE id = ?", (a,))
    conn.execute("UPDATE companies SET industry = 'Software' WHERE id = ?", (b,))
    conn.commit()
    conn.close()
    om.approve_company_merge_candidate(candidate_id, keep_id=b)
    conn = om.get_conn()
    try:
        row = conn.execute(
            "SELECT industry, headcount, domain FROM companies WHERE id = ?", (b,)).fetchone()
    finally:
        conn.close()
    assert row["industry"] == "Software"   # the survivor's own value wins
    assert row["headcount"] == "11-50"     # inherited: the survivor had none


def test_keep_id_outside_the_pair_is_rejected():
    candidate_id, a, b = _queued_pair()
    out = om.approve_company_merge_candidate(candidate_id, keep_id=999999)
    assert out["status"] == "error"
    assert "not part of candidate" in out["error"]
    conn = om.get_conn()
    try:
        assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (a,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM companies WHERE id = ?", (b,)).fetchone() is not None
    finally:
        conn.close()


def test_the_list_flattens_both_sides_and_reports_live_lead_counts():
    """Three generations of queueing code wrote three payload shapes. Every
    surface drawing the pair had to know all three; now none of them do. Lead
    counts come from the live tables, because which side has the leads is
    usually the whole decision and a weeks-old payload would lie about it."""
    candidate_id, a, b = _queued_pair()
    conn = om.get_conn()
    lead = om.resolve_lead(email="j@unrelated.com", name="Jane", conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (b, lead["id"]))
    conn.commit()
    conn.close()
    row = om.list_company_merge_candidates(reason="name_only_domain_attach")["candidates"][0]
    assert row["id"] == candidate_id
    assert row["existing_name"] == "Acme Services"
    assert row["candidate_domain"] == "unrelated.com"
    assert row["existing_leads"] == 0
    assert row["candidate_leads"] == 1
    assert row["confidence"]        # recomputed for legacy rows, always present


def test_the_list_pages_and_reports_the_full_total():
    conn = om.get_conn()
    for i in range(3):
        om.ensure_company(conn, name=f"Dup {i}")
        conn.commit()
        om.ensure_company(conn, name=f"Dup {i}", domain=f"d{i}.com")
        conn.commit()
    conn.close()
    first = om.list_company_merge_candidates(reason="name_only_domain_attach", limit=2)
    assert first["count"] == 2 and first["total"] == 3
    second = om.list_company_merge_candidates(
        reason="name_only_domain_attach", limit=2, offset=2)
    assert second["count"] == 1 and second["total"] == 3
    seen = {c["id"] for c in first["candidates"]} | {c["id"] for c in second["candidates"]}
    assert len(seen) == 3


def test_bulk_resolve_reports_each_outcome_separately():
    """Not all-or-nothing: each candidate is an independent judgement and a
    merge cannot be undone from here, so one stale row must not roll back the
    rest of the batch."""
    import dashboard_actions

    conn = om.get_conn()
    for i in range(2):
        om.ensure_company(conn, name=f"Bulk {i}")
        conn.commit()
        om.ensure_company(conn, name=f"Bulk {i}", domain=f"b{i}.com")
        conn.commit()
    conn.close()
    ids = [c["id"] for c in om.list_company_merge_candidates(
        reason="name_only_domain_attach")["candidates"]]
    out = dashboard_actions.resolve_merge_candidates_bulk(
        [*ids, "cmc_does_not_exist"], approve=True, keep="candidate")
    assert out["resolved"] == 2
    assert out["failed"] == 1
    assert out["errors"][0]["candidate_id"] == "cmc_does_not_exist"


def test_bulk_resolve_rejects_an_unknown_keep_side():
    import dashboard_actions

    with pytest.raises(ValueError):
        dashboard_actions.resolve_merge_candidates_bulk(["x"], approve=True, keep="whichever")
