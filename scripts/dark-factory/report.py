#!/usr/bin/env python3
"""Print ASCII summary from dark-factory result JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_results(paths: list[Path]) -> list[dict]:
    out = []
    for p in paths:
        if not p.is_file():
            continue
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", help="Result JSON files")
    args = parser.parse_args()
    bundles = load_results([Path(p) for p in args.results])
    if not bundles:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    total_pass = total_fail = 0
    lines = ["┌─ Dark factory test run ─────────────────────────────┐"]
    for bundle in bundles:
        env = bundle.get("environment", "unknown")
        passed = int(bundle.get("passed") or 0)
        failed = int(bundle.get("failed") or 0)
        total_pass += passed
        total_fail += failed
        lines.append(f"│  {env:<18} PASS: {passed:<3} FAIL: {failed:<3}              │")
        for row in bundle.get("results") or []:
            if row.get("status") != "fail":
                continue
            rid = row.get("id", "?")
            reason = row.get("reason") or "failed"
            lines.append(f"│    ✗ {rid}: {reason[:42]:<42} │")
    lines.append(f"│  {'TOTAL':<18} PASS: {total_pass:<3} FAIL: {total_fail:<3}              │")
    lines.append("└────────────────────────────────────────────────────┘")
    print("\n".join(lines))
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
