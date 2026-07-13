"""find_lead_by_identifier must resolve every entity_key shape the relay sends.

The relay keys workspace snapshots by whatever identity the lead had when it was
pushed. 9,015 of them (in a live pull) arrive as "linkedin_sales_nav_id:ACwAA...".

Those all failed to resolve, because the LinkedIn branch sniffs for the substring
"linkedin" -- which the *prefix* of that key contains. The key was routed into the
URL branch, normalize_linkedin() mangled the whole prefixed string into
"linkedin_sales_nav_id:acwaa...", and nothing matched. It never reached
parse_entity_key(), which resolves it correctly.

The lead was then treated as missing, so the snapshot apply path called
resolve_lead_from_agent_sync(entity_key, {}) with an EMPTY payload -- creating a
name="Unknown" lead with no identity. That is the junk-lead factory, and it is why
~10.8k of them exist.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_sync import find_lead_by_identifier  # noqa: E402

SALES_NAV_ID = "ACwAAB4i9MwBEhr66NBorLXXnq8GINUqoBd7WyI"


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _make_lead():
    return om.resolve_lead(
        name="Nav Person",
        email="nav@acme.com",
        linkedin_url="https://linkedin.com/in/navperson",
        identities=[
            ("email", "nav@acme.com"),
            ("linkedin_url", "linkedin.com/in/navperson"),
            ("linkedin_sales_nav_id", SALES_NAV_ID),
        ],
    )["id"]


@pytest.mark.parametrize(
    "entity_key",
    [
        "nav@acme.com",
        "linkedin.com/in/navperson",
        f"linkedin_sales_nav_id:{SALES_NAV_ID}",
        SALES_NAV_ID,
    ],
    ids=["email", "linkedin_url", "prefixed_sales_nav_id", "bare_sales_nav_id"],
)
def test_every_relay_entity_key_shape_resolves(entity_key):
    lead_id = _make_lead()
    conn = om.get_conn()
    try:
        assert find_lead_by_identifier(conn, entity_key) == lead_id, (
            f"entity_key {entity_key!r} must resolve to the lead; when it does not, "
            "the snapshot apply path creates an unmatchable 'Unknown' lead instead"
        )
    finally:
        conn.close()


def test_prefixed_key_is_not_hijacked_by_the_linkedin_substring_check():
    """The regression itself: 'linkedin_sales_nav_id:' *contains* 'linkedin'."""
    lead_id = _make_lead()
    conn = om.get_conn()
    try:
        key = f"linkedin_sales_nav_id:{SALES_NAV_ID}"
        assert "linkedin" in key.lower()  # this is what caused the misroute
        assert find_lead_by_identifier(conn, key) == lead_id
    finally:
        conn.close()


def test_unknown_key_still_returns_none():
    _make_lead()
    conn = om.get_conn()
    try:
        assert find_lead_by_identifier(conn, "linkedin_sales_nav_id:NOPE") is None
        assert find_lead_by_identifier(conn, "") is None
    finally:
        conn.close()
