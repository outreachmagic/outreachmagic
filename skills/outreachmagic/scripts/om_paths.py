"""
Skill install paths. Resolves ~/.hermes (or ~/.cursor / ~/.claude) from scripts location.
Hermes: install real files under <home>/skills/outreachmagic/; profile dirs use symlinks only.

Working files (CSVs, exports): relative paths resolve under process cwd, or an optional
project_root in outreachmagic_config.json. Skill state (DB, config) stays under skill_home.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

SKILL_NAME = "outreachmagic"

_PROJECT_ROOT_OVERRIDE: Optional[Path] = None


def _data_root_from_scripts_dir(scripts_dir: Path) -> Path:
    """Map scripts/ to platform home (e.g. ~/.hermes/skills/outreachmagic/scripts → ~/.hermes)."""
    skill_dir = scripts_dir.parent
    skills_dir = skill_dir.parent
    if skills_dir.name == "skills" and skill_dir.name == SKILL_NAME:
        return skills_dir.parent
    return Path.home() / ".hermes"


def _infer_data_root() -> Path:
    return _data_root_from_scripts_dir(Path(__file__).resolve().parent)


def get_install_dir() -> Path:
    """Resolved skill directory (follows profile symlinks to ~/.hermes/skills/outreachmagic)."""
    return Path(__file__).resolve().parent.parent


def hermes_profile_copy_warning() -> Optional[str]:
    """Hermes only: install_dir under profiles/.../skills/outreachmagic means a full copy, not a symlink."""
    parts = get_install_dir().parts
    try:
        i = parts.index("profiles")
    except ValueError:
        return None
    if i + 3 >= len(parts) or parts[i + 2] != "skills" or parts[i + 3] != SKILL_NAME:
        return None
    return (
        "Profile has a full copy of outreachmagic (not a symlink to ~/.hermes/skills/outreachmagic). "
        "Re-run install: bash install.sh --platform hermes --all-profiles"
    )


DEFAULT_DATA_ROOT = _infer_data_root()

_DATA_ROOT_OVERRIDE: Optional[Path] = None
_DB_PATH_OVERRIDE: Optional[Path] = None


def set_data_root_override(path: Optional[Path]) -> None:
    """Tests only: redirect all paths before importing pipeline."""
    global _DATA_ROOT_OVERRIDE
    _DATA_ROOT_OVERRIDE = path


def set_working_root_override(path: Optional[Path]) -> None:
    """Tests only: redirect working file paths (input/export)."""
    global _PROJECT_ROOT_OVERRIDE
    _PROJECT_ROOT_OVERRIDE = path


def set_project_root_override(path: Optional[Path]) -> None:
    """Tests only: alias for set_working_root_override."""
    set_working_root_override(path)


def set_db_path_override(path: Optional[Path]) -> None:
    """Redirect SQLite file (refresh staging pull, tests)."""
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = path


def _read_bootstrap_config(default_root: Path) -> dict:
    cfg_path = default_root / "skills" / SKILL_NAME / "config" / "outreachmagic_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_bootstrap_data_root(default_root: Path) -> Path:
    raw = (_read_bootstrap_config(default_root).get("data_root") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_root


def get_data_root() -> Path:
    if _DATA_ROOT_OVERRIDE is not None:
        return _DATA_ROOT_OVERRIDE
    env_root = os.environ.get("OUTREACHMAGIC_DATA_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    return _read_bootstrap_data_root(DEFAULT_DATA_ROOT)


def get_skill_home() -> Path:
    return get_data_root() / "skills" / SKILL_NAME


def get_config_path() -> Path:
    return get_skill_home() / "config" / "outreachmagic_config.json"


def get_agent_secrets_path() -> Path:
    return get_config_path().parent / "agent_secrets.env"


def get_db_path() -> Path:
    if _DB_PATH_OVERRIDE is not None:
        return _DB_PATH_OVERRIDE
    return get_skill_home() / "databases" / "outreachmagic.db"


def _config_working_root() -> Optional[Path]:
    if _PROJECT_ROOT_OVERRIDE is not None:
        return _PROJECT_ROOT_OVERRIDE
    raw = (_read_bootstrap_config(get_data_root()).get("project_root") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return None


def get_working_root() -> Path:
    """Root for relative input/export paths: config project_root or process cwd."""
    configured = _config_working_root()
    if configured:
        return configured.resolve()
    return Path.cwd()


def get_input_dir() -> Path:
    return get_working_root() / "input"


def get_export_dir() -> Path:
    return get_working_root() / "export"


def resolve_project_path(
    path: str,
    *,
    kind: Literal["input", "export"] = "input",
    for_write: bool = False,
) -> Path:
    """Resolve a user path under working_root (cwd by default). Absolute paths pass through."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if p.is_absolute():
        if for_write:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    root = get_working_root()
    if raw.startswith("input/") or raw.startswith("export/"):
        resolved = (root / raw).resolve()
    else:
        base = get_input_dir() if kind == "input" else get_export_dir()
        resolved = (base / raw).resolve()
    if for_write:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
