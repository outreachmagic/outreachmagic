"""
Skill install paths. Default data root ~/.hermes; optional data_root in config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

SKILL_NAME = "outreachmagic"
DEFAULT_DATA_ROOT = Path.home() / ".hermes"

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
