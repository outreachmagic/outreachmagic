#!/usr/bin/env python3
"""Generate update-manifest.json for any skill in skill-suite.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from skill_suite import (  # noqa: E402
    load_suite,
    manifest_relative_paths,
    read_skill_version,
    skill_def,
    skill_dir,
    skill_names,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_manifest_path(skill_name: str, rel: str) -> Path:
    base = skill_dir(skill_name)
    cfg = skill_def(skill_name)
    extra = set(cfg.get("extra_files") or [])
    if cfg.get("layout") == "flat_scripts" and rel not in ("SKILL.md", "VERSION") and rel not in extra and "/" not in rel:
        return base / "scripts" / rel
    if rel == "VERSION":
        return base / "scripts" / "VERSION"
    return base / rel


def generate_manifest(skill_name: str) -> dict:
    version = read_skill_version(skill_name)
    files: dict[str, str] = {}
    for rel in manifest_relative_paths(skill_name):
        path = resolve_manifest_path(skill_name, rel)
        if not path.is_file():
            raise SystemExit(f"missing file for {skill_name} manifest: {path}")
        files[rel] = sha256_file(path)
    return {"version": version, "files": files}


def write_manifest(skill_name: str, *, quiet: bool = False) -> Path:
    manifest = generate_manifest(skill_name)
    out = skill_dir(skill_name) / "update-manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if not quiet:
        print(f"Wrote {out} for {skill_name} version {manifest['version']}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate skill update-manifest.json from skill-suite.json")
    parser.add_argument("skill", nargs="?", help="Skill name (e.g. outreachmagic, email-finder)")
    parser.add_argument("--all", action="store_true", help="Regenerate manifests for every skill in skill-suite.json")
    parser.add_argument("--list-paths", action="store_true", help="Print manifest paths for a skill (debug)")
    args = parser.parse_args()

    suite = load_suite()
    names = skill_names(suite) if args.all else [args.skill] if args.skill else []
    if not names:
        parser.error("provide a skill name or --all")

    for name in names:
        if name not in suite["skills"]:
            raise SystemExit(f"unknown skill: {name}")
        if args.list_paths:
            for p in manifest_relative_paths(name, suite):
                print(p)
            continue
        write_manifest(name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
