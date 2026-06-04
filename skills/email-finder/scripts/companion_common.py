"""Shared helpers for outreachmagic companion skills (lead-enrich, email-finder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

OUTREACHMAGIC_NAME = "outreachmagic"

SKILL_SEARCH_PATHS = [
    Path.home() / ".hermes" / "skills",
    Path.home() / ".cursor" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".openclaw" / "skills",  # OpenClaw managed/local skills dir
]

_AGENT_ENV_LOADED = False


def agent_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


_API_KEY_VARS = frozenset({
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
    "OUTREACHMAGIC_AGENT_KEY",
})


def _env_value_empty(key: str) -> bool:
    val = os.environ.get(key, "")
    if not val or not str(val).strip():
        return True
    if str(val).strip() in ("***", "changeme", "your_key_here"):
        return True
    return False


def load_dotenv_file(path: Path, *, force_api_keys: bool = False) -> None:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return
    for line in text.splitlines():
        parsed = parse_dotenv_line(line)
        if not parsed:
            continue
        key, value = parsed
        if force_api_keys and key in _API_KEY_VARS:
            if (
                value
                and value.strip() not in ("***", "changeme", "your_key_here")
                and _env_value_empty(key)
            ):
                os.environ[key] = value
            continue
        if key not in os.environ:
            os.environ[key] = value


def active_profile() -> Optional[str]:
    """Hermes profile slug from HERMES_PROFILE env (per-profile .env overrides)."""
    return (os.environ.get("HERMES_PROFILE") or "").strip() or None


def _monorepo_dotenv(skill_dir: Optional[Path]) -> Optional[Path]:
    """Dev checkout: skills/<name>/ → repo root .env (install.sh sibling)."""
    if not skill_dir:
        return None
    root = skill_dir.resolve().parent.parent
    env_file = root / ".env"
    if env_file.is_file() and (root / "install.sh").is_file():
        return env_file
    return None


def ensure_agent_env_loaded(skill_dir: Optional[Path] = None, *, reload: bool = False) -> None:
    global _AGENT_ENV_LOADED
    if _AGENT_ENV_LOADED and not reload:
        return
    home = agent_home()
    for name in (".env", "default.env"):
        load_dotenv_file(home / name, force_api_keys=True)
    profile = active_profile()
    if profile:
        load_dotenv_file(home / "profiles" / profile / ".env", force_api_keys=True)
    repo_env = _monorepo_dotenv(skill_dir)
    if repo_env:
        load_dotenv_file(repo_env, force_api_keys=True)
    if skill_dir:
        load_dotenv_file(skill_dir / "default.env", force_api_keys=True)
    _AGENT_ENV_LOADED = True


def subprocess_env(skill_dir: Optional[Path] = None) -> dict[str, str]:
    ensure_agent_env_loaded(skill_dir)
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


def skill_dir_from_script(script_file: str) -> Path:
    return Path(script_file).resolve().parent.parent


def find_outreachmagic(
    config: dict[str, Any],
    skill_dir: Optional[Path] = None,
) -> Optional[Path]:
    if config.get("outreachmagic_home"):
        home = Path(config["outreachmagic_home"]).expanduser()
        if (home / "scripts" / "pipeline.py").exists():
            return home
        return None
    if skill_dir:
        sibling = skill_dir.parent / OUTREACHMAGIC_NAME
        if (sibling / "scripts" / "pipeline.py").exists():
            return sibling
    for skills_dir in SKILL_SEARCH_PATHS:
        candidate = skills_dir / OUTREACHMAGIC_NAME
        if (candidate / "scripts" / "pipeline.py").exists():
            return candidate
    return None


def get_pipeline_path(om_dir: Path) -> Path:
    return om_dir / "scripts" / "pipeline.py"


def outreachmagic_agent_key_status(om_dir: Optional[Path]) -> tuple[bool, str]:
    env_key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if env_key:
        return True, "env"
    if not om_dir:
        return False, "missing"
    cfg_path = om_dir / "config" / "outreachmagic_config.json"
    try:
        payload = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False, "missing"
    if isinstance(payload, dict) and str(payload.get("agent_key", "")).strip():
        return True, "outreachmagic_config"
    return False, "missing"


def history_lookup(
    om_dir: Path,
    extra_args: list[str],
    *,
    workspace: Optional[str] = None,
    timeout: int = 10,
    skill_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    pipeline = str(get_pipeline_path(om_dir))
    base_args = ["history", "--json", "--limit", "0"]
    if workspace:
        base_args.extend(["--workspace", workspace])
    try:
        proc = subprocess.run(
            [sys.executable, pipeline, *base_args, *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(skill_dir),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, dict) and data.get("error"):
            return None
        if isinstance(data, dict) and data.get("lead"):
            return data["lead"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    return None


def run_import_profiles(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str = "",
    overwrite: bool = False,
    source_detail: str = "companion",
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "import-profiles",
        "--json",
        json.dumps(profiles),
        "--source-detail",
        source_detail,
    ]
    if workspace:
        cmd.extend(["--workspace", workspace])
    if overwrite:
        cmd.append("--overwrite")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_batch_lead_lookup(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    workspace: str = "",
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Single subprocess: pipeline.py batch-lead-lookup."""
    if not items:
        return {"status": "ok", "results": []}
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "batch-lead-lookup",
        "--json",
        json.dumps(items),
    ]
    if workspace:
        cmd.extend(["--workspace", workspace])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_verification_candidates(
    om_dir: Path,
    workspace: str,
    *,
    max_age_days: int = 30,
    skip_mv_days: int = 7,
    limit: int = 5000,
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "verification-candidates",
        "--workspace",
        workspace,
        "--max-age",
        str(max_age_days),
        "--skip-mv-days",
        str(skip_mv_days),
        "--limit",
        str(limit),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_verify_email_batch(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not items:
        return {"status": "batch_recorded", "recorded": 0, "errors": []}
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "verify-email",
        "--batch",
        "--json",
        json.dumps(items),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}
