"""Ensure pipeline update installs every file listed in update-manifest.json."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from generate_skill_manifest import generate_manifest  # noqa: E402
from skill_suite import manifest_relative_paths  # noqa: E402


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
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest_relative_paths("outreachmagic")),
            set(manifest.get("files", {})),
        )
        generated = generate_manifest("outreachmagic")
        self.assertEqual(generated["files"], manifest["files"])


if __name__ == "__main__":
    unittest.main()
