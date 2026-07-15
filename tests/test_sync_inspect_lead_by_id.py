"""sync --inspect --type lead must work by lead id, not just email -- a
weak-identity lead (matched only by name+company, no email ever found) has
no email to look it up by at all.
"""

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

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


def _run_inspect(*extra_args):
    stdout = io.StringIO()
    argv = ["pipeline.py", "sync", "--inspect", *extra_args]
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(stdout):
            try:
                om.main()
            except SystemExit:
                pass
    return json.loads(stdout.getvalue())


def test_inspect_lead_by_id_for_weak_identity_lead_with_no_email():
    result = om.resolve_lead(
        name="No Email Lead", company="Acme", source="csv", allow_weak_identity=True,
    )
    lead_id = result["id"]
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()
    conn.close()

    out = _run_inspect(str(lead_id), "--type", "lead", "--workspace", "default")
    assert "error" not in out, out
    assert out["lead_id"] == lead_id
    assert "full_sync_payload" in out, "must return the actual payload, not just its key names"
    assert "full_sync_payload_keys" not in out


def test_inspect_lead_full_sync_payload_has_real_values_not_just_keys():
    """inspect_sync_lead used to return full_sync_payload_keys (sorted(payload.keys())
    -- just the field names) instead of the payload itself, for both the email and
    id lookup paths equally (they share the same function). Confirm it now returns
    the real values, matching what sync-preview/a real push would send."""
    om.resolve_lead(
        email="fullpayload@example.com", name="Full Payload Lead", title="VP Sales",
        company="Acme", source="csv",
    )
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    lead_id = conn.execute(
        "SELECT id FROM leads WHERE email = 'fullpayload@example.com'"
    ).fetchone()["id"]
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()
    conn.close()

    out = _run_inspect("fullpayload@example.com", "--type", "lead", "--workspace", "default")
    payload = out["full_sync_payload"]
    assert payload["name"] == "Full Payload Lead"
    assert payload["title"] == "VP Sales"
    assert payload["email"] == "fullpayload@example.com"


def test_inspect_lead_by_email_still_works():
    result = om.resolve_lead(email="normal@example.com", name="Normal Lead", company="Acme", source="csv")
    lead_id = result["id"]
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()
    conn.close()

    out = _run_inspect("normal@example.com", "--type", "lead", "--workspace", "default")
    assert "error" not in out, out
    assert out["lead_id"] == lead_id


def test_inspect_lead_by_nonexistent_id_reports_not_found():
    out = _run_inspect("999999999", "--type", "lead", "--workspace", "default")
    assert "error" in out
    assert "not found" in out["error"]
