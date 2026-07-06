"""skills/outreachmagic/install.sh (the post-install "choose your agent" script) must
run under macOS's stock /bin/bash (3.2), which has no associative arrays."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "skills" / "outreachmagic" / "install.sh"


def _fake_home_with_installed_skill(agent: str) -> Path:
    home = Path(tempfile.mkdtemp())
    skill_dir = home / f".{agent}" / "skills" / "outreachmagic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fake skill\n")
    return home


def _run(agent: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        ["/bin/bash", str(INSTALL), "--agent", agent],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_no_associative_arrays():
    """declare -A requires bash 4+; macOS ships bash 3.2 as /bin/bash."""
    text = INSTALL.read_text(encoding="utf-8")
    assert "declare -A" not in text


def test_runs_under_bash_3_2_for_claude():
    home = _fake_home_with_installed_skill("claude")
    proc = _run("claude", home)
    assert "declare: -A: invalid option" not in proc.stderr
    assert proc.returncode == 0, proc.stderr
    config = json.loads(
        (home / ".claude" / "skills" / "outreachmagic" / "config" / "outreachmagic_config.json").read_text()
    )
    assert config["data_root"] == str(home / ".claude")


def test_runs_under_bash_3_2_for_agents():
    """`agents` is a valid platform (AGENT_DIR_MAP in pipeline.py) but was missing from
    install.sh's directory lookup — covering it so it doesn't regress."""
    home = _fake_home_with_installed_skill("agents")
    proc = _run("agents", home)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(
        (home / ".agents" / "skills" / "outreachmagic" / "config" / "outreachmagic_config.json").read_text()
    )
    assert config["data_root"] == str(home / ".agents")
