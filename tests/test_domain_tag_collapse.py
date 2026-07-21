"""domain_found_<domain> tags mint one tag per unique value.

On a real workspace that produced 288 of 340 unique tags, 218 of them used by
exactly one lead -- burying the handful of tags that describe an actual
segment. It was also a denormalized copy of companies.domain that nothing kept
in step: tags reading domain_found_gmail.com. and
domain_found_health.usnews.com outlived the values themselves, so the tag
namespace became a permanent record of answers that had since been corrected.
"""

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
from pipeline_migration import _collapse_domain_found_tags  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _tags(conn, lead_id):
    return {r["tag"] for r in conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ?", (lead_id,))}


def _rearm(conn):
    """init_db() runs migrate_db(), which already fired this one-shot migration
    and set its flag. Clear it so the test can seed legacy rows and re-run."""
    conn.execute("DELETE FROM migration_flags WHERE name = 'domain_found_tag_collapse'")
    conn.commit()


def _seed_legacy_tags(conn, ws_id, pairs):
    for lead_id, tag in pairs:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, ?)""",
            (f"wlt_{ws_id}_{lead_id}_{abs(hash(tag)) % 10**8}", ws_id, lead_id, tag))
    conn.commit()


def test_discovery_writes_one_flag_not_a_tag_per_domain():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Modern Storefront LLC")
    lead = om.resolve_lead(name="Jane Doe", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    ws = om.resolve_workspace_identity(conn, "storefront")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
    conn.commit()
    conn.close()

    payload = {"organic": [{"link": "https://www.modernstorefront.com/contact",
                            "title": "Modern Storefront LLC",
                            "snippet": "Reach info@modernstorefront.com"}],
               "knowledgeGraph": {}}
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: payload):
        om.find_domains_for_workspace("storefront")

    conn = om.get_conn()
    tags = _tags(conn, lead["id"])
    assert "domain_discovered" in tags
    assert not any(t.startswith("domain_found_") for t in tags), tags
    conn.close()


def test_migration_collapses_many_domain_tags_onto_one_flag():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    leads = []
    for i in range(3):
        lead = om.resolve_lead(name=f"P{i}", source="csv", allow_weak_identity=True, conn=conn)
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
        leads.append(lead["id"])
    _seed_legacy_tags(conn, ws["id"], [
        (leads[0], "domain_found_acme.com"),
        (leads[1], "domain_found_beta.co.uk"),
        # The shapes that outlived their corrected values.
        (leads[2], "domain_found_gmail.com."),
        (leads[2], "domain_found_health.usnews.com"),
        # A real segment tag that must survive untouched.
        (leads[0], "office_asset_mgrs"),
    ])

    before = conn.execute(
        "SELECT COUNT(DISTINCT tag) c FROM workspace_lead_tags").fetchone()["c"]
    assert before == 5

    _rearm(conn)
    _collapse_domain_found_tags(conn)
    conn.commit()

    remaining = {r["tag"] for r in conn.execute("SELECT DISTINCT tag FROM workspace_lead_tags")}
    assert remaining == {"domain_discovered", "office_asset_mgrs"}
    # Every lead that had any domain_found_* now carries exactly the one flag.
    for lid in leads:
        assert "domain_discovered" in _tags(conn, lid)
    # ...and a lead that had TWO old tags gets one row, not two.
    assert conn.execute(
        """SELECT COUNT(*) c FROM workspace_lead_tags
           WHERE lead_id = ? AND tag = 'domain_discovered'""", (leads[2],)).fetchone()["c"] == 1
    conn.close()


def test_migration_is_idempotent():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    lead = om.resolve_lead(name="Solo", source="csv", allow_weak_identity=True, conn=conn)
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
    _seed_legacy_tags(conn, ws["id"], [(lead["id"], "domain_found_acme.com")])

    _rearm(conn)
    _collapse_domain_found_tags(conn)
    conn.commit()
    first = _tags(conn, lead["id"])
    _collapse_domain_found_tags(conn)   # flag set -> no-op
    conn.commit()
    assert _tags(conn, lead["id"]) == first == {"domain_discovered"}
    conn.close()


def test_status_tags_are_left_alone():
    """domain_not_found / domain_low_confidence are already single flags and
    carry real review signal -- the sprawl was only ever the value-embedding
    form."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    lead = om.resolve_lead(name="Held", source="csv", allow_weak_identity=True, conn=conn)
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
    _seed_legacy_tags(conn, ws["id"], [
        (lead["id"], "domain_not_found"),
        (lead["id"], "domain_low_confidence"),
    ])
    _rearm(conn)
    _collapse_domain_found_tags(conn)
    conn.commit()
    assert _tags(conn, lead["id"]) == {"domain_not_found", "domain_low_confidence"}
    conn.close()
