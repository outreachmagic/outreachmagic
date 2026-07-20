"""Stage D8: full relay round-trip for company domain membership, role,
label, and verified_mx. build_company_sync_payload() emits a structured
domain_identities field; apply_agent_company_sync_payload() reconstructs
full fidelity from it on a completely fresh database, with a backward-compat
fallback to the older flat aliases array for pre-D8 snapshots.

No wbhk-worker involvement is exercised here (confirmed unnecessary via
direct code reading of relay-db.js -- the relay stores/returns payloads
verbatim) -- these tests simulate the push+pull round trip purely at the
local payload-builder/payload-applier boundary.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from pipeline_personalize import (  # noqa: E402
    apply_agent_company_sync_payload,
    build_company_sync_payload,
)


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def test_payload_includes_domain_identities_with_full_metadata():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role, label, verified_mx) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email', 'Mail Server', 1)",
        (cid,),
    )
    conn.commit()

    payload = build_company_sync_payload(conn, cid)
    assert "mail.acme.com" in payload["aliases"], "flat aliases array must stay unchanged"
    entries = {e["domain"]: e for e in payload["domain_identities"]}
    assert entries["mail.acme.com"]["role"] == "email"
    assert entries["mail.acme.com"]["label"] == "Mail Server"
    assert entries["mail.acme.com"]["verified_mx"] == 1
    conn.close()


def test_payload_omits_domain_identities_when_no_extra_domains():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.commit()
    payload = build_company_sync_payload(conn, cid)
    assert "domain_identities" not in payload
    conn.close()


def test_apply_reconstructs_full_fidelity_on_a_fresh_company_row():
    """The core round-trip proof: apply a payload built from one company
    onto a DIFFERENT (freshly created, no domain) company row and confirm
    role/label/verified_mx all reconstruct correctly -- simulating a
    completely fresh install pulling this company down for the first time."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role, label, verified_mx) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email', 'Mail Server', 1)",
        (cid,),
    )
    conn.commit()
    payload = build_company_sync_payload(conn, cid)
    # The payload is a plain dict, independent of the source row -- delete it
    # (cascades to its company_identities rows) so applying the payload to a
    # separate target below can't accidentally re-resolve back onto this
    # same row via ensure_company()'s own domain/name lookup, which would
    # defeat the point of this test (simulating a fresh install that has
    # never seen this company, under any id, before).
    conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
    conn.commit()

    fresh_id = conn.execute("INSERT INTO companies (name) VALUES ('Acme')").lastrowid
    conn.commit()

    apply_agent_company_sync_payload(fresh_id, payload, conn=conn)
    conn.commit()

    company_row = conn.execute("SELECT domain FROM companies WHERE id = ?", (fresh_id,)).fetchone()
    assert company_row["domain"] == "acme.com", "primary domain must be written"

    identity_row = conn.execute(
        "SELECT role, label, verified_mx, source FROM company_identities "
        "WHERE company_id = ? AND identity_value_normalized = 'mail.acme.com'",
        (fresh_id,),
    ).fetchone()
    assert identity_row is not None
    assert identity_row["role"] == "email"
    assert identity_row["label"] == "Mail Server"
    assert identity_row["verified_mx"] == 1
    assert identity_row["source"] == "relay_pull"
    conn.close()


def test_apply_falls_back_to_bare_alias_for_legacy_payload():
    """A payload with only the older flat aliases array (no domain_identities
    -- simulating an already-stored pre-Stage-D8 snapshot) must still
    reconstruct bare domain membership, just without role/label."""
    conn = om.get_conn()
    legacy_payload = {
        "name": "Acme", "domain": "acme.com",
        "aliases": ["acme.com", "acme", "mail.acme.com"],
    }
    fresh_id = conn.execute("INSERT INTO companies (name) VALUES ('Acme')").lastrowid
    conn.commit()

    apply_agent_company_sync_payload(fresh_id, legacy_payload, conn=conn)
    conn.commit()

    identity_row = conn.execute(
        "SELECT role, source FROM company_identities "
        "WHERE company_id = ? AND identity_value_normalized = 'mail.acme.com'",
        (fresh_id,),
    ).fetchone()
    assert identity_row is not None
    assert identity_row["role"] is None
    assert identity_row["source"] == "relay_alias"
    conn.close()


def test_apply_skips_the_companys_own_name_alias_not_a_real_domain():
    """aliases always includes the company's own lowercased name (per
    build_company_sync_payload) -- the legacy fallback must not create a
    bogus domain identity from it."""
    conn = om.get_conn()
    legacy_payload = {
        "name": "Acme Services", "domain": "acme.com",
        "aliases": ["acme.com", "acme services"],
    }
    fresh_id = conn.execute("INSERT INTO companies (name) VALUES ('Acme Services')").lastrowid
    conn.commit()

    apply_agent_company_sync_payload(fresh_id, legacy_payload, conn=conn)
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM company_identities WHERE company_id = ?", (fresh_id,),
    ).fetchone()["n"]
    assert count == 0, "the company's own name must never be written as a domain identity"
    conn.close()


def test_apply_is_idempotent():
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Acme", domain="acme.com")
    conn.execute(
        "INSERT INTO company_identities (org_id, company_id, identity_type, identity_value_normalized, role) "
        "VALUES ('default', ?, 'domain', 'mail.acme.com', 'email')",
        (cid,),
    )
    conn.commit()
    payload = build_company_sync_payload(conn, cid)
    conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
    conn.commit()

    fresh_id = conn.execute("INSERT INTO companies (name) VALUES ('Acme')").lastrowid
    conn.commit()

    apply_agent_company_sync_payload(fresh_id, payload, conn=conn)
    conn.commit()
    apply_agent_company_sync_payload(fresh_id, payload, conn=conn)
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM company_identities WHERE company_id = ? AND identity_value_normalized = 'mail.acme.com'",
        (fresh_id,),
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_apply_authoritative_attach_updates_existing_empty_domain_row():
    """A name-matched row with no domain yet (e.g. created locally from a
    name-only import) must have the pulled snapshot's domain attached
    directly, not spawn a stray second row -- this payload is resolved
    against a specific, already-known company_id via uid, not a name-only
    guess."""
    conn = om.get_conn()
    existing_id = om.ensure_company(conn, name="Acme", domain=None)
    conn.commit()

    payload = {"name": "Acme", "domain": "acme.com"}
    apply_agent_company_sync_payload(existing_id, payload, conn=conn)
    conn.commit()

    row = conn.execute("SELECT domain FROM companies WHERE id = ?", (existing_id,)).fetchone()
    assert row["domain"] == "acme.com"
    assert conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 1, (
        "must not create a stray second row for an already-known company_id"
    )
    conn.close()
