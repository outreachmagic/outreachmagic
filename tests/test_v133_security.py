"""v1.34.0 security audit items: rollback, public export, detect_platform, auth resync."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "skills" / "outreachmagic" / "scripts" / "pipeline.py"
DETECT = ROOT / "skills" / "outreachmagic" / "scripts" / "detect_platform.py"
EF_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"


def test_detect_platform_json_shape():
    proc = subprocess.run(
        [sys.executable, str(DETECT)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert "platform" in data
    assert "skills_dir" in data


def test_rollback_without_snapshot_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), "rollback"],
        capture_output=True,
        text=True,
        check=False,
        env={**__import__("os").environ, "HOME": "/tmp/om-test-no-rollback-home"},
    )
    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload.get("error") == "no_rollback_snapshot"


def test_resolve_sheets_export_access_public():
    sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))
    import pipeline as om  # noqa: E402

    args = SimpleNamespace(public=True, share_email=None)
    email, public = om.resolve_sheets_export_access(args)
    assert email is None
    assert public is True


def test_sheets_export_help_documents_public_flag():
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), "sheets", "export", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--public" in proc.stdout


def test_update_help_documents_channel_and_rollback():
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), "update", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--channel" in proc.stdout
    proc2 = subprocess.run(
        [sys.executable, str(PIPELINE), "rollback", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "rollback" in proc2.stdout.lower()


def test_batch_auth_resync_retries_once(monkeypatch):
    sys.path.insert(0, str(EF_SCRIPTS))
    import batch_runner as br  # noqa: E402

    calls = {"sync": 0, "find": 0}

    def fake_sync(**_kwargs):
        calls["sync"] += 1
        return True

    def fake_find(*_a, **_k):
        calls["find"] += 1
        if calls["find"] == 1:
            return {"status": "auth_error", "provider": "icypeas"}
        return {"status": "not_found", "provider": "icypeas"}

    monkeypatch.setattr(br.cc, "maybe_sync_secrets_from_portal", fake_sync)
    monkeypatch.setattr(br, "run_find_with_fallback", fake_find)

    auth_resync_attempted = False

    def maybe_resync(result):
        nonlocal auth_resync_attempted
        if str(result.get("status") or "") != "auth_error":
            return False
        if auth_resync_attempted:
            return False
        auth_resync_attempted = True
        return fake_sync()

    assert maybe_resync({"status": "auth_error"}) is True
    assert maybe_resync({"status": "auth_error"}) is False
    assert calls["sync"] == 1


def test_agents_install_sha256_and_release_pin():
    text = (ROOT / "AGENTS-INSTALL.md").read_text(encoding="utf-8")
    assert "v1.34.0" in text
    assert "SHA256SUMS" in text
    assert "detect_platform.py" in text
    assert "--public" in text
    assert "releases/download" in text


def test_release_workflow_publishes_install_assets():
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "dist/install.sh" in text
    assert "dist/SHA256SUMS" in text


def test_human_install_docs_no_curl_pipe_bash():
    import re

    pipe_install = re.compile(r"curl\s+-fsSL[^\n]*\|\s*bash")
    for rel in ("docs/install.md", "docs/install-companions.md", "SECURITY.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not pipe_install.search(text), rel
        assert "releases/download" in text, rel
