#!/usr/bin/env python3
"""Generate skills/lead-enrich/update-manifest.json with SHA256 checksums."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "lead-enrich"
MANIFEST_FILES = (
    "SKILL.md",
    "README.md",
    "SECURITY.md",
    "config.example.json",
    "default.env",
    ".gitignore",
    "references/email-finder.md",
    "scripts/companion_common.py",
    "scripts/enrich.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_version() -> str:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([^\s]+)\s*$", text, flags=re.M)
    if not match:
        raise SystemExit("missing version: in skills/lead-enrich/SKILL.md")
    return match.group(1).strip()


def main() -> None:
    version = skill_version()
    files: dict[str, str] = {}
    for rel in MANIFEST_FILES:
        path = SKILL / rel
        if not path.exists():
            raise SystemExit(f"missing file for manifest: {path}")
        files[rel] = sha256_file(path)
    manifest = {"version": version, "files": files}
    out = SKILL / "update-manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} for version {version}")


if __name__ == "__main__":
    main()
