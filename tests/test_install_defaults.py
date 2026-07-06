"""install.sh installs the consolidated outreachmagic skill; --platform is the only required flag."""

from __future__ import annotations

import os
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


def test_uninstall_backs_up_config_and_databases_before_deleting(tmp_path: Path):
    """--uninstall must not silently destroy agent_key/last_sync/the local DB."""
    skill_dir = tmp_path / ".claude" / "skills" / "outreachmagic"
    (skill_dir / "config").mkdir(parents=True)
    (skill_dir / "databases").mkdir(parents=True)
    (skill_dir / "config" / "outreachmagic_config.json").write_text('{"agent_key": "om_agent_test"}')
    (skill_dir / "databases" / "outreachmagic.db").write_text("fake db")
    (skill_dir / "SKILL.md").write_text("fake skill\n")

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", str(INSTALL), "--platform", "claude", "--uninstall"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert not skill_dir.exists()
    backups = list((tmp_path / ".claude" / "skills").glob("outreachmagic-backup-*"))
    assert len(backups) == 1, proc.stdout + proc.stderr
    backup = backups[0]
    assert (backup / "config" / "outreachmagic_config.json").read_text() == '{"agent_key": "om_agent_test"}'
    assert (backup / "databases" / "outreachmagic.db").read_text() == "fake db"


def test_uninstall_no_backup_when_nothing_to_preserve(tmp_path: Path):
    """A skill dir with no config/databases (e.g. never initialized) needs no backup."""
    skill_dir = tmp_path / ".claude" / "skills" / "outreachmagic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fake skill\n")

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    subprocess.run(
        ["bash", str(INSTALL), "--platform", "claude", "--uninstall"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert not skill_dir.exists()
    backups = list((tmp_path / ".claude" / "skills").glob("outreachmagic-backup-*"))
    assert backups == []
