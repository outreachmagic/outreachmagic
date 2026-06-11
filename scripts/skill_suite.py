#!/usr/bin/env python3
"""Load skill-suite.json — single source for install pins, manifest layout, and release metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "skill-suite.json"


def load_suite(path: Path | None = None) -> dict[str, Any]:
    p = path or SUITE_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def skill_names(suite: dict[str, Any] | None = None) -> list[str]:
    return list((suite or load_suite())["skills"].keys())


def skill_def(name: str, suite: dict[str, Any] | None = None) -> dict[str, Any]:
    skills = (suite or load_suite())["skills"]
    if name not in skills:
        raise KeyError(f"unknown skill in skill-suite.json: {name}")
    return skills[name]


def skill_dir(name: str, suite: dict[str, Any] | None = None) -> Path:
    rel = skill_def(name, suite)["path"]
    return ROOT / rel


def read_skill_version(name: str, suite: dict[str, Any] | None = None) -> str:
    cfg = skill_def(name, suite)
    base = skill_dir(name, suite)
    ver = cfg["version"]
    if ver["source"] == "file":
        return (base / ver["path"]).read_text(encoding="utf-8").strip()
    if ver["source"] == "skill_md":
        text = (base / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"^version:\s*([^\s]+)\s*$", text, flags=re.M)
        if not match:
            raise ValueError(f"missing version: in {base / 'SKILL.md'}")
        return match.group(1).strip()
    raise ValueError(f"unknown version source: {ver}")


def manifest_relative_paths(name: str, suite: dict[str, Any] | None = None) -> tuple[str, ...]:
    """All paths written to update-manifest.json for a skill (sorted, unique)."""
    cfg = skill_def(name, suite)
    base = skill_dir(name, suite)
    exclude = set(cfg.get("script_exclude") or [])
    scripts_dir = base / "scripts"
    layout = cfg.get("layout", "companion")

    script_paths: list[str] = []
    for py in sorted(scripts_dir.glob("*.py")):
        if py.name in exclude:
            continue
        if layout == "flat_scripts":
            script_paths.append(py.name)
        else:
            script_paths.append(f"scripts/{py.name}")

    extra = list(cfg.get("extra_files") or [])
    if layout == "flat_scripts":
        paths = script_paths + extra
        if "VERSION" not in paths:
            paths.append("VERSION")
    else:
        paths = extra + script_paths

    seen: set[str] = set()
    out: list[str] = []
    for rel in paths:
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return tuple(sorted(out, key=lambda p: (0 if p.startswith("scripts/") else 1, p)))


def install_default_tag(name: str, suite: dict[str, Any] | None = None) -> str:
    tag = skill_def(name, suite).get("install_default_tag") or ""
    if not tag:
        raise KeyError(f"skill {name} has no install_default_tag in skill-suite.json")
    return tag


def install_required_paths(name: str, suite: dict[str, Any] | None = None) -> tuple[str, ...]:
    return tuple(skill_def(name, suite).get("install_required") or [])


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="skill-suite.json helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    tag_p = sub.add_parser("install-tag", help="Print default install git tag for a companion skill")
    tag_p.add_argument("skill")

    paths_p = sub.add_parser("manifest-paths", help="Print manifest paths for a skill")
    paths_p.add_argument("skill")

    req_p = sub.add_parser("install-required", help="Print post-install required paths for a skill")
    req_p.add_argument("skill")

    args = parser.parse_args(argv)
    if args.command == "install-tag":
        print(install_default_tag(args.skill))
    elif args.command == "manifest-paths":
        for path in manifest_relative_paths(args.skill):
            print(path)
    elif args.command == "install-required":
        for path in install_required_paths(args.skill):
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
