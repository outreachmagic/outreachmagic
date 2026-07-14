#!/usr/bin/env python3
"""Regression test: pulling a bounced lead must not fabricate a fake
"<tool> reported bounced" verification observation.

Found 2026-07-14 on the live db (lead brian.schubert@uti.edu): a lead had
a real millionverifier/valid check and, later, a real PlusVibe platform
hard-bounce (kind=platform_bounce). Every subsequent pull of that lead
added ANOTHER observation: kind=email_verification, provider=millionverifier,
status=bounced -- despite MillionVerifier's API never having been called
again, and despite it never actually returning a "bounced" status (that's
a platform/ESP concept, not a verifier check result).

Root cause: apply_agent_lead_core_payload()'s legacy-replay tail block
(lead_sync.py) unconditionally called verify_email(email_verification_status,
latest_email_verification_source) -- i.e. it took the lead's *rolled-up*
status (which had flipped to "bounced" because of the platform bounce) and
attributed it to the *last tool provider* that ever ran a real check,
fabricating an observation that never happened. That block only needs to
run for pre-Stage-7 D1 snapshots that lack a provider_observations array;
once that array is present, the real events were already replayed and the
tail synthesis is both redundant and wrong.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from lead_sync import (  # noqa: E402
    apply_agent_lead_core_payload,
    build_lead_core_sync_payload,
    resolve_lead_from_agent_sync,
)
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _observations(conn, lead_id):
    return conn.execute(
        "SELECT kind, provider, status FROM lead_provider_observations "
        "WHERE lead_id = ? ORDER BY observed_at",
        (lead_id,),
    ).fetchall()


def test_pull_after_platform_bounce_does_not_fabricate_tool_bounce_observation():
    result = om.resolve_lead(
        email="brian@example.edu",
        name="Brian",
        company="Acme University",
        source="millionverifier",
        source_platform="agent",
    )
    lead_id = result["id"]
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO lead_provider_observations
           (obs_uid, org_id, lead_id, kind, origin, provider, email, status, observed_at)
           VALUES ('mv1', ?, ?, 'email_verification', 'verification', 'millionverifier',
                   'brian@example.edu', 'valid', '2026-07-01 06:01:49')""",
        (DEFAULT_ORG_ID, lead_id),
    )
    conn.execute(
        """INSERT INTO lead_provider_observations
           (obs_uid, org_id, lead_id, kind, origin, provider, email, status, sub_status,
            source_detail, bounce_message, observed_at)
           VALUES ('pb1', ?, ?, 'platform_bounce', 'verification', 'platform_bounce',
                   'brian@example.edu', 'bounced', 'hard_bounce', 'plusvibe:hard',
                   'Recipient address rejected', '2026-07-13 21:51:20')""",
        (DEFAULT_ORG_ID, lead_id),
    )
    # Mirrors what _compute_verification_status() does for real once the
    # bounce lands: the lead's rolled-up status flips to bounced, but
    # latest_email_verification_source (below) still reflects the last
    # *tool* provider, not the bounce.
    conn.execute(
        "UPDATE leads SET email_verification_status = 'bounced', "
        "email_verified_at = '2026-07-13 21:51:20' WHERE id = ?",
        (lead_id,),
    )
    conn.commit()

    payload = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()

    assert payload["email_verification_status"] == "bounced"
    assert payload["latest_email_verification_source"] == "millionverifier"
    assert len(payload["provider_observations"]) == 2

    # Simulate a pull on a fresh machine/db.
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    pulled = resolve_lead_from_agent_sync("brian@example.edu", payload)
    apply_agent_lead_core_payload(
        pulled["id"], payload, org_id=DEFAULT_ORG_ID, entity_key="brian@example.edu",
    )

    conn = om.get_conn()
    rows = _observations(conn, pulled["id"])
    conn.close()

    assert [dict(r) for r in rows] == [
        {"kind": "email_verification", "provider": "millionverifier", "status": "valid"},
        {"kind": "platform_bounce", "provider": "platform_bounce", "status": "bounced"},
    ], (
        "pulling a bounced lead must not fabricate a "
        "kind=email_verification/provider=millionverifier/status=bounced row"
    )


def test_legacy_snapshot_without_provider_observations_still_replays_via_tail_block():
    """Old D1 snapshots (pre-Stage-7) have no provider_observations key at
    all -- the legacy tail-block replay must still work for those, since
    it's the only record of verification state they carry."""
    result = om.resolve_lead(
        email="legacy@example.com",
        name="Legacy Lead",
        company="Acme",
        source="trykitt",
        source_platform="agent",
    )
    lead_id = result["id"]

    legacy_payload = {
        "lead_id": lead_id,
        "email": "legacy@example.com",
        "email_verification_status": "valid",
        "latest_email_verification_source": "trykitt",
        "email_verified_at": "2026-06-01 00:00:00",
    }

    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    pulled = resolve_lead_from_agent_sync("legacy@example.com", legacy_payload)
    apply_agent_lead_core_payload(
        pulled["id"], legacy_payload, org_id=DEFAULT_ORG_ID, entity_key="legacy@example.com",
    )

    conn = om.get_conn()
    rows = _observations(conn, pulled["id"])
    conn.close()

    assert [dict(r) for r in rows] == [
        {"kind": "email_verification", "provider": "trykitt", "status": "valid"},
    ]


if __name__ == "__main__":
    import unittest

    unittest.main()
