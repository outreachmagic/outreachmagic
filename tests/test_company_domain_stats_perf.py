"""company_domain_email_stats must not scale with unrelated observations.

It read the lead_provider_attempts compat VIEW, which is
ROW_NUMBER() OVER (PARTITION BY lead_id, provider ...) across the whole
observations table. SQLite cannot push an outer WHERE into a window function,
so every call ranked and sorted all 39k production rows to keep one company's.

At 130ms a call that made batch-lead-lookup O(leads x observations): 232 leads
took 32.6s, blowing batch-find's 58s dedup timeout. Dedup then silently
disabled itself and the run paid a provider for leads already resolved -- a
performance bug that cost real money.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from provider_observations import KIND_EMAIL_FIND, ORIGIN_ATTEMPT, record_observation  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _lead(conn, company_id, name):
    lead = om.resolve_lead(name=name, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (company_id, lead["id"]))
    return lead["id"]


def _attempt(conn, lead_id, domain, status, provider="trykitt", at=None):
    record_observation(
        conn, lead_id, kind=KIND_EMAIL_FIND, origin=ORIGIN_ATTEMPT,
        provider=provider, status=status, domain=domain, observed_at=at)


def test_counts_found_and_attempted_per_domain():
    """One row per (lead, provider) -- the compat view's semantics, which this
    query inlines. Two domains tried for the SAME lead and provider are two
    attempts at one thing, so only the latest counts; separate leads count
    separately."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    a, b, c = (_lead(conn, cid, "A One"), _lead(conn, cid, "B Two"),
               _lead(conn, cid, "C Three"))
    _attempt(conn, a, "acme.com", "found")
    _attempt(conn, b, "acme.com", "not_found")
    _attempt(conn, c, "mail.acme.com", "found")
    conn.commit()

    stats = om.company_domain_email_stats(conn, cid)
    assert stats["acme.com"] == {"found": 1, "attempted": 2}
    assert stats["mail.acme.com"] == {"found": 1, "attempted": 1}
    conn.close()


def test_a_second_domain_for_one_lead_supersedes_the_first():
    """Documents the semantics the line above depends on: the waterfall trying
    acme.com then mail.acme.com for one person is one attempt at that person,
    not two -- so the domain it settled on is the one that counts."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    b = _lead(conn, cid, "B Two")
    _attempt(conn, b, "acme.com", "not_found", at="2026-01-01 00:00:00")
    _attempt(conn, b, "mail.acme.com", "found", at="2026-06-01 00:00:00")
    conn.commit()

    stats = om.company_domain_email_stats(conn, cid)
    assert stats == {"mail.acme.com": {"found": 1, "attempted": 1}}
    conn.close()


def test_only_the_latest_attempt_per_lead_and_provider_counts():
    """The compat view's semantics, which this query inlines: a re-attempt
    supersedes the earlier one rather than counting twice."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    a = _lead(conn, cid, "A One")
    _attempt(conn, a, "acme.com", "not_found", at="2026-01-01 00:00:00")
    _attempt(conn, a, "acme.com", "found", at="2026-06-01 00:00:00")
    conn.commit()

    assert om.company_domain_email_stats(conn, cid)["acme.com"] == {"found": 1, "attempted": 1}
    conn.close()


def test_a_newest_attempt_without_a_domain_does_not_resurrect_an_older_one():
    """rn = 1 and the domain filter must stay OUTSIDE the window. Filtering
    domain first would rank the older attempt to the top and report a domain
    the latest attempt did not use."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme")
    a = _lead(conn, cid, "A One")
    _attempt(conn, a, "acme.com", "found", at="2026-01-01 00:00:00")
    _attempt(conn, a, None, "not_found", at="2026-06-01 00:00:00")
    conn.commit()

    assert om.company_domain_email_stats(conn, cid) == {}
    conn.close()


def test_other_companies_observations_are_excluded():
    conn = om.get_conn()
    mine = om.ensure_company(conn, name="Mine")
    theirs = om.ensure_company(conn, name="Theirs")
    _attempt(conn, _lead(conn, mine, "M One"), "mine.com", "found")
    _attempt(conn, _lead(conn, theirs, "T One"), "theirs.com", "found")
    conn.commit()

    assert set(om.company_domain_email_stats(conn, mine)) == {"mine.com"}
    conn.close()


def test_cost_does_not_scale_with_unrelated_observations():
    """The regression itself. One company's stats must cost the same whether
    the table holds 20 other observations or 2,000."""
    conn = om.get_conn()
    target = om.ensure_company(conn, name="Target")
    _attempt(conn, _lead(conn, target, "T One"), "target.com", "found")
    conn.commit()

    def timed(n=60):
        start = time.perf_counter()
        for _ in range(n):
            om.company_domain_email_stats(conn, target)
        return time.perf_counter() - start

    baseline = timed()

    noise = om.ensure_company(conn, name="Noise")
    for i in range(400):
        lead = _lead(conn, noise, f"N {i}")
        for p in ("trykitt", "icypeas", "millionverifier"):
            _attempt(conn, lead, f"noise{i}.com", "not_found", provider=p)
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) c FROM lead_provider_observations").fetchone()["c"] > 1000

    loaded = timed()
    conn.close()
    # Against the view this was strictly linear in table size. Allowing 5x
    # absorbs timing noise on a busy machine while still failing loudly if the
    # window function is ever ranking the whole table again.
    assert loaded < max(baseline * 5, 0.05), (
        f"{baseline:.4f}s with a near-empty table vs {loaded:.4f}s with 1200+ "
        f"observations -- cost is scaling with unrelated rows again")
