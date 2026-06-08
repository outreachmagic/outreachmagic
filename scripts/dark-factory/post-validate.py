#!/usr/bin/env python3
"""Re-validate agent/script result JSON against catalog expect fields."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from validate import validate_expect  # noqa: E402


def load_catalog(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    by_id: dict[str, dict] = {}
    for cases in data.get("skills", {}).values():
        for case in cases:
            by_id[case["id"]] = case
    return by_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--output", help="Write corrected results JSON")
    args = parser.parse_args()

    catalog = load_catalog(Path(args.catalog))
    bundle = json.loads(Path(args.results).read_text(encoding="utf-8"))
    results = bundle.get("results") or []

    passed = failed = 0
    for row in results:
        case_id = row.get("id", "")
        case = catalog.get(case_id)
        if case and case.get("mode") == "agent":
            agent_status = str(row.get("status") or "").lower()
            if agent_status == "pass":
                passed += 1
            else:
                failed += 1
            continue
        actual = row.get("actual") or row.get("output") or ""
        if not case:
            row["status"] = "fail"
            row["reason"] = f"unknown test id: {case_id}"
            failed += 1
            continue
        ok, reason = validate_expect(str(actual), case.get("expect") or {})
        row["status"] = "pass" if ok else "fail"
        row["reason"] = reason
        if ok:
            passed += 1
        else:
            failed += 1

    bundle["passed"] = passed
    bundle["failed"] = failed
    bundle["post_validated"] = True

    out = Path(args.output) if args.output else Path(args.results)
    out.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"POST-VALIDATE: PASS {passed} / FAIL {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
