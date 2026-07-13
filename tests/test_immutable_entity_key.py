"""The relay entity_key must be immutable.

It used to be derived from mutable columns (email > linkedin_url > identity), which
broke two ways:

  * Finding a lead's email MOVED its wire identity from the LinkedIn URL to the
    address. The relay's snapshot under the old key orphaned, a fresh one appeared
    under the new one, and every bit of workspace state filed under the old key was
    stranded. 52,693 live leads are one email-find away from exactly that. Worse,
    prosp keys its webhooks by LinkedIn URL, so that lead's events keep arriving
    under the *old* key forever -- permanent split brain.

  * A lead with neither email nor LinkedIn produced an EMPTY key, and the push loop
    skips empty keys. 2,830 real leads (a whole conference attendee list) have
    therefore never reached the relay at all.

The key is now an immutable uid, stamped once by a database trigger. Natural keys
ride along as aliases so inbound webhooks still resolve.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from lead_sync import build_lead_core_sync_payload  # noqa: E402
from pipeline_sync import find_lead_by_identifier  # noqa: E402
from workspace_routing import lead_entity_key  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _key(lead_id):
    conn = om.get_conn()
    try:
        return lead_entity_key(conn, om.DEFAULT_ORG_ID, lead_id)
    finally:
        conn.close()


def test_key_does_not_move_when_an_email_is_found():
    """The headline regression: LinkedIn-only lead -> email found -> key must hold."""
    lead_id = om.resolve_lead(
        name="Nav Only", linkedin_url="https://linkedin.com/in/navonly"
    )["id"]
    before = _key(lead_id)
    assert before.startswith("uid:")

    conn = om.get_conn()
    conn.execute("UPDATE leads SET email = ? WHERE id = ?", ("nav@acme.com", lead_id))
    conn.commit()
    conn.close()

    assert _key(lead_id) == before, (
        "finding an email must not move the lead's relay identity; it used to flip "
        "the key from the LinkedIn URL to the address and orphan the old snapshot"
    )


def test_key_does_not_move_when_a_second_email_is_added():
    lead_id = om.resolve_lead(name="Two Mails", email="first@acme.com")["id"]
    before = _key(lead_id)
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO lead_identities (org_id, lead_id, identity_type, identity_value_normalized)
           VALUES (?, ?, 'email', 'second@acme.com')""",
        (om.DEFAULT_ORG_ID, lead_id),
    )
    conn.commit()
    conn.close()
    assert _key(lead_id) == before


def test_lead_with_no_email_or_linkedin_still_gets_a_key():
    """These were silently unpushable: an empty key makes the push loop skip the lead.

    2,830 real leads (a whole conference attendee list, 805 of them tagged) had never
    once reached the relay: entity_key_from_prefetch returned "" for them, and the push
    loop skips an empty key.
    """
    lead_id = om.resolve_lead(
        name="Conference Attendee", company="Example University", allow_weak_identity=True
    )["id"]
    assert _key(lead_id).startswith("uid:"), "every lead must be pushable"


def test_uid_key_resolves_back_to_the_lead():
    lead_id = om.resolve_lead(name="Round Trip", email="rt@acme.com")["id"]
    conn = om.get_conn()
    try:
        assert find_lead_by_identifier(conn, _key(lead_id)) == lead_id
    finally:
        conn.close()


def test_natural_keys_survive_as_aliases():
    """The relay needs these: prosp keys webhooks by LinkedIn URL, plusvibe by email."""
    lead_id = om.resolve_lead(
        name="Aliased", email="alias@acme.com",
        linkedin_url="https://linkedin.com/in/aliased",
    )["id"]
    conn = om.get_conn()
    try:
        payload = build_lead_core_sync_payload(conn, om.DEFAULT_ORG_ID, lead_id)
    finally:
        conn.close()
    aliases = payload.get("aliases") or []
    assert "alias@acme.com" in aliases
    assert "linkedin.com/in/aliased" in aliases


def test_uid_is_stamped_on_insert_and_is_unique():
    ids = [
        om.resolve_lead(name=f"P{i}", email=f"p{i}@acme.com")["id"] for i in range(3)
    ]
    conn = om.get_conn()
    try:
        uids = [
            conn.execute("SELECT uid FROM leads WHERE id = ?", (i,)).fetchone()["uid"]
            for i in ids
        ]
    finally:
        conn.close()
    assert all(uids), "the AFTER INSERT trigger must stamp a uid on every new lead"
    assert len(set(uids)) == len(uids)
