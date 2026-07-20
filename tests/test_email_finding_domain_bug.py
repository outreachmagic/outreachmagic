"""debug-email-finding-domain-bug.md: TryKitt/Icypeas provider attempts were
recording the literal string "email_finding" as the domain -- a category
label (PROVIDER_DOMAINS, used only to derive `kind`) that leaked into the
actual domain column via `domain = domain or PROVIDER_DOMAINS.get(provider)`.
Traced end to end: the real domain was never even threaded through
build_import_profile() -> apply_email_find_results() -> record_provider_attempt()
in the first place, so every attempt via batch-find got the fake placeholder
regardless of whether the lead had a real domain. Worse, the skip-before-retry
logic (skip_reason_from_lookup) didn't consult domain at all, so a lead that
was never actually searched (no domain at the time) would never be retried
once a real domain became available (e.g. from a later Serper domain_lookup).

Three-part fix, tested here:
1. record_provider_attempt() no longer writes the category placeholder into
   the domain column -- only uses it to compute `kind`.
2. build_import_profile()/apply_email_find_results() now thread the real
   domain (or None) through end to end.
3. skip_reason_from_lookup() only treats a "not_found" attempt as
   retry-blocking when it was a genuine domain search.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from batch_runner import build_import_profile, skip_reason_from_lookup  # noqa: E402
from pipeline_provider_attempts import get_provider_attempts_for_lead, record_provider_attempt  # noqa: E402
import normalize as norm  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def test_record_provider_attempt_never_writes_the_category_placeholder():
    conn = om.get_conn()
    lead = om.resolve_lead(name="No Domain Lead", source="csv", allow_weak_identity=True, conn=conn)
    conn.commit()
    record_provider_attempt(conn, lead["id"], "trykitt", status="not_found", domain=None)
    conn.commit()

    attempts = get_provider_attempts_for_lead(conn, lead["id"])
    assert len(attempts) == 1
    assert attempts[0]["domain"] is None, (
        "domain must stay NULL when no real domain was searched, never the "
        "'email_finding' category placeholder"
    )
    conn.close()


def test_record_provider_attempt_stores_a_real_domain_when_given():
    conn = om.get_conn()
    lead = om.resolve_lead(name="Real Domain Lead", source="csv", allow_weak_identity=True, conn=conn)
    conn.commit()
    record_provider_attempt(conn, lead["id"], "trykitt", status="not_found", domain="acme.org")
    conn.commit()

    attempts = get_provider_attempts_for_lead(conn, lead["id"])
    assert attempts[0]["domain"] == "acme.org"
    conn.close()


def test_build_import_profile_threads_domain_into_provider_attempts():
    profile = build_import_profile(
        full_name="Jane", company="Acme", domain="acme.com", linkedin="",
        find_result={"status": "not_found", "provider": "trykitt"},
        normalize_linkedin_fn=norm.normalize_linkedin,
    )
    assert profile["_provider_attempts"][0]["domain"] == "acme.com"


def test_build_import_profile_domain_none_when_lead_has_none():
    profile = build_import_profile(
        full_name="Jane", company="Acme", domain="", linkedin="",
        find_result={"status": "not_found", "provider": "trykitt"},
        normalize_linkedin_fn=norm.normalize_linkedin,
    )
    assert profile["_provider_attempts"][0]["domain"] is None


def test_skip_reason_blocks_retry_only_for_genuine_domain_search():
    lookup_no_domain = {
        "status": "found",
        "provider_attempts": [{"provider": "trykitt", "status": "not_found", "domain": None}],
    }
    assert skip_reason_from_lookup(lookup_no_domain, ["trykitt"]) is None, (
        "a prior attempt with no domain never actually searched anything -- "
        "must not block retry once a domain is available"
    )

    lookup_with_domain = {
        "status": "found",
        "provider_attempts": [{"provider": "trykitt", "status": "not_found", "domain": "acme.com"}],
    }
    assert skip_reason_from_lookup(lookup_with_domain, ["trykitt"]) == "trykitt_attempted"


def test_skip_reason_found_status_always_blocks_regardless_of_domain():
    lookup = {
        "status": "found",
        "provider_attempts": [{"provider": "trykitt", "status": "found", "domain": None}],
    }
    assert skip_reason_from_lookup(lookup, ["trykitt"]) == "trykitt_attempted"


def test_skip_reason_error_status_never_blocks_retry():
    lookup = {
        "status": "found",
        "provider_attempts": [{"provider": "trykitt", "status": "error", "domain": "acme.com"}],
    }
    assert skip_reason_from_lookup(lookup, ["trykitt"]) is None


def test_batch_lead_lookup_returns_ranked_company_domains():
    """Stage D5: batch_lead_lookup() now surfaces every known domain for a
    lead's company (ranked via rank_company_domains()), not just the single
    primary companies.domain -- so batch-find can waterfall across
    candidates instead of only ever trying one."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email')",
        (cid,),
    )
    lead = om.resolve_lead(name="Jane", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    conn.close()

    result = om.batch_lead_lookup([{"index": 0, "lead_id": lead["id"]}])
    entry = result["results"][0]
    assert entry["status"] == "found"
    assert entry["company_domain"] == "acme.com"
    assert entry["company_domains"][0] == "mail.acme.com", "role='email' domain must rank first"
    assert set(entry["company_domains"]) == {"acme.com", "mail.acme.com"}


def test_batch_lead_lookup_single_domain_fallback():
    """A company with no company_identities rows at all falls back to a
    single-element list from the legacy companies.domain column."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    lead = om.resolve_lead(name="Jane", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    conn.close()

    result = om.batch_lead_lookup([{"index": 0, "lead_id": lead["id"]}])
    entry = result["results"][0]
    assert entry["company_domains"] == ["acme.com"]
