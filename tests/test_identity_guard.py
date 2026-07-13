"""Lead creation requires a *matchable* identity.

build_import_identities() always returns something for a named profile -- it
falls through to name_company/import_key. Those types are never persisted to
lead_identities and are excluded from matching, so a lead whose only identity is
weak used to be created, be unmatchable, and get re-created on every subsequent
sync. That produced ~10.8k "Unknown"/no-email rows in one backfill window
(name="Unknown" is truthy, so it earns an import_key and sailed past the old
`if not identities` check).

The worst offender was the relay snapshot apply path: pipeline_sync.py resolves a
workspace snapshot whose lead is missing locally by calling
resolve_lead_from_agent_sync(entity_key, {}) -- an *empty* payload, so
name defaults to "Unknown" and there is no email or linkedin at all.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from lead_sync import resolve_lead_from_agent_sync  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    """isolated_outreachmagic_data_root (conftest) gives a clean root; create the schema in it."""
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _lead_count():
    conn = om.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    finally:
        conn.close()


def test_agent_sync_empty_payload_creates_no_lead():
    """The exact recipe that made the 10,884 junk rows."""
    result = resolve_lead_from_agent_sync("unknown", {})
    assert result["status"] == "error"
    assert result.get("weak_identity") is True
    assert _lead_count() == 0


def test_name_only_profile_is_rejected():
    """name='Unknown' is truthy and earns an import_key -- it must still be rejected."""
    assert om.resolve_lead(name="Unknown")["status"] == "error"
    assert _lead_count() == 0


def test_name_company_rejected_without_opt_in():
    result = om.resolve_lead(name="Jane Doe", company="Acme Inc")
    assert result["status"] == "error"
    assert result.get("weak_identity") is True
    assert _lead_count() == 0


def test_weak_identity_opt_in_creates_and_persists_composite():
    """The opt-in must PERSIST the composite, or it just reopens the bug."""
    result = om.resolve_lead(name="Jane Doe", company="Acme Inc", allow_weak_identity=True)
    assert result["status"] == "created"

    conn = om.get_conn()
    try:
        types = {
            r[0] for r in conn.execute(
                "SELECT identity_type FROM lead_identities WHERE lead_id = ?", (result["id"],)
            )
        }
    finally:
        conn.close()
    assert "name_company" in types, "composite identity must be persisted so re-import matches"


def test_weak_identity_reimport_matches_and_does_not_duplicate():
    """Persisting is not enough -- resolve_lead must MATCH on the composite too."""
    first = om.resolve_lead(name="Jane Doe", company="Acme Inc", allow_weak_identity=True)
    second = om.resolve_lead(name="Jane Doe", company="Acme Inc", allow_weak_identity=True)
    assert second["id"] == first["id"]
    assert _lead_count() == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Real Person", "email": "real@acme.com"},
        {"name": "LI Person", "linkedin_url": "https://linkedin.com/in/liperson"},
    ],
    ids=["email", "linkedin_only"],
)
def test_strong_identity_still_creates(kwargs):
    """LinkedIn-only leads are legitimate (55k of them) -- don't sweep them up."""
    assert om.resolve_lead(**kwargs)["status"] == "created"
    assert _lead_count() == 1
