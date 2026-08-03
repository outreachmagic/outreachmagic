"""Multi-tag filtering: any / all / none, composable, on the shared clause.

`lead_filter_clause` is the single WHERE behind the contacts list, its stat
counts, the CSV export and bulk id selection — so these semantics only have to
be right once, but they have to be right there.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dashboard_queries as dq  # noqa: E402
import lead_export  # noqa: E402
import pipeline as om  # noqa: E402
import pipeline_tags  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _setup():
    """Three contacts: one tagged {a,b}, one {a}, one {a,c}."""
    om.create_workspace("Acme Outbound", slug="acme")
    conn = om.get_conn()
    ws_id = om.resolve_workspace_identity(conn, "acme")["id"]
    for name, tags in (("Both AB", ["alpha", "beta"]),
                       ("Only A", ["alpha"]),
                       ("A and C", ["alpha", "gamma"])):
        lead = om.resolve_lead(name=name, email=f"{name.replace(' ', '')}@acme.com",
                               source="csv", allow_weak_identity=True, conn=conn)
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead["id"])
        conn.commit()
        for tag in tags:
            pipeline_tags.tag_add(ws_id, lead["id"], tag)
    return conn, ws_id


def _names(conn, ws_id, **filters):
    return sorted(r["name"] for r in dq.search_leads(conn, ws_id, limit=50, **filters)["leads"])


def test_tags_any_is_a_union():
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tags_any=["beta", "gamma"]) == ["A and C", "Both AB"]
    conn.close()


def test_tags_all_requires_every_tag():
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tags_all=["alpha", "beta"]) == ["Both AB"]
    assert _names(conn, ws_id, tags_all=["alpha"]) == ["A and C", "Both AB", "Only A"]
    conn.close()


def test_tags_none_excludes():
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tags_none=["beta"]) == ["A and C", "Only A"]
    conn.close()


def test_include_and_exclude_compose():
    """The stated use case: two included tags and one excluded."""
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tags_all=["alpha"], tags_none=["gamma"]) == [
        "Both AB", "Only A"]
    assert _names(conn, ws_id, tags_any=["beta", "gamma"], tags_none=["gamma"]) == [
        "Both AB"]
    conn.close()


def test_legacy_scalar_tag_still_works_and_folds_into_any():
    """`tag` is in the stats drill-through, the CLI and saved agent commands.
    Renaming it would be a breaking change across the whole surface."""
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tag="beta") == ["Both AB"]
    assert _names(conn, ws_id, tag="beta", tags_any=["gamma"]) == ["A and C", "Both AB"]
    conn.close()


def test_tag_values_are_normalized():
    """Tags are stored normalized, so the filter has to normalize too or
    "ALPHA" and " alpha " silently match nothing."""
    conn, ws_id = _setup()
    assert _names(conn, ws_id, tags_any=["  ALPHA  "]) == ["A and C", "Both AB", "Only A"]
    conn.close()


def test_export_inherits_the_same_tag_semantics():
    """"Export what's on screen" is only true if both build the same WHERE."""
    conn, ws_id = _setup()
    _cols, rows = lead_export.export_rows(
        conn, ws_id, fields=["name"], tags_all=["alpha"], tags_none=["gamma"])
    assert sorted(r["name"] for r in rows) == ["Both AB", "Only A"]
    conn.close()


def test_stats_and_list_agree_on_the_same_filters():
    """The discrepancy this fix exists for: the stats query used to run its own
    WHERE with no record_type predicate while the list defaulted to people, so
    clicking a tile returned a different number than the tile showed."""
    conn, ws_id = _setup()
    for filters in ({}, {"tags_all": ["alpha"]}, {"tags_none": ["beta"]}):
        listed = dq.search_leads(conn, ws_id, limit=50, **filters)["total"]
        counted = dq.contacts_stats(conn, ws_id, **filters)["overall"]["total"]
        assert listed == counted, f"{filters}: list {listed} != stats {counted}"
    conn.close()


def test_stats_counts_placeholders_separately_from_people():
    conn, ws_id = _setup()
    lead = om.resolve_lead(name="Acme Inc", company="Acme Inc", source="csv",
                           allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET record_type = 'company_placeholder' WHERE id = ?",
                 (lead["id"],))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead["id"])
    conn.commit()

    stats = dq.contacts_stats(conn, ws_id)
    # The record-type tiles report the inventory; `overall` reports the
    # currently-selected record type, which defaults to people.
    assert stats["record_types"]["people"] == 3
    assert stats["record_types"]["companies"] == 1
    assert stats["overall"]["total"] == 3
    assert dq.contacts_stats(conn, ws_id, record_type="all")["overall"]["total"] == 4
    conn.close()
