#!/usr/bin/env python3
"""Install/update contract tests — skill-suite.json is the single source of truth."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from generate_skill_manifest import generate_manifest  # noqa: E402
from skill_suite import (  # noqa: E402
    install_default_tag,
    install_required_paths,
    load_suite,
    manifest_relative_paths,
    skill_dir,
)


def test_skill_suite_json_loads():
    suite = load_suite()
    assert "outreachmagic" in suite["skills"]
    assert "email-finder" in suite["skills"]
    assert install_default_tag("email-finder") == suite["skills"]["email-finder"]["install_default_tag"]
    assert install_default_tag("lead-enrich") == suite["skills"]["lead-enrich"]["install_default_tag"]


def test_install_sh_resolves_companion_tags_from_skill_suite():
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    companions = (ROOT / "platforms/common/install-companions.sh").read_text(encoding="utf-8")
    assert "_resolve_companion_tag" in install_sh
    assert "install_default_tag" in install_sh
    assert "_resolve_companion_tag lead-enrich" in install_sh
    assert "_resolve_companion_tag email-finder" in install_sh
    assert "install-tag" in companions
    suite = load_suite()
    assert install_default_tag("lead-enrich") == suite["skills"]["lead-enrich"]["install_default_tag"]
    assert install_default_tag("email-finder") == suite["skills"]["email-finder"]["install_default_tag"]


def test_install_companions_reads_install_required_from_skill_suite():
    text = (ROOT / "platforms/common/install-companions.sh").read_text(encoding="utf-8")
    assert "install-required" in text
    assert "_verify_companion_install" in text


@pytest.mark.parametrize("skill", ["email-finder", "lead-enrich"])
def test_install_required_cli_matches_skill_suite(skill):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "skill_suite.py"), "install-required", skill],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    assert paths == list(install_required_paths(skill))


def test_companion_manifest_validator_passes():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate-companion-manifests.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_publish_outreachmagic_includes_install_helpers():
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "dist/install.sh" in text
    assert "dist/SHA256SUMS" in text


@pytest.mark.parametrize("skill", ["email-finder", "lead-enrich"])
def test_companion_manifest_matches_generator(skill):
    committed = json.loads((skill_dir(skill) / "update-manifest.json").read_text(encoding="utf-8"))
    generated = generate_manifest(skill)
    assert set(committed["files"]) == set(generated["files"])
    assert committed["files"] == generated["files"]


@pytest.mark.parametrize("skill,entry,exclude", [
    ("email-finder", "email_finder", {"run_v22_tests.py"}),
    ("lead-enrich", "enrich", set()),
])
def test_companion_import_graph_in_manifest(skill, entry, exclude):
    scripts_dir = skill_dir(skill) / "scripts"
    expected = set(manifest_relative_paths(skill))
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
    stack = [entry]
    while stack:
        mod = stack.pop()
        if mod in reachable or mod not in local_modules:
            continue
        reachable.add(mod)
        for dep in _imports_from(scripts_dir / f"{mod}.py"):
            if dep in local_modules:
                stack.append(dep)

    for mod in sorted(reachable):
        rel = f"scripts/{mod}.py"
        assert rel in expected, f"{skill}: import graph needs {rel} — add scripts/*.py or adjust skill-suite.json exclude list"


def test_local_email_finder_install_copy_has_required_scripts():
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "skills" / "email-finder"
    src = skill_dir("email-finder")
    dest.mkdir(parents=True)

    for item in (
        "SKILL.md", "README.md", "SECURITY.md", "config.example.json",
        "default.env", ".gitignore", "references", "scripts", "update-manifest.json",
    ):
        if (src / item).exists():
            subprocess.run(["cp", "-a", str(src / item), str(dest / item)], check=True)

    for rel in load_suite()["skills"]["email-finder"]["install_required"]:
        assert (dest / rel).is_file(), f"missing after local install copy: {rel}"

    sys.path.insert(0, str(dest / "scripts"))
    import importlib
    importlib.import_module("batch_runner")


def test_outreachmagic_manifest_covers_all_scripts():
    scripts = skill_dir("outreachmagic") / "scripts"
    manifest = json.loads((skill_dir("outreachmagic") / "update-manifest.json").read_text(encoding="utf-8"))
    manifest_py = {n for n in manifest.get("files", {}) if n.endswith(".py")}
    on_disk = {p.name for p in scripts.glob("*.py")}
    assert manifest_py == on_disk


def test_companion_clis_have_no_update_files_tuple():
    for skill, cli in [("email-finder", "email_finder.py"), ("lead-enrich", "enrich.py")]:
        text = (skill_dir(skill) / "scripts" / cli).read_text(encoding="utf-8")
        assert "UPDATE_FILES" not in text, f"{skill} still defines UPDATE_FILES — use manifest keys only"


def test_outreachmagic_public_readme_is_single_source():
    """Public repo + org profile publish from skills/outreachmagic/README.md only."""
    canonical = skill_dir("outreachmagic") / "README.md"
    assert canonical.is_file()
    stale = ROOT / "platforms" / "outreachmagic-README.md"
    assert not stale.is_file(), f"remove {stale}; edit {canonical} only"
