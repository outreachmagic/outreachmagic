"""Subprocess smoke tests: pipeline.py must start (catches missing manifest modules)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
PIPELINE = SCRIPTS / "pipeline.py"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"


class PipelineImportSmokeTests(unittest.TestCase):
    def test_version_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(PIPELINE), "version"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_query_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(PIPELINE), "query", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("engagement", proc.stdout)
        self.assertIn("campaign-stats", proc.stdout)

    def test_import_profiles_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(PIPELINE), "import-profiles", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_manifest_script_files_exist(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        skill_root = ROOT / "skills" / "outreachmagic"
        for rel in data.get("files", {}):
            if rel == "SKILL.md":
                path = skill_root / "SKILL.md"
            else:
                path = SCRIPTS / rel
            self.assertTrue(path.is_file(), f"missing manifest file: {rel} -> {path}")


if __name__ == "__main__":
    unittest.main()
