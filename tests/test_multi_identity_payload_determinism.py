"""A lead merge can leave more than one row for a normally-scalar identity
(external_id) or a normally-order-insensitive set (tags). Without a
deterministic tie-break, two builds of the "same" payload -- one via the
single-lookup path (sync-preview/sync-diff), one via the bulk-push prefetch
path (real sync) -- could disagree, or the same lead could hash differently
across two runs for no real reason and get needlessly re-pushed forever.

Found via sync-diff during the Stage 10c relay round-trip verification: a
sales-nav case-merge survivor (two external_id rows, one from each merged
lead) reported "out of sync" purely because sync-diff's single lookup and
the last real push (bulk prefetch) had picked different rows.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from lead_sync import _load_lead_sync_prefetch, build_lead_core_sync_payload  # noqa: E402
from workspace_routing import lead_external_id_value  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_lead(conn, **cols):
    base = {"name": "A", "email": "a@example.com", "company": "Acme"}
    base.update(cols)
    keys = ", ".join(base)
    marks = ", ".join("?" * len(base))
    cur = conn.execute(f"INSERT INTO leads ({keys}) VALUES ({marks})", tuple(base.values()))
    conn.commit()
    return cur.lastrowid


def _add_external_id(conn, lead_id, value, *, created_at):
    conn.execute(
        """INSERT INTO lead_identities
               (org_id, lead_id, identity_type, identity_value_normalized, source, created_at)
           VALUES (?, ?, 'external_id', ?, 'relay', ?)""",
        (om.DEFAULT_ORG_ID, lead_id, value, created_at),
    )
    conn.commit()


def test_external_id_agrees_between_single_lookup_and_prefetch_paths():
    conn = om.get_conn()
    lead_id = _mk_lead(conn, email="merge-survivor@example.com")
    _add_external_id(conn, lead_id, "sales_navigator:1448614903", created_at="2026-06-17 13:24:26")
    _add_external_id(conn, lead_id, "icypeas:68c5ef1470e8cd5842431a93", created_at="2026-07-12 06:31:19")

    single = lead_external_id_value(conn, om.DEFAULT_ORG_ID, lead_id)

    prefetch = _load_lead_sync_prefetch(conn, om.DEFAULT_ORG_ID, [lead_id])
    payload = build_lead_core_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id, prefetch=prefetch)
    conn.close()

    assert single == "icypeas:68c5ef1470e8cd5842431a93"
    assert payload["external_id"] == single, (
        "sync-diff (single lookup) and a real push (prefetch batch) must resolve "
        "the same external_id for a lead with more than one identity row"
    )


def test_external_id_prefers_most_recently_recorded():
    conn = om.get_conn()
    lead_id = _mk_lead(conn, email="newer-wins@example.com")
    _add_external_id(conn, lead_id, "old_source:1", created_at="2020-01-01 00:00:00")
    _add_external_id(conn, lead_id, "new_source:2", created_at="2026-01-01 00:00:00")
    value = lead_external_id_value(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert value == "new_source:2"


def test_workspace_payload_tags_are_sorted_regardless_of_insertion_order():
    """Same created_at (a same-instant batch import) ties the old ORDER BY, so
    the emitted order -- and therefore content_hash -- must not depend on it."""
    from lead_sync import build_lead_workspace_sync_payload

    conn = om.get_conn()
    lead_id = _mk_lead(conn, email="tags-order@example.com")
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    for tag in ("zzz_last", "aaa_first", "mmm_mid"):
        conn.execute(
            "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag, created_at) "
            "VALUES (?, ?, ?, ?, '2026-07-11 07:00:00')",
            (f"{ws_row['id']}:{lead_id}:{tag}", ws_row["id"], lead_id, tag),
        )
    conn.commit()

    payload = build_lead_workspace_sync_payload(
        conn, om.DEFAULT_ORG_ID, lead_id, workspace_slug="default",
    )
    conn.close()
    assert payload["tags"] == ["aaa_first", "mmm_mid", "zzz_last"]
