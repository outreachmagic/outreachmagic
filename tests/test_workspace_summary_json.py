#!/usr/bin/env python3
"""workspace summary --json must emit a single parseable JSON object on stdout."""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(tempfile.mkdtemp()))

import pipeline as om  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_workspace_summary_json_stdout_only():
    _reset_db()
    om.create_workspace("Test WS", slug="testws")
    pending = {
        "workspaces": 0,
        "rules": 0,
        "local_agent_events": 0,
        "cloud_pending_lead_core": 2,
        "cloud_pending_lead_workspaces": 1,
        "cloud_pending_leads": 3,
        "total": 3,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(om, "get_local_pending_counts", lambda: pending),
        patch.object(om, "notify_update_available", lambda **kwargs: None),
        patch.object(sys, "argv", ["pipeline.py", "workspace", "summary", "--workspace", "testws", "--json"]),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        om.main()

    out = stdout.getvalue().strip()
    assert stderr.getvalue() == "", f"unexpected stderr: {stderr.getvalue()!r}"
    payload = json.loads(out)
    assert payload["workspace"] == "testws"
    assert payload["local_pending"] == pending


if __name__ == "__main__":
    test_workspace_summary_json_stdout_only()
    print("ok")
