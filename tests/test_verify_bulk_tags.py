"""Tag-scoped verification candidates.

MillionVerifier bills per email, so a campaign wants to verify the segment it
is about to send to -- not every address in the workspace. Before this, the
only scope was the whole workspace (339 leads) when the actual send was one
segment (80).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    made = {}
    for person, email, tags in [
        ("Ann A", "ann@acme.com", ["seg_a"]),
        ("Bob B", "bob@acme.com", ["seg_a"]),
        ("Cy C", "cy@beta.com", ["seg_b"]),
        ("Dee D", "dee@beta.com", ["seg_a", "seg_b"]),   # in both
        ("Eve E", "eve@gamma.com", []),                   # untagged
    ]:
        lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET email = ? WHERE id = ?", (email, lead["id"]))
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
        for t in tags:
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (f"wlt_{ws['id']}_{lead['id']}_{t}", ws["id"], lead["id"], t))
        made[person] = lead["id"]
    conn.commit()
    conn.close()
    return made


def _names(**kw):
    res = om.leads_needing_verification("storefront", **kw)
    return {l["name"] for l in res["leads"]}


def test_no_tag_returns_the_whole_workspace():
    _seed()
    assert _names() == {"Ann A", "Bob B", "Cy C", "Dee D", "Eve E"}


def test_a_tag_scopes_to_that_segment():
    _seed()
    assert _names(tags=["seg_a"]) == {"Ann A", "Bob B", "Dee D"}
    assert _names(tags=["seg_b"]) == {"Cy C", "Dee D"}


def test_multiple_tags_are_ORed_and_deduped():
    """A lead carrying both tags must appear once, not twice -- each row is a
    billable verification."""
    _seed()
    res = om.leads_needing_verification("storefront", tags=["seg_a", "seg_b"])
    ids = [l["lead_id"] for l in res["leads"]]
    assert len(ids) == len(set(ids))
    assert {l["name"] for l in res["leads"]} == {"Ann A", "Bob B", "Cy C", "Dee D"}


def test_untagged_leads_are_excluded_when_a_tag_is_given():
    _seed()
    assert "Eve E" not in _names(tags=["seg_a"])


def test_an_unknown_tag_returns_nothing():
    _seed()
    assert _names(tags=["does_not_exist"]) == set()


def test_the_tag_filter_is_reported_back():
    _seed()
    res = om.leads_needing_verification("storefront", tags=["seg_a"])
    assert res["tags"] == ["seg_a"]
    assert om.leads_needing_verification("storefront")["tags"] is None


def test_leads_without_an_email_are_never_candidates():
    """Verification is per address; a lead with no email cannot be billed for."""
    _seed()
    conn = om.get_conn()
    conn.execute("UPDATE leads SET email = NULL WHERE name = 'Ann A'")
    conn.commit()
    conn.close()
    assert _names(tags=["seg_a"]) == {"Bob B", "Dee D"}
