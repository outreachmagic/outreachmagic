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
