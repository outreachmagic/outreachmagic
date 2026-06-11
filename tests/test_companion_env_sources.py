"""Companion API keys must load only from portal-synced agent_secrets.env (strict mode)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LE_SCRIPTS = ROOT / "skills" / "lead-enrich" / "scripts"
OM_SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(LE_SCRIPTS))
sys.path.insert(0, str(OM_SCRIPTS))

import companion_common as cc  # noqa: E402


def _stub_agent_secrets_cloud(skills: Path) -> None:
    (skills / "scripts" / "agent_secrets_cloud.py").write_text(
        "import os\n"
        "def parse_agent_secrets_file(p):\n"
        "    out = {}\n"
        "    for line in open(p):\n"
        "        line = line.strip()\n"
        "        if not line or line.startswith('#') or '=' not in line:\n"
        "            continue\n"
        "        k, v = line.split('=', 1)\n"
        "        out[k.strip()] = [v.strip()]\n"
        "    return out\n"
        "def apply_secrets_to_environ(pools, override=False):\n"
        "    for k, vals in pools.items():\n"
        "        if vals:\n"
        "            os.environ[k] = vals[0]\n"
        "def load_local_agent_secrets_to_environ(override=False): pass\n"
    )


def test_icypeas_not_loaded_from_hermes_when_missing_from_agent_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes"
        skills = home / "skills" / "outreachmagic"
        (skills / "config").mkdir(parents=True)
        (skills / "scripts").mkdir(parents=True)
        (skills / "scripts" / "pipeline.py").write_text("# test stub\n")
        _stub_agent_secrets_cloud(skills)
        (skills / "config" / "agent_secrets.env").write_text("TRYKITT_API_KEY=from-dashboard\n")
        (home / ".env").write_text("ICYPEAS_API_KEY=stale-wrong-key\n")

        os.environ.pop("OM_ALLOW_LOCAL_API_KEYS", None)
        os.environ["HERMES_HOME"] = str(home)
        os.environ.pop("ICYPEAS_API_KEY", None)
        os.environ.pop("TRYKITT_API_KEY", None)
        cc._AGENT_ENV_LOADED = False
        skill_dir = home / "skills" / "email-finder"
        skill_dir.mkdir(parents=True)
        cc.ensure_agent_env_loaded(skill_dir, reload=True)

        assert os.environ.get("TRYKITT_API_KEY") == "from-dashboard"
        assert not os.environ.get("ICYPEAS_API_KEY")


def test_serper_not_loaded_from_hermes_in_strict_mode():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes"
        skills = home / "skills" / "outreachmagic"
        (skills / "config").mkdir(parents=True)
        (skills / "scripts").mkdir(parents=True)
        (skills / "scripts" / "pipeline.py").write_text("# test stub\n")
        _stub_agent_secrets_cloud(skills)
        (home / ".env").write_text("SERPER_API_KEY=from-hermes\n")
        os.environ.pop("OM_ALLOW_LOCAL_API_KEYS", None)
        os.environ["HERMES_HOME"] = str(home)
        os.environ.pop("SERPER_API_KEY", None)
        cc._AGENT_ENV_LOADED = False
        cc.ensure_agent_env_loaded(reload=True)
        assert not os.environ.get("SERPER_API_KEY")


def test_serper_loads_from_hermes_when_local_keys_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes"
        home.mkdir()
        (home / ".env").write_text("SERPER_API_KEY=from-hermes\n")
        os.environ["OM_ALLOW_LOCAL_API_KEYS"] = "1"
        os.environ["HERMES_HOME"] = str(home)
        os.environ.pop("SERPER_API_KEY", None)
        cc._AGENT_ENV_LOADED = False
        cc.ensure_agent_env_loaded(reload=True)
        assert os.environ.get("SERPER_API_KEY") == "from-hermes"
        os.environ.pop("OM_ALLOW_LOCAL_API_KEYS", None)


def test_stale_shell_key_cleared_when_not_in_agent_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes"
        skills = home / "skills" / "outreachmagic"
        (skills / "config").mkdir(parents=True)
        (skills / "scripts").mkdir(parents=True)
        (skills / "scripts" / "pipeline.py").write_text("# test stub\n")
        _stub_agent_secrets_cloud(skills)
        (skills / "config" / "agent_secrets.env").write_text("# empty\n")

        os.environ.pop("OM_ALLOW_LOCAL_API_KEYS", None)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["TRYKITT_API_KEY"] = "stale-shell-key"
        cc._AGENT_ENV_LOADED = False
        skill_dir = home / "skills" / "email-finder"
        skill_dir.mkdir(parents=True)
        cc.ensure_agent_env_loaded(skill_dir, reload=True)
        assert not os.environ.get("TRYKITT_API_KEY")


def test_companion_api_key_source_reports_agent_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes"
        skills = home / "skills" / "outreachmagic"
        (skills / "config").mkdir(parents=True)
        (skills / "scripts").mkdir(parents=True)
        (skills / "scripts" / "pipeline.py").write_text("# test stub\n")
        _stub_agent_secrets_cloud(skills)
        (skills / "config" / "agent_secrets.env").write_text("ICYPEAS_API_KEY=portal-key\n")
        os.environ.pop("OM_ALLOW_LOCAL_API_KEYS", None)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["ICYPEAS_API_KEY"] = "portal-key"
        skill_dir = home / "skills" / "email-finder"
        skill_dir.mkdir(parents=True)
        assert cc.companion_api_key_source("ICYPEAS_API_KEY", skill_dir) == "agent_secrets"
