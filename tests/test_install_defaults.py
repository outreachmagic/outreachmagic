"""install.sh installs full suite; --platform is the only required flag."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SUITE = ROOT / "skill-suite.json"


def test_install_help_documents_suite_not_optional_flags():
    proc = subprocess.run(
        ["bash", str(INSTALL), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    text = proc.stdout
    assert "lead-enrich" in text
    assert "email-finder" in text
    assert "--migrate" not in text
    assert "--with-lead-enrich" not in text


def test_install_sh_has_no_hardcoded_companion_tag_fallbacks():
    text = INSTALL.read_text(encoding="utf-8")
    assert '|| LE_TAG="v' not in text
    assert '|| EF_TAG="v' not in text
    assert "install_default_tag" in text or "skill-suite.json" in text


def test_dry_run_uses_skill_suite_companion_tags():
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    le_tag = suite["skills"]["lead-enrich"]["install_default_tag"]
    ef_tag = suite["skills"]["email-finder"]["install_default_tag"]
    proc = subprocess.run(
        ["bash", str(INSTALL), "--local", "--dry-run", "--platform", "cursor"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"lead-enrich tag:   {le_tag}" in proc.stdout
    assert f"email-finder tag:  {ef_tag}" in proc.stdout


def test_local_dry_run_always_includes_companions():
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--local",
            "--dry-run",
            "--platform",
            "cursor",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "lead-enrich" in proc.stdout
    assert "email-finder" in proc.stdout
    assert "outreachmagic" in proc.stdout
