"""Pipeline stage is per-workspace; the lead payload stops repeating itself.

Three defects, all visible in the D1 snapshots:

1. `stage` was an org-wide column *and* a per-workspace one. A lead can be
   `replied` in one workspace and `prospecting` in another, so the org-wide copy
   is ill-defined by construction. Worse, the *workspace* payload only emitted
   its stage when it differed from the org one -- so rebuilding workspace state
   from the relay silently depended on core state.

2. list_source / import_name / email_verification_source were verbatim copies of
   latest_source_detail / original_source_detail / latest_email_verification_source
   -- three strings sent twice on every one of ~150k lead payloads.

3. *_source_platform held the transport ("relay"), not a provenance fact.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from lead_sync import (  # noqa: E402
    build_lead_core_sync_payload,
    build_lead_workspace_sync_payload,
)


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


def _join_ws(conn, lead_id, slug=None, status="prospecting"):
    if slug:
        row = conn.execute("SELECT id FROM workspaces WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
                (slug, "default", slug, slug),
            )
            ws_id = slug
        else:
            ws_id = row["id"]
    else:
        ws_id = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()["id"]
    org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{ws_id}:{lead_id}", org, ws_id, lead_id, status),
    )
    conn.commit()
    return ws_id


def test_core_payload_has_no_stage():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _join_ws(conn, lead_id, status="replied")
    payload = build_lead_core_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert "stage" not in payload, (
        "stage is a per-workspace fact and must not ride on the org-wide snapshot"
    )


def test_workspace_payload_always_carries_its_stage():
    """The old code omitted it whenever it matched leads.stage, so a rebuild from
    the relay lost the stage of every lead that agreed with the org-wide value."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    ws_id = _join_ws(conn, lead_id, status="prospecting")
    slug = conn.execute(
        "SELECT slug FROM workspaces WHERE id = ?", (ws_id,)
    ).fetchone()["slug"]
    payload = build_lead_workspace_sync_payload(
        conn, om.DEFAULT_ORG_ID, lead_id, workspace_slug=slug
    )
    conn.close()
    assert payload.get("stage") == "prospecting"


def test_core_payload_drops_duplicate_and_transport_fields():
    """`*_source_platform` used to hold "relay" (the transport) on 85% of leads.
    The DB now aborts any INSERT/UPDATE that would install a transport string
    there, so the wire never sees it either. The four duplicate wire fields
    (list_source / import_name / email_verification_source / stage-on-core) also
    stay off the payload."""
    conn = om.get_conn()
    lead_id = _mk_lead(
        conn,
        original_source="csv_import",
        original_source_detail="list A",
        original_source_platform="csv",
        latest_source="plusvibe",
        latest_source_detail="list B",
        latest_source_platform="plusvibe",
    )
    _join_ws(conn, lead_id)
    payload = build_lead_core_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id)
    conn.close()

    for dupe in ("list_source", "import_name", "email_verification_source"):
        assert dupe not in payload, f"{dupe} is a verbatim copy of another field"
    for transport in ("original_source_platform", "latest_source_platform"):
        assert transport not in payload, f"{transport} carries transport, not provenance"

    assert payload["original_source_detail"] == "list A"
    assert payload["latest_source_detail"] == "list B"


def test_leads_table_aborts_transport_strings_in_provenance_columns():
    """Guard behind the wire-side drop: without it, a code path that (re)wrote
    "agent_sync"/"relay_sync"/"relay" into a provenance column would silently
    re-dirty 85% of leads and the report drift would come back."""
    conn = om.get_conn()
    for col in ("original_source", "latest_source", "original_source_platform", "latest_source_platform"):
        for value in ("agent_sync", "relay_sync", "relay"):
            try:
                conn.execute(
                    f"INSERT INTO leads (name, email, {col}) VALUES ('T', 't@e.com', ?)",
                    (value,),
                )
            except sqlite3.IntegrityError as exc:
                assert "transport string" in str(exc)
                continue
            raise AssertionError(f"leads.{col} = {value!r} should have been rejected")
    conn.close()


def test_leads_stage_is_derived_from_the_workspace():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _join_ws(conn, lead_id, status="prospecting")

    conn.execute(
        "UPDATE workspace_leads SET status = 'replied' WHERE lead_id = ?", (lead_id,)
    )
    conn.commit()
    stage = conn.execute(
        "SELECT stage FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()["stage"]
    conn.close()
    assert stage == "replied", "the org-wide cache must follow the workspace, not lead it"


def test_derived_stage_takes_the_furthest_across_workspaces():
    """A lead in two workspaces has no single stage. The cache reports the
    furthest, matching furthest_stage()'s ordering."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _join_ws(conn, lead_id, slug="ws-a", status="prospecting")
    _join_ws(conn, lead_id, slug="ws-b", status="interested")
    stage = conn.execute(
        "SELECT stage FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()["stage"]
    conn.close()
    assert stage == "interested"


def test_auto_import_notes_cleared_but_human_notes_survive():
    conn = om.get_conn()
    auto = _mk_lead(conn, email="auto@example.com",
                    notes="Auto-imported from smartlead via relay")
    human = _mk_lead(conn, email="human@example.com",
                     notes="Met at SaaStr, wants a demo in Q3")
    mentions = _mk_lead(conn, email="m@example.com",
                        notes="Auto-imported from smartlead via relay, but verify with Sam")
    conn.close()

    om.migrate_db()

    conn = om.get_conn()
    rows = {r["id"]: r["notes"] for r in conn.execute("SELECT id, notes FROM leads")}
    conn.close()
    assert rows[auto] is None, "the machine-written note should be gone"
    assert rows[human] == "Met at SaaStr, wants a demo in Q3"
    assert rows[mentions] is not None, "a human note is not a machine note"


def test_linkedin_bio_folds_out_of_personalization():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.execute(
        "INSERT INTO lead_personalization (lead_id, field_name, field_value) "
        "VALUES (?, 'linkedin_bio', 'Head of Ops at Acme')",
        (lead_id,),
    )
    conn.execute(
        "INSERT INTO lead_personalization (lead_id, field_name, field_value) "
        "VALUES (?, 'first_name', 'Gylian')",
        (lead_id,),
    )
    conn.commit()
    conn.close()

    om.migrate_db()

    conn = om.get_conn()
    bio = conn.execute(
        "SELECT linkedin_bio FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()["linkedin_bio"]
    fields = {
        r["field_name"]
        for r in conn.execute(
            "SELECT field_name FROM lead_personalization WHERE lead_id = ?", (lead_id,)
        )
    }
    conn.close()

    assert bio == "Head of Ops at Acme", "the bio must be preserved on the lead"
    assert "linkedin_bio" not in fields, "it duplicated a leads column"
    assert "first_name" in fields, (
        "a mailmerge-ready first_name is a human render value, not a duplicate of leads.name"
    )
