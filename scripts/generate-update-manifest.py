#!/usr/bin/env python3
"""Generate skills/outreachmagic/update-manifest.json with SHA256 checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "outreachmagic"
SCRIPTS = SKILL / "scripts"


def manifest_file_names() -> tuple[str, ...]:
    """All installable skill files: every scripts/*.py plus VERSION and SKILL.md."""
    scripts = tuple(sorted(p.name for p in SCRIPTS.glob("*.py")))
    return (*scripts, "VERSION", "SKILL.md")


MANIFEST_FILES = manifest_file_names()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    version = (SCRIPTS / "VERSION").read_text().strip()
    files: dict[str, str] = {}
    for name in MANIFEST_FILES:
        path = SCRIPTS / name if name != "SKILL.md" else SKILL / "SKILL.md"
        if not path.exists():
            raise SystemExit(f"missing file for manifest: {path}")
        files[name] = sha256_file(path)

    manifest = {"version": version, "files": files}
    out = SKILL / "update-manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {out} for version {version}")


if __name__ == "__main__":
    main()
