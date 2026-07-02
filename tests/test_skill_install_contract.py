#!/usr/bin/env python3
"""Install/update contract tests — skill-suite.json is the single source of truth."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from generate_skill_manifest import generate_manifest  # noqa: E402
from skill_suite import (  # noqa: E402
    load_suite,
    manifest_relative_paths,
    skill_dir,
)


def test_skill_suite_json_loads():
    suite = load_suite()
    assert "outreachmagic" in suite["skills"]
    assert len(suite["skills"]) == 1


def test_publish_outreachmagic_includes_install_helpers():
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "dist/install.sh" in text
    assert "dist/SHA256SUMS" in text


def test_outreachmagic_manifest_matches_generator():
    committed = json.loads((skill_dir("outreachmagic") / "update-manifest.json").read_text(encoding="utf-8"))
    generated = generate_manifest("outreachmagic")
    assert set(committed["files"]) == set(generated["files"])
    assert committed["files"] == generated["files"]


def test_outreachmagic_manifest_covers_all_scripts():
    scripts = skill_dir("outreachmagic") / "scripts"
    manifest = json.loads((skill_dir("outreachmagic") / "update-manifest.json").read_text(encoding="utf-8"))
    manifest_py = {n for n in manifest.get("files", {}) if n.endswith(".py")}
    on_disk = {p.name for p in scripts.glob("*.py")}
    assert manifest_py == on_disk


def test_outreachmagic_import_graph_in_manifest():
    """Every module reachable from pipeline.py must be in the manifest."""
    import ast

    scripts_dir = skill_dir("outreachmagic") / "scripts"
    expected = set(manifest_relative_paths("outreachmagic"))
    exclude = set()
    local_modules = {p.stem for p in scripts_dir.glob("*.py") if p.name not in exclude}

    def _imports_from(path: Path) -> set[str]:
        out: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module.split(".")[0])
        return out

    reachable: set[str] = set()
    stack = ["pipeline"]
    while stack:
        mod = stack.pop()
        if mod in reachable or mod not in local_modules:
            continue
        reachable.add(mod)
        for dep in _imports_from(scripts_dir / f"{mod}.py"):
            if dep in local_modules:
                stack.append(dep)

    for mod in sorted(reachable):
        rel = f"{mod}.py"
        assert rel in expected, f"import graph needs {rel} — add to script_exclude or regenerate manifest"


def test_clis_have_no_update_files_tuple():
    """CLI entry points use manifest keys, not hand-maintained UPDATE_FILES."""
    for cli in ("email_finder.py", "enrich.py", "pipeline.py"):
        text = (skill_dir("outreachmagic") / "scripts" / cli).read_text(encoding="utf-8")
        assert "UPDATE_FILES" not in text, f"{cli} still defines UPDATE_FILES — use manifest keys only"


def test_outreachmagic_public_readme_is_single_source():
    """Public repo + org profile publish from skills/outreachmagic/README.md only."""
    canonical = skill_dir("outreachmagic") / "README.md"
    assert canonical.is_file()
    stale = ROOT / "platforms" / "outreachmagic-README.md"
    assert not stale.is_file(), f"remove {stale}; edit {canonical} only"
