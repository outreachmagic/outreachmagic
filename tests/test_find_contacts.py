"""find-contacts: company selection, the budget cap, and the dry-run estimate.

The property that matters most here is that `--dry-run` is trustworthy. It has
to spend nothing, and it has to estimate from *cache misses* rather than target
count -- an estimate that over-reports on the second run is a cap nobody
believes, and a cap nobody believes is a cap nobody uses.

Nothing in this file reaches the network: the fetch is patched, and the
selection/dry-run tests assert that it was never called.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contact_discovery as cd  # noqa: E402
import contact_icp  # noqa: E402
import page_cache as pc  # noqa: E402
import pipeline as om  # noqa: E402

STAFF_PAGE = """
## Meet Our Team

### Dana Whitfield

#### General Manager

### Ines Fournier

#### Sales Consultant
"""


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()
    om.create_workspace("Storefront", slug="storefront")
    contact_icp.cli_set("storefront", "ops",
                        whitelist="general manager,service director",
                        blocklist="sales consultant")


def _ws_id(slug="storefront"):
    conn = om.get_conn()
    try:
        return om.resolve_workspace_identity(conn, slug)["id"]
    finally:
        conn.close()


def _company_in_workspace(name, domain, *, titled=False, tags=(), slug="storefront"):
    """A company reachable from the workspace via one lead."""
    conn = om.get_conn()
    try:
        cid = om.ensure_company(conn, name=name, domain=domain)
        lead = om.resolve_lead(
            name=f"Seed {name}", company=name,
            title="General Manager" if titled else None,
            allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
        ws = om.resolve_workspace_identity(conn, slug)
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag) "
                "VALUES (?, ?, ?, ?)",
                (f"t_{ws['id']}_{lead['id']}_{tag}", ws["id"], lead["id"], tag))
        conn.commit()
        return cid
    finally:
        conn.close()


def _cache(cid, url, markdown=STAFF_PAGE):
    conn = om.get_conn()
    try:
        pc.store_page(conn, url, markdown, company_id=cid, http_status=200)
        conn.commit()
    finally:
        conn.close()


def _fake_pool(keys=("test-key",)):
    def api_key_pool(_env_key):
        return list(keys)

    def call_with_key_pool(_env_key, fn, *, provider):  # noqa: ARG001
        return fn(keys[0])

    return (api_key_pool, call_with_key_pool, None)


def _run(**kw):
    """find-contacts with the network sealed off."""
    with mock.patch("shared.require_api_key_pool", return_value=_fake_pool()), \
            mock.patch.object(pc, "_firecrawl_post", return_value=(STAFF_PAGE, 200)) as post, \
            mock.patch("enrich.serper_search", return_value={"organic": []}) as serper:
        result = om.find_contacts_for_workspace("storefront", **kw)
    return result, post, serper


# ── company selection ────────────────────────────────────────────────────────

def test_only_companies_without_a_contact_are_targeted():
    _company_in_workspace("Needs Contacts", "needs.test", titled=False)
    _company_in_workspace("Already Done", "done.test", titled=True)
    result, _post, _s = _run(dry_run=True)
    assert result["companies_targeted"] == 1
    assert result["results"][0]["domain"] == "needs.test"


def test_force_includes_companies_that_already_have_contacts():
    _company_in_workspace("Needs Contacts", "needs.test", titled=False)
    _company_in_workspace("Already Done", "done.test", titled=True)
    result, _post, _s = _run(dry_run=True, force=True)
    assert result["companies_targeted"] == 2


def test_a_company_with_no_domain_is_not_targeted():
    """There is nothing to match a staff URL against."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="No Domain Co")
    lead = om.resolve_lead(name="Seed", company="No Domain Co",
                           allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, _ws_id(), lead["id"])
    conn.commit()
    conn.close()
    result, _post, _s = _run(dry_run=True)
    assert result["companies_targeted"] == 0


def test_tags_narrow_the_target_set():
    _company_in_workspace("Tagged", "tagged.test", tags=("campaign-a",))
    _company_in_workspace("Untagged", "untagged.test")
    result, _post, _s = _run(dry_run=True, tags=["campaign-a"])
    assert [r["domain"] for r in result["results"]] == ["tagged.test"]


def test_exclude_tags_drop_a_company():
    _company_in_workspace("Fine", "fine.test")
    _company_in_workspace("Excluded", "excluded.test", tags=("needs_review",))
    result, _post, _s = _run(dry_run=True, exclude_tags=["needs_review"])
    assert [r["domain"] for r in result["results"]] == ["fine.test"]


def test_limit_caps_the_run():
    for i in range(4):
        _company_in_workspace(f"Company {i}", f"c{i}.test")
    result, _post, _s = _run(dry_run=True, limit=2)
    assert result["companies_targeted"] == 2


def test_an_unknown_workspace_is_an_error():
    assert om.find_contacts_for_workspace("nope")["status"] == "error"


def test_a_missing_named_icp_is_an_error():
    result = om.find_contacts_for_workspace("storefront", icp_name="absent", dry_run=True)
    assert result["status"] == "error"


# ── the dry run ──────────────────────────────────────────────────────────────

def test_dry_run_spends_nothing():
    _company_in_workspace("Needs Contacts", "needs.test")
    result, post, serper = _run(dry_run=True)
    assert post.call_count == 0, "a dry run must not fetch"
    assert serper.call_count == 0, "a dry run must not search either"
    assert result["dry_run"] is True


def test_dry_run_reports_targets_and_worst_case_credits():
    for i in range(3):
        _company_in_workspace(f"Company {i}", f"c{i}.test")
    result, _post, _s = _run(dry_run=True)
    assert result["companies_targeted"] == 3
    assert result["needs_url_discovery"] == 3
    assert result["firecrawl_credits_worst_case"] == 3
    assert result["serper_queries_worst_case"] == 3


def test_the_estimate_counts_cache_misses_not_targets():
    """The second run of the same workspace must not re-report the full cost."""
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    _company_in_workspace("Fresh Co", "fresh.test")

    result, _post, _s = _run(dry_run=True)
    assert result["companies_targeted"] == 2
    assert result["already_cached"] == 1
    assert result["firecrawl_credits_worst_case"] == 1, \
        "the cached company is free and must not be counted"


def test_max_fetches_caps_the_estimate():
    for i in range(5):
        _company_in_workspace(f"Company {i}", f"c{i}.test")
    result, _post, _s = _run(dry_run=True, max_fetches=2)
    assert result["firecrawl_credits_worst_case"] == 2


def test_dry_run_names_the_icp_version_it_would_use():
    _company_in_workspace("Needs Contacts", "needs.test")
    result, _post, _s = _run(dry_run=True)
    assert result["icp"] == "ops"
    assert result["icp_config_hash"] == contact_icp.cli_show("storefront", "ops")["config_hash"]


# ── the real run ─────────────────────────────────────────────────────────────

def test_a_cached_page_is_extracted_without_spending_a_credit():
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    result, post, _s = _run()
    assert post.call_count == 0
    assert result["firecrawl_credits_spent"] == 0
    assert result["contacts_attached"] == 1, "the GM is kept; the sales consultant is blocked"


def test_the_icp_decides_who_is_attached():
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    _run()
    conn = om.get_conn()
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM leads WHERE company_id = ?", (cid,)).fetchall()}
        assert "Dana Whitfield" in names
        assert "Ines Fournier" not in names, "blocklisted title must not be attached"
    finally:
        conn.close()


def test_max_fetches_stops_the_run_cleanly():
    for i in range(3):
        cid = _company_in_workspace(f"Company {i}", f"c{i}.test")
        _cache(cid, f"https://c{i}.test/staff", markdown="## Nothing here\n")
    # Every page is cached, so nothing is billed and the cap never trips.
    result, _post, _s = _run(max_fetches=0)
    assert result["firecrawl_credits_spent"] == 0
    assert "stopped_reason" not in result, \
        "an exhausted budget must not block work that costs nothing"


def test_a_company_with_no_discoverable_url_is_recorded_not_skipped():
    _company_in_workspace("Invisible Co", "invisible.test")
    result, _post, _s = _run()
    assert result["outcomes"].get("no_staff_url") == 1
    conn = om.get_conn()
    try:
        obs = conn.execute(
            "SELECT outcome FROM company_contact_observations").fetchone()
        assert obs["outcome"] == "no_staff_url", "every path records an observation"
    finally:
        conn.close()


def test_one_observation_per_company_per_run():
    """apply_company_contacts writes its own observation for the agent path;
    the orchestrator must not let both fire and double every count."""
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    _run()
    conn = om.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM company_contact_observations WHERE company_id = ?",
            (cid,)).fetchone()["c"] == 1
    finally:
        conn.close()


def test_the_run_is_idempotent():
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    _run()
    _run(force=True)
    conn = om.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE name = 'Dana Whitfield'").fetchone()["c"] == 1
    finally:
        conn.close()


def test_reparse_re_extracts_without_fetching():
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    contact_icp.cli_set("storefront", "ops", whitelist="", blocklist="")
    result, post, serper = _run(reparse=True)
    assert post.call_count == 0 and serper.call_count == 0
    assert result["firecrawl_credits_spent"] == 0
    assert result["contacts_attached"] == 2, "a widened ICP now keeps both, for free"


def test_reparse_on_a_company_with_no_cached_page_says_so():
    _company_in_workspace("Uncached Co", "uncached.test")
    result, post, _s = _run(reparse=True)
    assert post.call_count == 0
    assert result["outcomes"].get("no_cached_page") == 1


# ── staff URL discovery ──────────────────────────────────────────────────────

def test_a_cached_page_is_the_first_place_a_url_comes_from():
    cid = _company_in_workspace("Cached Co", "cached.test")
    _cache(cid, "https://cached.test/staff")
    conn = om.get_conn()
    try:
        url, source = cd.staff_url_from_local_evidence(conn, cid, "cached.test")
        assert url == "https://cached.test/staff"
        assert source == "page_cache"
    finally:
        conn.close()


def test_a_previous_find_domains_run_supplies_the_url_for_free():
    """find-domains already stored the top organic links; the Serper credit for
    this company has effectively been paid once already."""
    import json

    cid = _company_in_workspace("Evidence Co", "evidence.test")
    conn = om.get_conn()
    try:
        lead_id = conn.execute(
            "SELECT id FROM leads WHERE company_id = ?", (cid,)).fetchone()["id"]
        from provider_observations import record_observation
        record_observation(
            conn, lead_id, kind="domain_lookup", origin="attempt", provider="serper",
            status="found",
            metadata_json=json.dumps({"top_links": [
                "https://directory.test/evidence-co",
                "https://evidence.test/about-us/staff/",
            ]}))
        conn.commit()
        url, source = cd.staff_url_from_local_evidence(conn, cid, "evidence.test")
        assert url == "https://evidence.test/about-us/staff/"
        assert source == "domain_observation"
    finally:
        conn.close()


def test_an_off_site_link_is_never_chosen():
    """A directory profile is not a staff page, and paying to fetch one is how
    a contact list fills up with other people's data."""
    assert cd._rank_links(
        ["https://dealerrater.test/dealer/acme/staff"], "acme.test") is None


@pytest.mark.parametrize("path", [
    "/staff", "/about-us/staff/", "/dealership/staff.htm", "/staff.aspx",
    "/meet-our-team/", "/our-team",
])
def test_staff_shaped_paths_are_recognised(path):
    assert cd._rank_links([f"https://acme.test{path}"], "acme.test") == \
        f"https://acme.test{path}"


def test_a_serper_result_on_the_right_site_is_used_even_without_a_staff_path():
    """Several dealer CMSs put the roster on /about."""
    _company_in_workspace("About Co", "about.test")
    with mock.patch("shared.require_api_key_pool", return_value=_fake_pool()), \
            mock.patch.object(pc, "_firecrawl_post", return_value=(STAFF_PAGE, 200)), \
            mock.patch("enrich.serper_search",
                       return_value={"organic": [{"link": "https://about.test/about"}]}):
        result = om.find_contacts_for_workspace("storefront")
    assert result["contacts_attached"] == 1
    assert result["serper_queries_spent"] == 1


def test_the_staff_query_carries_no_search_operators():
    """Free Serper accounts reject `site:` outright with an HTTP 400."""
    query = cd.build_staff_query("Acme Motors, Inc.")
    assert ":" not in query and '"' not in query
    assert query == "Acme Motors staff"
