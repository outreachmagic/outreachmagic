"""pipeline.py find-domains: workspace-level orchestration around
domain_discovery.run_company_domain_discovery -- company selection (only
undomained companies in the target workspace), tagging, and reporting."""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domain_discovery as dd  # noqa: E402
import enrich  # noqa: E402
import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _fake_result(with_email=True):
    organic = [{
        "link": "https://www.linkedin.com/company/modern-storefront",
        "title": "Modern Storefront LLC | LinkedIn",
        "snippet": "Modern Storefront LLC on LinkedIn",
    }]
    if with_email:
        organic.insert(0, {
            "link": "https://www.modernstorefront.com/contact",
            "title": "Modern Storefront LLC - Contact",
            "snippet": "Reach us at info@modernstorefront.com any time.",
        })
    return {"organic": organic, "knowledgeGraph": {}}


def _setup_workspace_lead(company_name="Modern Storefront LLC", person="Jane Doe"):
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    cid = om.ensure_company(conn, name=company_name)
    lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    ws_row = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead["id"])
    conn.commit()
    conn.close()
    return cid, lead["id"]


def test_unknown_workspace_returns_error():
    result = om.find_domains_for_workspace("does-not-exist")
    assert result["status"] == "error"


def test_finds_domain_tags_lead_and_reports_counts():
    cid, lead_id = _setup_workspace_lead()

    with mock.patch.object(enrich, "serper_search", return_value=_fake_result(with_email=True)):
        result = om.find_domains_for_workspace("storefront")

    assert result["status"] == "ok"
    assert result["found"] == 1
    assert result["companies_targeted"] == 1

    conn = om.get_conn()
    tags = {r["tag"] for r in conn.execute("SELECT tag FROM workspace_lead_tags WHERE lead_id = ?", (lead_id,))}
    assert "domain_discovered" in tags
    assert not any(t.startswith("domain_found_") for t in tags), "per-domain tags mint one tag per value"
    company = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["domain"] == "modernstorefront.com"


def test_companies_with_domain_already_set_are_not_retargeted():
    _setup_workspace_lead()

    with mock.patch.object(enrich, "serper_search", return_value=_fake_result(with_email=True)):
        om.find_domains_for_workspace("storefront")

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not re-query")) as fake:
        result = om.find_domains_for_workspace("storefront")
        fake.assert_not_called()

    assert result["companies_targeted"] == 0


def test_dry_run_lists_targets_without_calling_serper():
    _setup_workspace_lead()

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")) as fake:
        result = om.find_domains_for_workspace("storefront", dry_run=True)
        fake.assert_not_called()

    assert result["companies_targeted"] == 1
    assert result["results"][0]["status"] == "dry_run"


def test_limit_caps_companies_searched():
    _setup_workspace_lead(company_name="First Co LLC", person="Alice")
    conn = om.get_conn()
    cid2 = om.ensure_company(conn, name="Second Co LLC")
    lead2 = om.resolve_lead(name="Bob", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid2, lead2["id"]))
    ws_row = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead2["id"])
    conn.commit()
    conn.close()

    with mock.patch.object(enrich, "serper_search", return_value=_fake_result(with_email=True)):
        result = om.find_domains_for_workspace("storefront", limit=1)

    assert result["companies_targeted"] == 1


# ── Tag targeting ────────────────────────────────────────────────────────────
# Tags narrow only what gets SEARCHED. Every "do we already know this domain?"
# check stays org-wide, so searching one tag never re-pays for a company
# another tag, workspace, or campaign already resolved.

def _tagged_company(company, person, tags=()):
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    cid = om.ensure_company(conn, name=company)
    lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    ws_row = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead["id"])
    for t in tags:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, ?)""",
            (f"wlt_{ws_row['id']}_{lead['id']}_{t}", ws_row["id"], lead["id"], t))
    conn.commit()
    conn.close()
    return cid, lead["id"]


def _targets(**kw):
    """Company names find-domains would search, via the real --dry-run path."""
    res = om.find_domains_for_workspace("storefront", dry_run=True, **kw)
    return {r["company_name"] for r in res["results"]}


def test_tag_filter_targets_only_tagged_companies():
    _tagged_company("Alpha Care", "A One", tags=("assisted_living",))
    _tagged_company("Beta Realty", "B Two", tags=("apartment_mgrs",))
    _tagged_company("Gamma Co", "G Three")

    assert _targets(tags=["assisted_living"]) == {"Alpha Care"}
    assert _targets(tags=["assisted_living", "apartment_mgrs"]) == {"Alpha Care", "Beta Realty"}
    assert _targets() == {"Alpha Care", "Beta Realty", "Gamma Co"}


def test_exclude_tag_skips_the_company_entirely():
    """needs_review on ANY lead disqualifies the company -- the domain is a
    company-level fact, so a per-lead exclusion would be incoherent."""
    cid, _ = _tagged_company("Delta Group", "D One", tags=("assisted_living",))
    conn = om.get_conn()
    lead2 = om.resolve_lead(name="D Two", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead2["id"]))
    ws_row = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead2["id"])
    conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, 'needs_review')""",
        (f"wlt_{ws_row['id']}_{lead2['id']}_nr", ws_row["id"], lead2["id"]))
    conn.commit()
    conn.close()
    _tagged_company("Epsilon Ltd", "E One", tags=("assisted_living",))

    assert _targets(tags=["assisted_living"]) == {"Delta Group", "Epsilon Ltd"}
    assert _targets(tags=["assisted_living"], exclude_tags=["needs_review"]) == {"Epsilon Ltd"}


def test_already_resolved_company_is_skipped_regardless_of_tag():
    """The 'already known' check is org-wide: a domain found under any tag,
    workspace, or campaign drops the company from every future target list."""
    cid, _ = _tagged_company("Zeta Health", "Z One", tags=("assisted_living",))
    assert _targets(tags=["assisted_living"]) == {"Zeta Health"}

    conn = om.get_conn()
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("zetahealth.com", cid))
    conn.commit()
    conn.close()
    assert _targets(tags=["assisted_living"]) == set()


def test_rep_lead_for_a_tagged_run_is_itself_tagged():
    """The observation must land on a lead the campaign cares about, not on
    whichever untagged lead happens to have the lowest id."""
    cid, _ = _tagged_company("Theta Co", "Untagged First")
    conn = om.get_conn()
    tagged = om.resolve_lead(name="Tagged Second", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, tagged["id"]))
    ws_row = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], tagged["id"])
    conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, 'assisted_living')""",
        (f"wlt_{ws_row['id']}_{tagged['id']}_al", ws_row["id"], tagged["id"]))
    conn.commit()
    conn.close()

    with mock.patch.object(enrich, "serper_search", return_value=_fake_result()):
        res = om.find_domains_for_workspace("storefront", tags=["assisted_living"])
    assert res["companies_targeted"] == 1

    conn = om.get_conn()
    obs_lead = conn.execute(
        "SELECT lead_id FROM lead_provider_observations WHERE kind = 'domain_lookup'").fetchone()
    assert obs_lead["lead_id"] == tagged["id"]


def test_summary_reports_the_tag_filter_used():
    _tagged_company("Eta Co", "E Two", tags=("assisted_living",))
    res = om.find_domains_for_workspace(
        "storefront", dry_run=True, tags=["assisted_living"], exclude_tags=["needs_review"])
    assert res["tags"] == ["assisted_living"]
    assert res["exclude_tags"] == ["needs_review"]


def test_retry_unresolved_reworks_only_the_companies_without_a_domain():
    """After a scoring change you want the held companies re-evaluated, but
    not the resolved ones re-targeted -- that is what --force does, and it
    would re-spend a credit on every company that already succeeded."""
    resolved_cid, _ = _tagged_company("Resolved Co", "R One", tags=("seg",))
    held_cid, _ = _tagged_company("Held Co", "H One", tags=("seg",))
    conn = om.get_conn()
    conn.execute("UPDATE companies SET domain = 'resolvedco.com' WHERE id = ?", (resolved_cid,))
    conn.commit()
    conn.close()

    # A prior search of the held company that resolved nothing -- normally
    # cached for 30 days, which is exactly what has to be bypassed here.
    with mock.patch.object(enrich, "serper_search",
                           return_value={"organic": [], "knowledgeGraph": {}}):
        om.find_domains_for_workspace("storefront", tags=["seg"])

    # Age it past the short retry window. --retry-unresolved deliberately still
    # honours that window so a killed-and-restarted run does not re-spend on
    # companies it searched minutes ago; the 30-day cache is what it bypasses.
    conn = om.get_conn()
    conn.execute(
        "UPDATE lead_provider_observations SET observed_at = datetime('now', ?) "
        "WHERE kind = 'domain_lookup'",
        (f"-{dd.RETRY_FRESHNESS_HOURS + 2} hours",))
    conn.commit()
    conn.close()

    searched = []
    def fake(query, config):
        searched.append(query)
        return {"organic": [], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=fake):
        res = om.find_domains_for_workspace("storefront", tags=["seg"], retry_unresolved=True)

    assert res["companies_targeted"] == 1          # the resolved one is not re-targeted
    assert res["results"][0]["company_name"] == "Held Co"
    assert searched, "the 30-day cache must be bypassed for the unresolved company"


def test_plain_rerun_still_respects_the_cache():
    _tagged_company("Cached Co", "C One", tags=("seg2",))
    with mock.patch.object(enrich, "serper_search",
                           return_value={"organic": [], "knowledgeGraph": {}}):
        om.find_domains_for_workspace("storefront", tags=["seg2"])

    with mock.patch.object(enrich, "serper_search",
                           side_effect=AssertionError("must not re-query")) as fake:
        res = om.find_domains_for_workspace("storefront", tags=["seg2"])
        fake.assert_not_called()
    assert res["cached"] == 1


# ── CLI wiring ───────────────────────────────────────────────────────────────
# Every flag above was tested against find_domains_for_workspace() directly,
# which cannot catch a flag wired into the wrong command's dispatch --
# --retry-unresolved was injected into the `login` call, so find-domains
# silently ignored it AND `pipeline.py login` would have raised TypeError.

@pytest.mark.parametrize("argv, kwarg, expected", [
    (["--retry-unresolved"], "retry_unresolved", True),
    (["--force"], "force", True),
    (["--debug"], "debug", True),
    (["--dry-run"], "dry_run", True),
    (["--max-queries", "42"], "max_queries", 42),
    (["--limit", "7"], "limit", 7),
    (["--tag", "a", "b"], "tags", ["a", "b"]),
    (["--exclude-tag", "z"], "exclude_tags", ["z"]),
])
def test_cli_flags_reach_find_domains_for_workspace(argv, kwarg, expected, monkeypatch):
    import pipeline_cli

    seen = {}
    def fake(workspace, **kw):
        seen.update(kw)
        return {"status": "ok", "results": []}

    # main() does `import pipeline as _pipeline`, so the alias IS this module
    # object -- patching the attribute here is what the dispatch will see.
    monkeypatch.setattr(om, "find_domains_for_workspace", fake)
    monkeypatch.setattr(
        sys, "argv", ["pipeline.py", "find-domains", "--workspace", "storefront", *argv])
    pipeline_cli.main()
    assert seen.get(kwarg) == expected, f"{argv} did not reach the function as {kwarg}"


def test_login_dispatch_takes_no_find_domains_flags():
    """Guards the specific mistake: a find-domains kwarg pasted into the login
    call, which argparse cannot catch and no find-domains test would see."""
    import inspect
    import pipeline_cli

    src = inspect.getsource(pipeline_cli.main)
    login_call = src[src.index('if args.command == "login"'):]
    login_call = login_call[:login_call.index("return")]
    assert "retry_unresolved" not in login_call
