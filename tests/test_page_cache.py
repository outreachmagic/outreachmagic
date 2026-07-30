"""The page cache, and the credit arithmetic that depends on it.

Firecrawl bills per page and its credits do not roll over, so every property
here is a money property: a cache hit must not call out, a URL that differs
only by tracking noise must not be fetched twice, and `--dry-run` must estimate
from cache misses rather than from the target count.

No test in this file is allowed to reach the network. `_firecrawl_post` is
patched everywhere; the one test that does not patch it asserts that a cache
hit never calls it at all.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import page_cache as pc  # noqa: E402
import pipeline as om  # noqa: E402

PAGE = "## Meet Our Team\n\n### Dana Whitfield\n\n#### General Manager\n"
URL = "https://example.test/about-us/staff/"


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _conn():
    return om.get_conn()


def _company(name="Acme Motors", domain="example.test"):
    conn = _conn()
    try:
        cid = om.ensure_company(conn, name=name, domain=domain)
        conn.commit()
        return cid
    finally:
        conn.close()


# ── URL normalization ────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", [
    "https://example.test/staff?srsltid=AfmBOoqVOAEhYOKUXhE",
    "https://example.test/staff#main",
    "https://EXAMPLE.test/staff",
    "https://example.test/staff?utm_source=x&gclid=y",
    "  https://example.test/staff  ",
])
def test_tracking_noise_does_not_mint_a_second_cache_key(variant):
    """A real Serper result carried ?srsltid= during the Phase 0 run. Keyed
    naively, that page would be fetched and billed a second time."""
    assert pc.url_hash(variant) == pc.url_hash("https://example.test/staff")


def test_a_meaningful_query_parameter_is_kept():
    assert pc.url_hash("https://example.test/staff?dept=sales") != \
        pc.url_hash("https://example.test/staff")


def test_different_paths_are_different_pages():
    assert pc.url_hash("https://example.test/staff") != \
        pc.url_hash("https://example.test/team")


def test_a_malformed_url_still_hashes_rather_than_crashing():
    assert pc.url_hash("not a url")


# ── fetch and cache ──────────────────────────────────────────────────────────

CONFIG = {"firecrawl_endpoint": "https://api.firecrawl.dev/v1/scrape"}


def _fake_pool(keys=("test-key",)):
    """Stand in for shared.require_api_key_pool: a pool with one usable key.

    The tests must not depend on a real FIRECRAWL_API_KEY being configured on
    the machine running them, and must never reach the network.
    """
    def api_key_pool(_env_key):
        return list(keys)

    def call_with_key_pool(_env_key, fn, *, provider):  # noqa: ARG001
        return fn(keys[0])

    return (api_key_pool, call_with_key_pool, None)


def _patched_fetch(conn, url=URL, *, markdown=PAGE, status=200, **kw):
    with mock.patch("shared.require_api_key_pool", return_value=_fake_pool()), \
            mock.patch.object(pc, "_firecrawl_post", return_value=(markdown, status)) as post:
        result = pc.fetch_page(conn, url, config=CONFIG, **kw)
    return result, post


def test_a_miss_fetches_stores_and_costs_one_credit():
    cid = _company()
    conn = _conn()
    try:
        fetch, post = _patched_fetch(conn, company_id=cid)
        conn.commit()
        assert post.call_count == 1
        assert fetch.cache_hit is False
        assert fetch.credits == 1
        assert fetch.markdown == PAGE
        row = pc.cached_page(conn, URL)
        assert row["char_count"] == len(PAGE)
        assert row["company_id"] == cid
        assert row["content_hash"] == fetch.content_hash
    finally:
        conn.close()


def test_a_hit_costs_nothing_and_makes_no_call():
    """Not patched on purpose -- if this path called out, the test would try to
    reach the network and fail."""
    conn = _conn()
    try:
        _patched_fetch(conn)
        conn.commit()
        again = pc.fetch_page(conn, URL, config=CONFIG)
        assert again.cache_hit is True
        assert again.credits == 0
        assert again.markdown == PAGE
    finally:
        conn.close()


def test_the_same_page_under_a_tracking_url_is_a_hit():
    conn = _conn()
    try:
        _patched_fetch(conn, "https://example.test/staff")
        conn.commit()
        hit = pc.fetch_page(conn, "https://example.test/staff?srsltid=abc123", config=CONFIG)
        assert hit.cache_hit is True
    finally:
        conn.close()


def test_force_refetches_and_replaces_in_place():
    conn = _conn()
    try:
        _patched_fetch(conn)
        conn.commit()
        fresh, post = _patched_fetch(conn, markdown=PAGE + "\n### Marco Bell\n\nService Director\n", force=True)
        conn.commit()
        assert post.call_count == 1
        assert fresh.cache_hit is False
        rows = conn.execute("SELECT COUNT(*) c FROM company_page_cache").fetchone()["c"]
        assert rows == 1, "a re-fetch updates the row rather than accumulating a second"
        assert "Marco Bell" in pc.cached_page(conn, URL)["markdown"]
    finally:
        conn.close()


def test_nothing_expires_on_its_own():
    """A cache that silently invalidates itself silently re-spends."""
    conn = _conn()
    try:
        _patched_fetch(conn)
        conn.execute("UPDATE company_page_cache SET fetched_at = datetime('now', '-400 days')")
        conn.commit()
        assert pc.cached_page(conn, URL) is not None
        assert pc.cached_page(conn, URL, max_age_days=30) is None, "TTL still available when asked for"
    finally:
        conn.close()


def test_a_failed_fetch_returns_an_error_rather_than_raising():
    """This runs across hundreds of companies; one bad response costs one."""
    conn = _conn()
    try:
        with mock.patch("shared.require_api_key_pool", return_value=_fake_pool()), \
                mock.patch.object(pc, "_firecrawl_post",
                                  side_effect=ValueError("Firecrawl HTTP 500 for 'x': boom")):
            fetch = pc.fetch_page(conn, URL, config=CONFIG)
        assert fetch.ok is False
        assert fetch.credits == 0, "a hard error is not billed"
        assert "HTTP 500" in fetch.error
        assert pc.cached_page(conn, URL) is None, "a failure must not poison the cache"
    finally:
        conn.close()


def test_a_missing_key_is_reported_not_raised():
    conn = _conn()
    try:
        with mock.patch("shared.require_api_key_pool", return_value=_fake_pool(keys=())):
            fetch = pc.fetch_page(conn, URL, config=CONFIG)
        assert fetch.ok is False
        assert "FIRECRAWL_API_KEY" in fetch.error
    finally:
        conn.close()


def test_an_empty_url_is_not_a_fetch():
    conn = _conn()
    try:
        assert pc.fetch_page(conn, "").credits == 0
    finally:
        conn.close()


def test_the_error_message_carries_the_code_the_pool_fails_over_on():
    """call_with_key_pool decides to try the next key by matching `HTTP <code>`
    in the message. Reword this and rotation silently stops working."""
    import re
    import api_key_pool

    msg = "Firecrawl HTTP 429 for 'https://x.test': rate limited"
    assert api_key_pool._VALUE_ERROR_FAILOVER_RE.search(msg)


# ── the credit estimate ──────────────────────────────────────────────────────

def test_cache_misses_counts_only_what_would_be_billed():
    conn = _conn()
    try:
        _patched_fetch(conn, "https://example.test/staff")
        conn.commit()
        misses = pc.cache_misses(conn, [
            "https://example.test/staff",
            "https://example.test/staff?srsltid=x",
            "https://other.test/staff",
        ])
        assert misses == ["https://other.test/staff"]
    finally:
        conn.close()


def test_cache_misses_deduplicates_within_the_request():
    conn = _conn()
    try:
        assert pc.cache_misses(conn, [
            "https://a.test/staff", "https://a.test/staff#x", "https://a.test/staff",
        ]) == ["https://a.test/staff"]
    finally:
        conn.close()


def test_cache_misses_ignores_blanks():
    conn = _conn()
    try:
        assert pc.cache_misses(conn, ["", None]) == []
    finally:
        conn.close()
