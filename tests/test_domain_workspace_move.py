"""Reassigning a sending domain's mailboxes to another workspace.

The reported symptom was "it acts like it works but they don't get removed".
Three separate causes, all real:

1. `/api/domains/workspaces` answers HTTP 200 with `{"status": "error"}` when a
   mailbox fails, and the dashboard's `formResult` only rejected on HTTP status
   -- so a rejected write printed "Saved." (fixed in dashboard.html's `post`).
2. Membership lives in `workspace_sender_accounts`, which `SYNC_MAP` does not
   cover, so the write fired no outbox trigger and never reached the relay --
   while the inbound payload *does* carry `workspace_slugs`. The next pull
   reconciled the local edit away. `test_membership_change_is_enqueued_for_push`
   is the guard.
3. The write matched `email_domain` with bare LOWER() while `domain_health`
   groups on the stored value, so a padded or oddly-cased row was skipped.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_sender_accounts as psa  # noqa: E402
import reporting_queries  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed(domain_value="acmemail.com"):
    om.create_workspace("Alpha", slug="alpha")
    om.create_workspace("Beta", slug="beta")
    conn = om.get_conn()
    alpha = om.resolve_workspace_identity(conn, "alpha")["id"]
    for local in ("ann", "bob"):
        sid = psa.upsert_sender_account(
            conn, {"email": f"{local}@acmemail.com", "last_outbound_at": "2026-01-01"})
        conn.execute(
            "UPDATE sender_accounts SET email_domain = ?, last_outbound_at = ? WHERE id = ?",
            (domain_value, "2026-01-01 00:00:00", sid))
        psa._link_workspace(conn, alpha, sid)
    conn.commit()
    conn.close()
    return alpha


def _domains_for(slug):
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, slug)["id"]
    try:
        return {d["domain"] for d in reporting_queries.domain_health(conn, ws)["domains"]}
    finally:
        conn.close()


def test_reassigned_domain_leaves_the_old_workspace():
    _seed()
    assert "acmemail.com" in _domains_for("alpha")
    res = psa.set_domain_workspaces("acmemail.com", ["beta"])
    assert res["status"] == "ok" and res["updated"] == 2
    assert "acmemail.com" not in _domains_for("alpha")
    assert "acmemail.com" in _domains_for("beta")


def test_domain_with_no_mailboxes_is_an_error_not_a_silent_ok():
    """The UI shows this verbatim now, so it must not claim success."""
    _seed()
    res = psa.set_domain_workspaces("nosuchdomain.com", ["beta"])
    assert res["status"] == "error"
    assert "no mailboxes" in res["error"]


def test_padded_or_mixed_case_domain_still_matches():
    """domain_health groups on the stored value; the write must find the same rows."""
    _seed(domain_value="  AcmeMail.com ")
    res = psa.set_domain_workspaces("acmemail.com", ["beta"])
    assert res["status"] == "ok" and res["updated"] == 2


def test_membership_change_is_enqueued_for_push():
    """Without this the relay never learns, and the next pull reverts the edit."""
    _seed()
    conn = om.get_conn()
    conn.execute("DELETE FROM outbox WHERE entity_type = 'sender_account'")
    conn.commit()
    conn.close()

    psa.set_domain_workspaces("acmemail.com", ["beta"])

    conn = om.get_conn()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) n FROM outbox"
            " WHERE entity_type = 'sender_account' AND op = 'upsert'").fetchone()["n"]
    finally:
        conn.close()
    assert pending == 2, "membership change must mark its sender accounts dirty"


def test_pushed_payload_carries_the_new_membership():
    _seed()
    psa.set_domain_workspaces("acmemail.com", ["beta"])
    conn = om.get_conn()
    try:
        sid = conn.execute(
            "SELECT id FROM sender_accounts WHERE email = 'ann@acmemail.com'").fetchone()["id"]
        payload = psa.build_sender_account_sync_payload(conn, sid)
    finally:
        conn.close()
    assert payload["workspace_slugs"] == ["beta"]
