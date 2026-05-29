"""Tests for om_paths (Hermes global install + profile symlinks, Cursor)."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OM_PATHS_SRC = ROOT / "skills" / "outreachmagic" / "scripts" / "om_paths.py"


def _load_om_paths_from_scripts_dir(scripts_dir: Path):
    target = scripts_dir / "om_paths.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(OM_PATHS_SRC.read_text())
    spec = importlib.util.spec_from_file_location(
        f"om_paths_{scripts_dir.as_posix().replace('/', '_')}",
        target,
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestOmPathsDataRoot(unittest.TestCase):
    def test_global_hermes_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".hermes" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            data_root = Path(tmp) / ".hermes"
            self.assertEqual(om._data_root_from_scripts_dir(scripts).resolve(), data_root.resolve())
            self.assertEqual(
                om.get_db_path().resolve(),
                (data_root / "skills" / "outreachmagic" / "databases" / "outreachmagic.db").resolve(),
            )

    def test_hermes_profile_symlink_resolves_to_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_skill = root / ".hermes" / "skills" / "outreachmagic"
            global_scripts = global_skill / "scripts"
            global_scripts.mkdir(parents=True)
            (global_skill / "SKILL.md").write_text("x")

            prof_skills = root / ".hermes" / "profiles" / "popcam" / "skills"
            prof_skills.mkdir(parents=True)
            os.symlink("../../../skills/outreachmagic", prof_skills / "outreachmagic")

            profile_scripts = (prof_skills / "outreachmagic" / "scripts").resolve()
            om = _load_om_paths_from_scripts_dir(profile_scripts)
            data_root = root / ".hermes"
            self.assertEqual(om._data_root_from_scripts_dir(profile_scripts).resolve(), data_root.resolve())
            self.assertEqual(om.get_install_dir().resolve(), global_skill.resolve())
            self.assertEqual(
                om.get_db_path().resolve(),
                (data_root / "skills" / "outreachmagic" / "databases" / "outreachmagic.db").resolve(),
            )

    def test_cursor_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            self.assertEqual(om._data_root_from_scripts_dir(scripts).resolve(), (Path(tmp) / ".cursor").resolve())


if __name__ == "__main__":
    unittest.main()
