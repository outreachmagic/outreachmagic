"""Ensure update-manifest lists every pipeline.py dependency module."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"


class TestUpdateManifest(unittest.TestCase):
    def test_manifest_includes_pipeline_imports(self):
        pipeline = SCRIPTS / "pipeline.py"
        tree = ast.parse(pipeline.read_text(encoding="utf-8"))
        local_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    if (SCRIPTS / f"{name}.py").is_file():
                        local_imports.add(f"{name}.py")
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = node.module.split(".")[0]
                if (SCRIPTS / f"{base}.py").is_file():
                    local_imports.add(f"{base}.py")

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        files = set(manifest.get("files") or {})
        missing = sorted(local_imports - files)
        self.assertEqual(
            missing,
            [],
            f"update-manifest.json missing: {missing}",
        )

    def test_manifest_files_exist_on_disk(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name in manifest.get("files") or {}:
            if name == "SKILL.md":
                path = ROOT / "skills" / "outreachmagic" / "SKILL.md"
            else:
                path = SCRIPTS / name
            self.assertTrue(path.is_file(), f"missing manifest file: {path}")
