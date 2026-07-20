"""CLI surface: `pipeline.py import-linkedin-connections --file --workspace --sender [--tag]`."""

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
import pipeline_cli  # noqa: E402

_SAMPLE_CSV = (
    "Notes:\n"
    '"some notice text"\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Jane,Doe,https://www.linkedin.com/in/janedoe,jane@acme.com,Acme Corp,CEO,12 Jan 2026\n"
)


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _run(*extra_args):
    stdout = io.StringIO()
    argv = ["pipeline_cli.py", "import-linkedin-connections", *extra_args]
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(stdout):
            try:
                pipeline_cli.main()
            except SystemExit:
                pass
    return json.loads(stdout.getvalue())


def test_cli_imports_and_reports_summary(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(_SAMPLE_CSV, encoding="utf-8-sig")

    out = _run(
        "--file", str(f), "--workspace", "default", "--sender", "linkedin.com/in/senderprofile",
    )
    assert out["created"] == 1
    assert "error" not in out


def test_cli_missing_file_reports_error():
    out = _run(
        "--file", "/nonexistent/path.csv", "--workspace", "default",
        "--sender", "linkedin.com/in/senderprofile",
    )
    assert "error" in out
