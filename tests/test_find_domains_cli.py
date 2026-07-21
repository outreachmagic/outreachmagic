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
    assert "domain_found_modernstorefront.com" in tags
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
