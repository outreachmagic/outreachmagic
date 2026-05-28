"""Tests for om_paths shared data root (Hermes profiles, Cursor, Claude)."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OM_PATHS_SRC = ROOT / "skills" / "outreachmagic" / "scripts" / "om_paths.py"


def _load_om_paths_from_scripts_dir(scripts_dir: Path):
    """Import om_paths as if installed under scripts_dir (fresh module per layout)."""
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

    def test_hermes_profile_install_uses_shared_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = (
                Path(tmp)
                / ".hermes"
                / "profiles"
                / "client-a"
                / "skills"
                / "outreachmagic"
                / "scripts"
            )
            om = _load_om_paths_from_scripts_dir(scripts)
            shared = Path(tmp) / ".hermes"
            self.assertEqual(om._data_root_from_scripts_dir(scripts).resolve(), shared.resolve())
            self.assertEqual(om.get_install_dir().resolve(), scripts.parent.resolve())
            self.assertEqual(om.get_skill_home().resolve(), (shared / "skills" / "outreachmagic").resolve())
            self.assertEqual(
                om.get_db_path().resolve(),
                (shared / "skills" / "outreachmagic" / "databases" / "outreachmagic.db").resolve(),
            )
            profile_db = scripts.parent / "databases" / "outreachmagic.db"
            self.assertNotEqual(om.get_db_path().resolve(), profile_db.resolve())

    def test_cursor_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ".cursor" / "skills" / "outreachmagic" / "scripts"
            om = _load_om_paths_from_scripts_dir(scripts)
            self.assertEqual(om._data_root_from_scripts_dir(scripts).resolve(), (Path(tmp) / ".cursor").resolve())


if __name__ == "__main__":
    unittest.main()
