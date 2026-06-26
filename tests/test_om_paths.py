"""Tests for om_paths (Hermes global install + profile symlinks, Cursor)."""

from __future__ import annotations

import importlib.util
import json
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

    def test_cursor_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            self.assertEqual(om._data_root_from_scripts_dir(scripts).resolve(), (Path(tmp) / ".cursor").resolve())

    def test_profile_copy_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / ".hermes" / "profiles" / "popcam" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            warn = om.hermes_profile_copy_warning()
            self.assertIsNotNone(warn)
            self.assertIn("symlink", warn.lower())

    def test_symlinked_profile_no_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_scripts = root / ".hermes" / "skills" / "outreachmagic" / "scripts"
            global_scripts.mkdir(parents=True)
            prof_skills = root / ".hermes" / "profiles" / "popcam" / "skills"
            prof_skills.mkdir(parents=True)
            os.symlink("../../../skills/outreachmagic", prof_skills / "outreachmagic")
            profile_scripts = (prof_skills / "outreachmagic" / "scripts").resolve()
            om = _load_om_paths_from_scripts_dir(profile_scripts)
            self.assertIsNone(om.hermes_profile_copy_warning())

    def test_data_root_override_wins(self):
        """_DATA_ROOT_OVERRIDE takes highest priority."""
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            om.set_data_root_override(Path(tmp) / ".custom")
            self.assertEqual(om.get_data_root(), Path(tmp) / ".custom")

    def test_data_root_env_var(self):
        """OUTREACHMAGIC_DATA_ROOT env var takes priority over config."""
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            os.environ["OUTREACHMAGIC_DATA_ROOT"] = str(Path(tmp) / ".env-override")
            try:
                self.assertEqual(om.get_data_root(), Path(tmp) / ".env-override")
            finally:
                os.environ.pop("OUTREACHMAGIC_DATA_ROOT", None)

    def test_data_root_config_field(self):
        """Config data_root field is used when no env var or override."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Write a config with data_root set
            cfg_dir = root / ".cursor" / "skills" / "outreachmagic" / "config"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "outreachmagic_config.json").write_text(
                json.dumps({"data_root": str(root / ".custom-root")})
            )
            scripts = root / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            self.assertEqual(om.get_data_root(), root / ".custom-root")

    def test_data_root_falls_back_to_script_location(self):
        """When no override, env var, or config data_root, use script-location inference."""
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            expected_inferred = om._read_bootstrap_data_root(om.DEFAULT_DATA_ROOT)
            self.assertEqual(om.get_data_root(), expected_inferred)

    def test_get_db_path_default(self):
        """DB path is under skill_home/databases/."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Set up a config with explicit data_root so we get deterministic paths
            cfg_dir = root / ".cursor" / "skills" / "outreachmagic" / "config"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "outreachmagic_config.json").write_text(
                json.dumps({"data_root": str(root / ".cursor")})
            )
            scripts = root / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            expected_db = root / ".cursor" / "skills" / "outreachmagic" / "databases" / "outreachmagic.db"
            self.assertEqual(om.get_db_path(), expected_db)

    def test_check_duplicate_installs_returns_list(self):
        """check_duplicate_installs returns a list (possibly empty)."""
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            result = om.check_duplicate_installs()
            self.assertIsInstance(result, list)
            for entry in result:
                self.assertIn("path", entry)
                self.assertIn("is_symlink", entry)


if __name__ == "__main__":
    unittest.main()
