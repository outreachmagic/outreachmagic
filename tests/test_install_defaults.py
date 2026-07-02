"""install.sh installs the consolidated outreachmagic skill; --platform is the only required flag."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SUITE = ROOT / "skill-suite.json"


def test_install_help_documents_single_skill():
    proc = subprocess.run(
        ["bash", str(INSTALL), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    text = proc.stdout
    assert "outreachmagic" in text
    assert "Installs outreachmagic" in text
    assert "--lead-enrich-tag" not in text
    assert "--email-finder-tag" not in text


def test_install_sh_has_no_companion_tag_fallbacks():
    text = INSTALL.read_text(encoding="utf-8")
    assert '|| LE_TAG="v' not in text
    assert '|| EF_TAG="v' not in text
    assert "_resolve_companion_tag" not in text


def test_dry_run_uses_outreachmagic_tag():
    proc = subprocess.run(
        ["bash", str(INSTALL), "--local", "--dry-run", "--platform", "cursor"],
        capture_output=True,
        text=True,
        check=True,
    )
    main_version = "v" + (ROOT / "skills" / "outreachmagic" / "scripts" / "VERSION").read_text().strip()
    assert f"outreachmagic tag: {main_version}" in proc.stdout or f"outreachmagic tag:  {main_version}" in proc.stdout


def test_local_dry_run_includes_outreachmagic():
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
    assert "outreachmagic" in proc.stdout
    assert "lead-enrich" not in proc.stdout
    assert "email-finder" not in proc.stdout
