"""Ensure pipeline update installs every file listed in update-manifest.json."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"
GEN = ROOT / "scripts" / "generate-update-manifest.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


def _load_manifest_generator():
    spec = importlib.util.spec_from_file_location("gen_update_manifest", GEN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class UpdateManifestSyncTests(unittest.TestCase):
    def test_update_script_files_include_every_script_module(self):
        all_py = {p.name for p in SCRIPTS.glob("*.py")}
        self.assertEqual(set(om.UPDATE_SCRIPT_FILES), all_py)

    def test_manifest_lists_every_script_module(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest_scripts = {
            name for name in manifest.get("files", {})
            if name.endswith(".py")
        }
        all_py = {p.name for p in SCRIPTS.glob("*.py")}
        self.assertEqual(manifest_scripts, all_py)

    def test_update_download_names_uses_manifest(self):
        manifest = {
            "files": {
                "pipeline.py": "abc",
                "pipeline_lead_review.py": "def",
                "review_cloud.py": "ghi",
                "pipeline_dedup.py": "jkl",
                "VERSION": "mno",
                "SKILL.md": "pqr",
            },
        }
        names = om.update_download_names(manifest)
        self.assertIn("pipeline_lead_review.py", names)
        self.assertIn("review_cloud.py", names)
        self.assertIn("pipeline_dedup.py", names)
        self.assertNotIn("SKILL.md", names)

    def test_manifest_generator_matches_on_disk(self):
        gen = _load_manifest_generator()
        self.assertEqual(
            set(gen.manifest_file_names()),
            set(json.loads(MANIFEST.read_text(encoding="utf-8")).get("files", {})),
        )


if __name__ == "__main__":
    unittest.main()
