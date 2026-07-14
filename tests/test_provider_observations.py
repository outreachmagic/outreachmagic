"""Stage 7: lead_provider_attempts + lead_email_verification unified into one
append-only lead_provider_observations log, with the old names surviving as
read-only compat VIEWs.

These prove the four load-bearing claims: the migration is idempotent, the
compat views still answer what every existing reader expects, both writers
(record_provider_attempt, verify_email/record_platform_bounce) now append
rather than overwrite, and the new table is wired into the outbox exactly
like every other synced child table.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import sync_contract  # noqa: E402
from bounces import record_platform_bounce, verify_email  # noqa: E402
from pipeline_provider_attempts import (  # noqa: E402
    get_provider_attempts_for_lead,
    get_provider_attempts_map,
    has_attempted,
    record_provider_attempt,
)
from provider_observations import (  # noqa: E402
    ORIGIN_ATTEMPT,
    ORIGIN_VERIFICATION,
    compute_obs_uid,
    record_observation,
)


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _mk_lead(conn, email="a@example.com"):
    cur = conn.execute(
        "INSERT INTO leads (name, email, company) VALUES ('A', ?, 'Acme')", (email,)
    )
    conn.commit()
    return cur.lastrowid


def _outbox(conn, entity_type=None):
    sql = "SELECT entity_type, entity_id, op FROM outbox"
    params = ()
    if entity_type:
        sql += " WHERE entity_type = ?"
        params = (entity_type,)
    return conn.execute(sql, params).fetchall()


def _clear_outbox(conn):
    conn.execute("DELETE FROM outbox")
    conn.commit()


# --- table + migration shape ----------------------------------------------


def test_lead_provider_observations_is_a_real_table():
    conn = om.get_conn()
    kind = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'lead_provider_observations'"
    ).fetchone()["type"]
    assert kind == "table"
    conn.close()


def test_legacy_names_are_read_only_views():
    conn = om.get_conn()
    for name in ("lead_email_verification", "lead_provider_attempts"):
        kind = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()["type"]
        assert kind == "view", f"{name} must be a VIEW, not a {kind}"
    conn.close()


def test_migration_flag_is_set_and_migration_is_idempotent():
    conn = om.get_conn()
    assert conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'provider_observations_unification'"
    ).fetchone()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="found")
    conn.commit()
    conn.close()

    from pipeline_migration import migrate_db

    conn = om.get_conn()
    migrate_db(conn)
    migrate_db(conn)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lead_provider_observations"
    ).fetchone()["n"]
    conn.close()
    assert count == 1, "re-running migrate_db must not duplicate the observations table"


# --- writers now append, they don't overwrite ------------------------------


def test_record_provider_attempt_lands_in_observations_with_attempt_origin():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="found", result_email="a@x.com")
    conn.commit()
    row = conn.execute(
        "SELECT kind, origin, provider, status, result_email FROM lead_provider_observations WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["origin"] == ORIGIN_ATTEMPT
    assert row["kind"] == "email_find"
    assert row["provider"] == "trykitt"
    assert row["status"] == "found"
    assert row["result_email"] == "a@x.com"


def test_verify_email_lands_in_observations_with_verification_origin():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    verify_email(lead_id, "valid", "zerobounce")
    conn = om.get_conn()
    row = conn.execute(
        "SELECT kind, origin, provider, status FROM lead_provider_observations WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["origin"] == ORIGIN_VERIFICATION
    assert row["kind"] == "email_verification"
    assert row["provider"] == "zerobounce"
    assert row["status"] == "valid"


def test_record_provider_attempt_is_append_only_not_upsert():
    """The bug this whole table exists to fix: a re-attempt used to overwrite
    the prior row in place, discarding the fact that a second attempt happened."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="not_found", attempted_at="2026-01-01 00:00:00")
    conn.commit()
    record_provider_attempt(conn, lead_id, "trykitt", status="found", attempted_at="2026-01-01 00:05:00")
    conn.commit()
    rows = conn.execute(
        "SELECT status FROM lead_provider_observations WHERE lead_id = ? AND provider = 'trykitt' ORDER BY observed_at",
        (lead_id,),
    ).fetchall()
    conn.close()
    assert [r["status"] for r in rows] == ["not_found", "found"], (
        "both attempts must survive as separate rows"
    )


def test_verify_email_twice_is_append_only():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    verify_email(lead_id, "unknown", "zerobounce", verified_at="2026-01-01 00:00:00")
    verify_email(lead_id, "valid", "zerobounce", verified_at="2026-02-01 00:00:00")
    conn = om.get_conn()
    rows = conn.execute(
        "SELECT status FROM lead_provider_observations WHERE lead_id = ? ORDER BY observed_at",
        (lead_id,),
    ).fetchall()
    conn.close()
    assert [r["status"] for r in rows] == ["unknown", "valid"]


def test_record_observation_is_idempotent_on_exact_replay():
    """A snapshot replay onto a wiped DB must not duplicate history."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    kwargs = dict(
        kind="email_verification", origin=ORIGIN_VERIFICATION, provider="zerobounce",
        status="valid", email="a@example.com", observed_at="2026-01-01 00:00:00",
    )
    obs_uid_1 = record_observation(conn, lead_id, **kwargs)
    conn.commit()
    obs_uid_2 = record_observation(conn, lead_id, **kwargs)
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lead_provider_observations WHERE lead_id = ?", (lead_id,)
    ).fetchone()["n"]
    conn.close()
    assert obs_uid_1 == obs_uid_2
    assert count == 1


def test_same_second_different_status_does_not_collide():
    """Two genuinely different facts (status changed) landing in the same
    wall-clock second must not collapse into one row via obs_uid."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    same_ts = "2026-01-01 00:00:00"
    uid_pending = compute_obs_uid(
        "default", None, "trykitt", "email_find", ORIGIN_ATTEMPT, same_ts, status="pending",
    )
    uid_found = compute_obs_uid(
        "default", None, "trykitt", "email_find", ORIGIN_ATTEMPT, same_ts, status="found",
    )
    conn.close()
    assert uid_pending != uid_found


def test_record_platform_bounce_lands_in_observations():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    record_platform_bounce(conn, lead_id, "a@example.com", "smartlead", "hard", "mailbox does not exist")
    conn.commit()
    row = conn.execute(
        "SELECT kind, origin, provider, status FROM lead_provider_observations WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["kind"] == "platform_bounce"
    assert row["origin"] == ORIGIN_VERIFICATION
    assert row["provider"] == "platform_bounce"
    assert row["status"] == "bounced"


# --- compat views project "latest per provider" ---------------------------


def test_lead_provider_attempts_view_projects_latest_per_provider():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="pending", attempted_at="2026-01-01 00:00:00")
    conn.commit()
    record_provider_attempt(conn, lead_id, "trykitt", status="found", attempted_at="2026-01-02 00:00:00")
    conn.commit()
    rows = get_provider_attempts_for_lead(conn, lead_id)
    conn.close()
    assert len(rows) == 1, f"the legacy view must project exactly one row per provider, got {rows}"
    assert rows[0]["status"] == "found"


def test_lead_email_verification_view_projects_latest_per_provider():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    verify_email(lead_id, "unknown", "zerobounce", verified_at="2026-01-01 00:00:00")
    verify_email(lead_id, "valid", "zerobounce", verified_at="2026-02-01 00:00:00")
    conn = om.get_conn()
    rows = conn.execute(
        "SELECT status FROM lead_email_verification WHERE lead_id = ?", (lead_id,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "valid"


def test_attempt_and_verification_for_same_provider_project_separately():
    """The 2,703-lead intersection: an *attempt* at millionverifier and a
    *verification result* from millionverifier are different observations with
    disjoint columns. Both must be independently visible through their own
    legacy view."""
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    record_provider_attempt(conn, lead_id, "millionverifier", status="found", result_email="a@x.com")
    conn.commit()
    conn.close()
    verify_email(lead_id, "valid", "millionverifier")

    conn = om.get_conn()
    attempt = get_provider_attempts_for_lead(conn, lead_id)
    verification = conn.execute(
        "SELECT status FROM lead_email_verification WHERE lead_id = ?", (lead_id,)
    ).fetchall()
    conn.close()
    assert len(attempt) == 1 and attempt[0]["status"] == "found"
    assert len(verification) == 1 and verification[0]["status"] == "valid"


def test_has_attempted_and_map_still_work_through_the_view():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "icypeas", status="not_found")
    conn.commit()
    assert has_attempted(conn, lead_id, "icypeas") is True
    assert has_attempted(conn, lead_id, "serper") is False
    mapped = get_provider_attempts_map(conn, [lead_id])
    conn.close()
    assert len(mapped[lead_id]) == 1
    assert mapped[lead_id][0]["provider"] == "icypeas"


def test_compute_verification_status_reads_through_the_view():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    verify_email(lead_id, "valid", "zerobounce")
    conn = om.get_conn()
    row = conn.execute(
        "SELECT email_verification_status FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    conn.close()
    assert row["email_verification_status"] == "valid"


# --- outbox wiring ----------------------------------------------------------


def test_provider_observation_marks_lead_dirty():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    _clear_outbox(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="pending")
    conn.commit()
    rows = _outbox(conn, "lead_core")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["op"] == "upsert"


def test_verify_email_marks_lead_dirty():
    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    conn = om.get_conn()
    _clear_outbox(conn)
    conn.close()
    verify_email(lead_id, "valid", "zerobounce")
    conn = om.get_conn()
    rows = _outbox(conn, "lead_core")
    conn.close()
    assert len(rows) == 1


def test_lead_provider_observations_in_sync_contract():
    """Stage 6's contract must know about the table Stage 7 introduced."""
    assert sync_contract.SYNC_MAP["lead_provider_observations"][0] == "lead_core"
    assert "lead_provider_observations" in sync_contract.SYNCED_COLUMNS
    assert "lead_provider_observations" in sync_contract.NOT_SYNCED_COLUMNS
    assert "lead_email_verification" not in sync_contract.SYNC_MAP
    assert "lead_provider_attempts" not in sync_contract.SYNC_MAP


# --- wire: outbound emits provider_observations, inbound accepts both ------


def test_build_lead_core_sync_payload_emits_provider_observations():
    from lead_sync import build_lead_core_sync_payload
    from workspace_routing import DEFAULT_ORG_ID

    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    record_provider_attempt(conn, lead_id, "trykitt", status="found", result_email="a@x.com")
    conn.commit()
    payload = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert "provider_attempts" not in payload, "the legacy key must no longer be emitted"
    obs = payload.get("provider_observations")
    assert obs and obs[0]["provider"] == "trykitt"
    assert obs[0]["origin"] == ORIGIN_ATTEMPT
    assert obs[0]["kind"] == "email_find"


def test_apply_accepts_provider_observations_key():
    from lead_sync import apply_agent_lead_core_payload

    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    apply_agent_lead_core_payload(
        lead_id,
        {
            "provider_observations": [
                {
                    "kind": "email_find", "origin": "attempt", "provider": "trykitt",
                    "status": "found", "result_email": "a@x.com",
                    "observed_at": "2026-01-01 00:00:00",
                },
            ],
        },
        entity_key="a@example.com",
    )
    conn = om.get_conn()
    row = conn.execute(
        "SELECT status, result_email FROM lead_provider_observations WHERE lead_id = ?", (lead_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "found"
    assert row["result_email"] == "a@x.com"


def test_apply_still_accepts_legacy_provider_attempts_key():
    """~150k D1 snapshots already carry the old key; they must still replay."""
    from lead_sync import apply_agent_lead_core_payload

    conn = om.get_conn()
    lead_id = _mk_lead(conn)
    conn.commit()
    conn.close()
    apply_agent_lead_core_payload(
        lead_id,
        {
            "provider_attempts": [
                {"provider": "trykitt", "status": "found", "result_email": "a@x.com"},
            ],
        },
        entity_key="a@example.com",
    )
    conn = om.get_conn()
    rows = get_provider_attempts_for_lead(conn, lead_id)
    conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "found"
