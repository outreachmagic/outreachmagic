#!/usr/bin/env python3
"""Sync release tag into install docs and validate secure-install patterns."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "skills" / "outreachmagic" / "scripts" / "VERSION"
SNIPPET = ROOT / "platforms" / "common" / "install-release.snippet.sh"

SYNC_FILES = [
    ROOT / "AGENTS-INSTALL.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "install.md",
    # install-companions.md removed in v1.0.0 (consolidated single skill)
    ROOT / "docs" / "hermes-skills-layout.md",
    ROOT / "docs" / "RELEASING.md",
    ROOT / "platforms" / "hermes" / "README.md",
    ROOT / "platforms" / "cursor" / "README.md",
    ROOT / "platforms" / "claude-code" / "README.md",
    ROOT / "skills" / "outreachmagic" / "SKILL.md",
    ROOT / "install.sh",
]

BROKEN_PATTERNS = [
    re.compile(r"om_install\.sh"),
    re.compile(r"om_SHA256SUMS"),
    re.compile(r"om_install\.sh.*shasum.*--check", re.S),
]

VERSION_IN_OM = re.compile(r"OM_VERSION=v[\d.]+")
VERSION_IN_URL = re.compile(
    r"(releases/download/)v[\d.]+(/install\.sh|/SHA256SUMS)"
)
VERSION_TAG_FLAG = re.compile(r"--tag v[\d.]+")
VERSION_EXAMPLE = re.compile(r"\(e\.g\. v[\d.]+\)")


def read_tag() -> str:
    ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    return f"v{ver}" if not ver.startswith("v") else ver


def sync_text(text: str, tag: str) -> str:
    text = VERSION_IN_OM.sub(f"OM_VERSION={tag}", text)
    text = VERSION_IN_URL.sub(rf"\1{tag}\2", text)
    text = VERSION_TAG_FLAG.sub(f"--tag {tag}", text)
    text = VERSION_EXAMPLE.sub(f"(e.g. {tag})", text)
    return text


def sync_snippet(tag: str) -> None:
    lines = SNIPPET.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("# Canonical"):
        body_start = 2
    else:
        body_start = 0
    header = SNIPPET.read_text(encoding="utf-8").splitlines()[:2]
    body = "\n".join(
        line for line in SNIPPET.read_text(encoding="utf-8").splitlines()[body_start:]
        if not line.startswith("OM_VERSION=")
    )
    SNIPPET.write_text(
        "\n".join(header + [f"OM_VERSION={tag}"] + body.splitlines()) + "\n",
        encoding="utf-8",
    )


def validate(text: str, path: Path) -> list[str]:
    errors: list[str] = []
    for pat in BROKEN_PATTERNS:
        if pat.search(text):
            errors.append(f"{path}: broken install pattern ({pat.pattern})")
    if "SHA256SUMS" in text and "shasum" in text and "INSTALL_DIR" not in text:
        if path.name != "sync_install_docs.py":
            errors.append(f"{path}: SHA256 verify should use INSTALL_DIR with install.sh filename")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate only; exit 1 on drift")
    args = parser.parse_args()
    tag = read_tag()
    errors: list[str] = []

    if not args.check:
        sync_snippet(tag)
        for path in SYNC_FILES:
            if not path.exists():
                continue
            updated = sync_text(path.read_text(encoding="utf-8"), tag)
            path.write_text(updated, encoding="utf-8")

    snippet_tag = None
    if SNIPPET.exists():
        for line in SNIPPET.read_text(encoding="utf-8").splitlines():
            if line.startswith("OM_VERSION="):
                snippet_tag = line.split("=", 1)[1].strip()
                break
        if snippet_tag != tag:
            errors.append(f"{SNIPPET}: OM_VERSION={snippet_tag!r} expected {tag!r}")

    for path in SYNC_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if f"OM_VERSION={tag}" not in text and "releases/download/" in text:
            if "OM_VERSION=" in text:
                errors.append(f"{path}: OM_VERSION does not match {tag}")
        errors.extend(validate(text, path))

    if "advice.detachedHead=false" not in (ROOT / "install.sh").read_text(encoding="utf-8"):
        errors.append("install.sh: missing advice.detachedHead=false on git clone")

    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print(f"sync_install_docs: OK ({tag})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
