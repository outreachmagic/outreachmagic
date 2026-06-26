#!/usr/bin/env python3
"""Pre-flight: companion manifests match skill-suite.json and on-disk tree."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from generate_skill_manifest import generate_manifest, resolve_manifest_path  # noqa: E402
from skill_suite import (  # noqa: E402
    install_default_tag,
    load_suite,
    manifest_relative_paths,
    skill_def,
    skill_dir,
    skill_names,
)

COMPANIONS = ("email-finder", "lead-enrich")


def _local_import_graph(scripts_dir: Path, entry: str, exclude: set[str]) -> set[str]:
    local_modules = {p.stem for p in scripts_dir.glob("*.py") if p.name not in exclude}

    def imports_from(path: Path) -> set[str]:
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
        for dep in imports_from(scripts_dir / f"{mod}.py"):
            if dep in local_modules:
                stack.append(dep)
    return reachable


def _validate_skill(name: str) -> list[str]:
    errors: list[str] = []
    cfg = skill_def(name)
    base = skill_dir(name)
    manifest_path = base / "update-manifest.json"
    expected_paths = set(manifest_relative_paths(name))
    exclude = set(cfg.get("script_exclude") or [])

    for rel in expected_paths:
        if not resolve_manifest_path(name, rel).is_file():
            errors.append(f"{name}: missing on-disk file {rel}")

    committed = json.loads(manifest_path.read_text(encoding="utf-8"))
    committed_keys = set(committed.get("files", {}))
    if committed_keys != expected_paths:
        errors.append(
            f"{name}: update-manifest.json keys mismatch "
            f"missing={sorted(expected_paths - committed_keys)} "
            f"extra={sorted(committed_keys - expected_paths)}"
        )

    generated = generate_manifest(name)
    if generated.get("files") != committed.get("files"):
        errors.append(f"{name}: run python3 scripts/generate_skill_manifest.py {name}")

    for rel in cfg.get("install_required") or []:
        if not (base / rel).is_file():
            errors.append(f"{name}: install-required file missing: {rel}")

    entry = "email_finder.py" if name == "email-finder" else "enrich.py"
    for mod in sorted(_local_import_graph(base / "scripts", entry.replace(".py", ""), exclude)):
        rel = f"scripts/{mod}.py"
        if rel not in expected_paths:
            errors.append(f"{name}: import graph requires {rel} but manifest generator omitted it")

    install_sh = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
    companions_sh = Path(__file__).resolve().parents[1] / "platforms/common/install-companions.sh"
    companions_text = companions_sh.read_text(encoding="utf-8")
    reads_tag = (
        "_resolve_companion_tag" in install_sh
        and "install_default_tag" in install_sh
        and f"_resolve_companion_tag {name}" in install_sh
        and "install-tag" in companions_text
    )
    if not reads_tag:
        errors.append(f"{name}: install.sh does not read install tag from skill-suite.json")

    return errors


def main() -> int:
    errors: list[str] = []
    for name in COMPANIONS:
        if name not in skill_names():
            errors.append(f"missing companion in skill-suite.json: {name}")
            continue
        errors.extend(_validate_skill(name))

    companions_sh = Path(__file__).resolve().parents[1] / "platforms/common/install-companions.sh"
    companions_text = companions_sh.read_text(encoding="utf-8")
    if "install-required" not in companions_text:
        errors.append("install-companions.sh must read install-required from skill_suite.py")

    publish_yml = Path(__file__).resolve().parents[1] / ".github/workflows/publish-email-finder.yml"
    if "cp skills/email-finder/scripts/*.py" not in publish_yml.read_text(encoding="utf-8"):
        errors.append("publish-email-finder.yml must copy scripts/*.py")

    suite_path = Path(__file__).resolve().parents[1] / "skill-suite.json"
    if not suite_path.is_file():
        errors.append("missing skill-suite.json at repo root")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    print("PASS: companion manifests match skill-suite.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
