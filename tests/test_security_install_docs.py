"""Docs and CLI help must not teach legacy key paths or false aliases."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "skills" / "outreachmagic" / "scripts" / "pipeline.py"


def test_agents_install_uses_download_not_pipe_only():
    text = (ROOT / "AGENTS-INSTALL.md").read_text(encoding="utf-8")
    assert "download" in text.lower()
    assert "agent_secrets.env" in text
    assert "export SERPER_API_KEY" not in text
    assert "~/.hermes/.env" not in text


def test_sheets_help_not_alias_for_review():
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), "sheets", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "alias" not in proc.stdout.lower()


def test_review_help_distinguishes_from_sheets():
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), "review", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "dedup" in proc.stdout.lower() or "two-way" in proc.stdout.lower()


def test_format_local_sync_hint_actionable():
    sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))
    import pipeline as om  # noqa: E402

    hint = om.format_local_sync_hint(
        {
            "total": 3,
            "workspaces": 0,
            "rules": 0,
            "local_agent_events": 0,
            "cloud_pending_leads": 3,
        }
    )
    assert "pipeline.py sync" in hint
    assert "Ask Outreach Magic" not in hint
