"""domain_discovered means "a real domain is attached for this company", and
the tag-collapse migration (domain_found_tag_collapse) can leave that promise
broken for legacy data: it rewrites domain_found_<domain> tags purely by
STRING, from before the confidence floor existed, without checking whether
the company still has anything to show for it.

Live production example: RangeWater Residential's every domain_lookup
observation, then and now, scored confidence 0.35 -- always below the 0.40
attach floor, correctly tagged domain_low_confidence by every run of today's
code -- but also carried a fossil domain_discovered from before that floor
was added. 240 companies on one production workspace matched "domain_
discovered, no companies.domain"; 203 of them (85%) were the legitimate
pending-merge case (a real domain found, parked behind a merge because
another company row already owns it) and must be left alone. Only the other
15% -- nothing in companies.domain AND nothing in company_identities either
-- are the stale-tag bug this fixes.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_migration import _reconcile_stale_domain_discovered_tags  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _rearm(conn):
    conn.execute(
        "DELETE FROM migration_flags WHERE name = 'domain_discovered_tag_reconcile'")
    conn.commit()


def _lead_with_tag(conn, ws_id, company_name, person, tag):
    cid = om.ensure_company(conn, name=company_name)
    lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead["id"])
    conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, ?)""",
        (f"wlt_{ws_id}_{lead['id']}_{tag}", ws_id, lead["id"], tag))
    conn.commit()
    return cid, lead["id"]


def _domain_lookup_observation(conn, lead_id, confidence):
    import json as _json

    from provider_observations import KIND_DOMAIN_LOOKUP, ORIGIN_ATTEMPT, record_observation
    record_observation(
        conn, lead_id, kind=KIND_DOMAIN_LOOKUP, origin=ORIGIN_ATTEMPT, provider="serper",
        status="found", metadata_json=_json.dumps({"confidence": confidence}))
    conn.commit()


def _tags(conn, lead_id):
    return {r["tag"] for r in conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ?", (lead_id,))}


def test_stale_tag_with_a_low_confidence_observation_is_retagged():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "RangeWater Residential", "R One",
                                  "domain_discovered")
    _domain_lookup_observation(conn, lead_id, 0.35)

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == {"domain_low_confidence"}
    conn.close()


def test_stale_tag_with_no_observation_at_all_is_just_removed():
    """No evidence to reclassify from -- drop it rather than guess."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "No History Co", "N One",
                                  "domain_discovered")

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == set()
    conn.close()


def test_company_with_a_pending_merge_is_left_alone():
    """The legitimate case: a real domain WAS found, but it belongs to a
    duplicate company row, so it lives in company_identities while
    companies.domain stays empty on purpose. 85% of the affected population
    on real data was this case -- it must not be touched."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "Duplicate Co", "D One",
                                  "domain_discovered")
    conn.execute(
        """INSERT INTO company_identities
               (org_id, company_id, identity_type, identity_value_normalized, source)
           VALUES ('default', ?, 'domain', 'duplicateco.com', 'serper_domain_discovery')""",
        (cid,))
    conn.commit()

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == {"domain_discovered"}
    conn.close()


def test_company_with_companies_domain_set_is_left_alone():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "Resolved Co", "Res One",
                                  "domain_discovered")
    conn.execute("UPDATE companies SET domain = 'resolvedco.com' WHERE id = ?", (cid,))
    conn.commit()

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == {"domain_discovered"}
    conn.close()


def test_a_genuinely_high_confidence_observation_with_no_domain_is_just_removed():
    """Edge case: confidence cleared the floor but the domain still never
    landed anywhere (e.g. an older bug). Nothing to retag it AS, so it is
    dropped rather than mislabeled low_confidence."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "Odd Co", "O One", "domain_discovered")
    _domain_lookup_observation(conn, lead_id, 0.95)

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == set()
    conn.close()


def test_migration_is_idempotent():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "RangeWater Residential", "R Two",
                                  "domain_discovered")
    _domain_lookup_observation(conn, lead_id, 0.35)

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()
    first = _tags(conn, lead_id)

    _reconcile_stale_domain_discovered_tags(conn)  # flag set -> no-op
    conn.commit()
    assert _tags(conn, lead_id) == first == {"domain_low_confidence"}
    conn.close()


def test_other_tags_on_the_lead_are_untouched():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _lead_with_tag(conn, ws["id"], "RangeWater Residential", "R Three",
                                  "domain_discovered")
    conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, 'apartment_mgrs')""",
        (f"wlt_{ws['id']}_{lead_id}_seg", ws["id"], lead_id))
    _domain_lookup_observation(conn, lead_id, 0.35)
    conn.commit()

    _rearm(conn)
    _reconcile_stale_domain_discovered_tags(conn)
    conn.commit()

    assert _tags(conn, lead_id) == {"domain_low_confidence", "apartment_mgrs"}
    conn.close()
