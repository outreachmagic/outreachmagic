"""
Skill install paths. Resolves ~/.hermes (or ~/.cursor / ~/.claude) from scripts location.

Working files (CSVs, exports): under outreachmagic/ relative to project_root or cwd.
Skill state (DB, config) stays under skill_home.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

SKILL_NAME = "outreachmagic"

OM_SUBDIR = "outreachmagic"
IMPORTS_SUBDIR = "imports"
EXPORTS_SUBDIR = "exports"
BATCHES_SUBDIR = "batches"
SHEETS_SUBDIR = "sheets"
ARCHIVE_SUBDIR = "archive"
LOGS_SUBDIR = "logs"

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
    """Tests only: redirect working file paths."""
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
    """Root for outreachmagic/ working tree: config project_root or process cwd."""
    configured = _config_working_root()
    if configured:
        return configured.resolve()
    return Path.cwd()


def get_om_data_dir() -> Path:
    return get_working_root() / OM_SUBDIR


def get_input_dir() -> Path:
    return get_om_data_dir() / IMPORTS_SUBDIR


def get_export_dir() -> Path:
    return get_om_data_dir() / EXPORTS_SUBDIR


def get_batches_dir() -> Path:
    return get_om_data_dir() / BATCHES_SUBDIR


def get_sheets_dir() -> Path:
    return get_om_data_dir() / SHEETS_SUBDIR


def get_archive_dir() -> Path:
    return get_om_data_dir() / ARCHIVE_SUBDIR


def get_logs_dir() -> Path:
    return get_om_data_dir() / LOGS_SUBDIR


def working_paths_payload() -> dict[str, str]:
    """Resolved working-file directories for CLI output."""
    return {
        "working_root": str(get_working_root()),
        "om_data_dir": str(get_om_data_dir()),
        "imports": str(get_input_dir()),
        "exports": str(get_export_dir()),
        "batches": str(get_batches_dir()),
        "sheets": str(get_sheets_dir()),
        "archive": str(get_archive_dir()),
        "logs": str(get_logs_dir()),
    }


def resolve_project_path(
    path: str,
    *,
    kind: Literal["input", "export", "batches", "sheets", "archive", "logs"] = "input",
    for_write: bool = False,
) -> Path:
    """Resolve a user path under the outreachmagic working tree. Absolute paths pass through."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if p.is_absolute():
        if for_write:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p.resolve()

    root = get_working_root()
    om_prefix = f"{OM_SUBDIR}/"
    if raw.startswith(om_prefix):
        resolved = (root / raw).resolve()
    else:
        kind_dirs = {
            "input": get_input_dir(),
            "export": get_export_dir(),
            "batches": get_batches_dir(),
            "sheets": get_sheets_dir(),
            "archive": get_archive_dir(),
            "logs": get_logs_dir(),
        }
        base = kind_dirs[kind]
        resolved = (base / raw).resolve()
    if for_write:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def save_sheets_export_record(
    *,
    workspace: str,
    title: str,
    sheet_id: str,
    url: str = "",
    detail: str = "",
) -> Path:
    """Persist sheet export metadata under outreachmagic/sheets/."""
    from datetime import datetime, timezone

    sheets_dir = get_sheets_dir()
    sheets_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_ws = "".join(c if c.isalnum() or c in "-_" else "-" for c in workspace)[:40]
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in (title or "export"))[:40]
    out_path = sheets_dir / f"{safe_ws}-{safe_title}-{stamp}.json"
    payload = {
        "workspace": workspace,
        "title": title,
        "sheet_id": sheet_id,
        "url": url,
        "detail": detail,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path
