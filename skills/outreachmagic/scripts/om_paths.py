"""
Skill install paths. Infers data root from script location; falls back to ~/.hermes.
Optional data_root override in config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

SKILL_NAME = "outreachmagic"


def _infer_data_root() -> Path:
    """Derive data root from where this script actually lives on disk.
    ~/.hermes/skills/outreachmagic/scripts/om_paths.py -> ~/.hermes
    ~/.cursor/skills/outreachmagic/scripts/om_paths.py -> ~/.cursor
    ~/.claude/skills/outreachmagic/scripts/om_paths.py -> ~/.claude
    """
    scripts_dir = Path(__file__).resolve().parent
    skill_dir = scripts_dir.parent
    skills_dir = skill_dir.parent
    if skills_dir.name == "skills" and skill_dir.name == SKILL_NAME:
        return skills_dir.parent
    return Path.home() / ".hermes"


DEFAULT_DATA_ROOT = _infer_data_root()

_DATA_ROOT_OVERRIDE: Optional[Path] = None


def set_data_root_override(path: Optional[Path]) -> None:
    """Tests only: redirect all paths before importing pipeline."""
    global _DATA_ROOT_OVERRIDE
    _DATA_ROOT_OVERRIDE = path


def _read_bootstrap_data_root(default_root: Path) -> Path:
    cfg_path = default_root / "skills" / SKILL_NAME / "config" / "outreachmagic_config.json"
    if not cfg_path.exists():
        return default_root
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return default_root
    raw = (cfg.get("data_root") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_root


def get_data_root() -> Path:
    if _DATA_ROOT_OVERRIDE is not None:
        return _DATA_ROOT_OVERRIDE
    return _read_bootstrap_data_root(DEFAULT_DATA_ROOT)


def get_skill_home() -> Path:
    return get_data_root() / "skills" / SKILL_NAME


def get_config_path() -> Path:
    return get_skill_home() / "config" / "outreachmagic_config.json"


def get_db_path() -> Path:
    return get_skill_home() / "databases" / "outreachmagic.db"
