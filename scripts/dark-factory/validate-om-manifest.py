#!/usr/bin/env python3
"""Pre-flight: manifest and update file lists include every outreachmagic script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SCRIPTS))

from generate_skill_manifest import generate_manifest  # noqa: E402
from skill_suite import manifest_relative_paths  # noqa: E402


def main() -> int:
    errors: list[str] = []
    all_py = {p.name for p in SCRIPTS.glob("*.py")}

    import pipeline as om  # noqa: E402

    if set(om.UPDATE_SCRIPT_FILES) != all_py:
        missing = sorted(all_py - set(om.UPDATE_SCRIPT_FILES))
        extra = sorted(set(om.UPDATE_SCRIPT_FILES) - all_py)
        if missing:
            errors.append(f"UPDATE_SCRIPT_FILES missing: {missing}")
        if extra:
            errors.append(f"UPDATE_SCRIPT_FILES unexpected: {extra}")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_py = {n for n in manifest.get("files", {}) if n.endswith(".py")}
    if manifest_py != all_py:
        errors.append(
            f"update-manifest.json script set mismatch: missing={sorted(all_py - manifest_py)} "
            f"extra={sorted(manifest_py - all_py)}"
        )

    expected = set(manifest_relative_paths("outreachmagic"))
    if set(manifest.get("files", {})) != expected:
        errors.append("skill-suite.json paths do not match committed outreachmagic manifest")

    generated = generate_manifest("outreachmagic")
    if generated.get("files") != manifest.get("files"):
        errors.append("run python3 scripts/generate_skill_manifest.py outreachmagic")

    download_names = set(om.update_download_names(manifest))
    if not {"pipeline_lead_review.py", "pipeline_dedup.py", "review_cloud.py"}.issubset(download_names):
        errors.append("update_download_names() would skip review/dedup modules")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    print("PASS: outreachmagic manifest/update file lists are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
