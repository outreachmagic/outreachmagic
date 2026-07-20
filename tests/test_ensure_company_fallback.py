"""ensure_company()'s name-only fallback (Stage C4) must never silently
attach an incoming domain to an existing name-matched company row -- two
unrelated real companies can share a generic name (e.g. two different "Miles
Perret" orgs). Confirms the fix: a new row is created instead, and a
company_merge_candidates row is logged for human review."""

import json
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


def test_name_only_match_never_attaches_incoming_domain():
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="Acme Services")
    conn.commit()
    assert conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (existing_id,)
    ).fetchone()["domain"] is None

    new_id = om.ensure_company(conn, name="Acme Services", domain="unrelated-acme.com")
    conn.commit()

    assert new_id != existing_id, "must create a new row, not reuse the name-matched one"
    existing_domain = conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (existing_id,)
    ).fetchone()["domain"]
    assert existing_domain is None, "the existing row's domain must never be silently attached"
    new_domain = conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (new_id,)
    ).fetchone()["domain"]
    assert new_domain == "unrelated-acme.com"
    conn.close()


def test_name_only_match_logs_a_merge_candidate_for_review():
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="Acme Services")
    conn.commit()
    new_id = om.ensure_company(conn, name="Acme Services", domain="unrelated-acme.com")
    conn.commit()

    row = conn.execute(
        """SELECT * FROM company_merge_candidates
           WHERE existing_company_id = ? AND candidate_company_id = ?""",
        (existing_id, new_id),
    ).fetchone()
    assert row is not None
    assert row["reason"] == "name_only_domain_attach"
    assert row["status"] == "pending"
    payload = json.loads(row["payload_json"])
    assert payload["incoming_domain"] == "unrelated-acme.com"
    assert payload["existing_company_id"] == existing_id
    conn.close()


def test_domain_match_still_works_normally():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    again = om.ensure_company(conn, name="Something Else Entirely", domain="acme.com")
    assert again == cid, "an existing domain match must still win regardless of the name given"
    conn.close()


def test_name_match_different_registrable_domain_creates_candidate_medium():
    """Stage D3b: existing row already has ITS OWN domain (not the
    empty-domain case) that differs from the incoming one. Used to silently
    fall through to 'rec = existing', discarding the incoming domain with no
    record anywhere. "acme.com" and "acme-second.com" are different
    registrable domains -- could be a real subsidiary, could be a wholly
    different company -- so this must still create a new row and log a
    MEDIUM-confidence merge candidate for human review, never silently
    attach or reuse."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    again = om.ensure_company(conn, name="Acme", domain="acme-second.com")
    conn.commit()
    assert again != cid, "different registrable domain must not reuse the existing row"
    row = conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (cid,)
    ).fetchone()
    assert row["domain"] == "acme.com", "must not overwrite the existing row's domain"
    candidate = conn.execute(
        "SELECT * FROM company_merge_candidates WHERE existing_company_id = ? AND candidate_company_id = ?",
        (cid, again),
    ).fetchone()
    assert candidate is not None
    payload = json.loads(candidate["payload_json"])
    assert payload["confidence"] == "MEDIUM"


def test_name_match_same_registrable_domain_auto_tracks_identity_high():
    """Stage D3b: existing row's own domain and the incoming domain share a
    registrable domain (e.g. mail.acme.com is a subdomain of acme.com) -- a
    hard-to-spoof signal (DNS delegation implies control of the parent
    zone), unlike name-string similarity alone. Safe to record as an
    ADDITIONAL identity on the SAME row without a merge or human review."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    again = om.ensure_company(conn, name="Acme", domain="mail.acme.com")
    conn.commit()
    assert again == cid, "same registrable domain must auto-track on the existing row, no new row"
    row = conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (cid,)
    ).fetchone()
    assert row["domain"] == "acme.com", "must not overwrite the primary domain column"
    identity = conn.execute(
        """SELECT source FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain' AND identity_value_normalized = ?""",
        (cid, "mail.acme.com"),
    ).fetchone()
    assert identity is not None
    assert identity["source"] == "ensure_company_registrable_match"
    assert conn.execute("SELECT COUNT(*) AS n FROM company_merge_candidates").fetchone()["n"] == 0, (
        "same-registrable-domain auto-tracking must never require human review"
    )
    conn.close()


def test_authoritative_domain_attach_opt_in_allows_direct_attach():
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="Acme Services")
    conn.commit()
    same_id = om.ensure_company(
        conn, name="Acme Services", domain="acme.com", authoritative_domain_attach=True,
    )
    conn.commit()
    assert same_id == existing_id
    row = conn.execute("SELECT domain FROM companies WHERE id = ?", (existing_id,)).fetchone()
    assert row["domain"] == "acme.com"
    assert conn.execute("SELECT COUNT(*) AS n FROM company_merge_candidates").fetchone()["n"] == 0
    conn.close()


def test_is_non_company_name_guard_unchanged():
    conn = om.get_conn()
    first = om.ensure_company(conn, name="Self-Employed", domain="a.com")
    second = om.ensure_company(conn, name="Self-Employed", domain="b.com")
    conn.commit()
    assert first != second, "non-company placeholder names must still get their own row per person"
    conn.close()


def test_shared_email_domain_guard_unchanged():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="gmail.com")
    conn.commit()
    row = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()
    assert row["domain"] is None, "shared email domains must never be stored as a company domain"
    conn.close()
