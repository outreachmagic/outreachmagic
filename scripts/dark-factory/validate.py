#!/usr/bin/env python3
"""Deterministic validators for dark-factory expect fields."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

PERSONAL_DOMAINS = (
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "proton.me",
    "mail.com",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def extract_emails(text: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)]


def _parse_json_output(text: str) -> Any | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _json_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def validate_expect(output: str, expect: dict[str, Any]) -> tuple[bool, str | None]:
    text = output or ""
    parsed_json = _parse_json_output(text) if any(
        k in expect for k in ("json_field", "json_fields", "contains_json", "json_stats")
    ) else None
    for key, value in expect.items():
        if key == "has_email":
            emails = extract_emails(text)
            if value:
                if not emails:
                    return False, "expected an email address in output"
            else:
                if emails:
                    return False, f"expected no email but found {emails[0]}"
        elif key == "domain_contains":
            needle = str(value).lower()
            emails = extract_emails(text)
            if not any(needle in (e.split("@", 1)[-1].lower()) for e in emails):
                return False, f"expected email domain containing '{value}'"
        elif key == "no_personal_email":
            if value:
                for email in extract_emails(text):
                    domain = email.split("@", 1)[-1].lower()
                    if domain in PERSONAL_DOMAINS:
                        return False, f"personal email not allowed: {email}"
        elif key == "has_company_size":
            if value and not re.search(
                r"employee|headcount|workforce|team of|company size|\b\d{1,3}[,.]?\d*\s*(k\+?)?\s*employee",
                text,
                re.I,
            ):
                return False, "expected company size / employee count mention"
        elif key == "has_recent_news":
            if value and not re.search(
                r"news|announce|launch|raised|acquir|funding|recent|partnership|expansion",
                text,
                re.I,
            ):
                return False, "expected recent news mention"
        elif key == "has_tech_stack":
            if value and not re.search(
                r"tech stack|technolog|framework|platform|cloud|aws|azure|gcp|kubernetes|python|react",
                text,
                re.I,
            ):
                return False, "expected tech stack mention"
        elif key == "has_confidence":
            if value and not re.search(r"confidence|%\s|score|rating", text, re.I):
                return False, "expected confidence score mention"
        elif key == "contains_string":
            if str(value).lower() not in text.lower():
                return False, f"expected output to contain '{value}'"
        elif key == "min_length":
            if len(text) < int(value):
                return False, f"expected at least {value} characters, got {len(text)}"
        elif key == "contains_json":
            if value and parsed_json is None:
                return False, "expected valid JSON in output"
        elif key in ("json_field", "json_fields"):
            if parsed_json is None:
                return False, "expected valid JSON in output"
            specs = value if isinstance(value, list) else [value]
            for spec in specs:
                if not isinstance(spec, dict):
                    return False, "json_field spec must be an object"
                path = str(spec.get("path") or "")
                actual = _json_path(parsed_json, path)
                if actual is None and "equals" not in spec and "gte" not in spec and "lte" not in spec:
                    return False, f"json path not found: {path}"
                if "equals" in spec and actual != spec["equals"]:
                    return False, f"expected {path} == {spec['equals']}, got {actual}"
                if "gte" in spec and (actual is None or actual < spec["gte"]):
                    return False, f"expected {path} >= {spec['gte']}, got {actual}"
                if "lte" in spec and (actual is None or actual > spec["lte"]):
                    return False, f"expected {path} <= {spec['lte']}, got {actual}"
                if spec.get("type") == "int" and actual is not None and not isinstance(actual, int):
                    return False, f"expected {path} to be int, got {type(actual).__name__}"
        elif key == "json_stats":
            if parsed_json is None:
                return False, "expected valid JSON in output"
            stats = parsed_json.get("stats") if isinstance(parsed_json, dict) else None
            if not isinstance(stats, dict):
                return False, "expected stats object in JSON output"
            required = value if isinstance(value, list) else []
            for field in required:
                if field not in stats:
                    return False, f"stats missing field: {field}"
        else:
            return False, f"unknown expect field: {key}"
    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate output against expect JSON")
    parser.add_argument("--output", required=True, help="Agent/script output text or path to file")
    parser.add_argument("--expect", required=True, help="JSON object of expect fields")
    args = parser.parse_args()

    output = args.output
    if output.startswith("@") or ("\n" not in output and len(output) < 260 and not output.strip().startswith("{")):
        from pathlib import Path

        p = Path(output.lstrip("@"))
        if p.is_file():
            output = p.read_text(encoding="utf-8", errors="replace")

    expect = json.loads(args.expect)
    ok, reason = validate_expect(output, expect)
    if ok:
        print("PASS")
        sys.exit(0)
    print(f"FAIL: {reason}")
    sys.exit(1)


if __name__ == "__main__":
    main()
