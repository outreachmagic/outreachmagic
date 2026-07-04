"""
Normalization helpers and utility functions extracted from pipeline.py.

Dependency-free leaf module — uses only stdlib, typed leaf imports,
and no pipeline.py internals.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Optional

from constants import PIPELINE_STAGES
from platform_registry import LINKEDIN_PLATFORMS
from workspace_routing import normalize_linkedin


def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


def normalize_company_domain(raw: Optional[str]) -> Optional[str]:
    """Normalize a company domain to canonical form: 'acme.com'."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if not text or "." not in text or " " in text or len(text) > 253:
        return None
    return text


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return str(email).strip().lower()


def normalize_event_sender(platform: str, sender: str) -> Optional[str]:
    """Normalize relay sender for storage; None if missing or unknown."""
    raw = (sender or "").strip()
    if not raw or raw.lower() == "unknown":
        return None
    plat = (platform or "").lower()
    if plat in LINKEDIN_PLATFORMS:
        return normalize_linkedin(raw)
    return raw.lower()


def normalize_tag(tag: str) -> str:
    """Lowercase, strip whitespace, collapse internal whitespace."""
    return " ".join(tag.strip().lower().split())


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        norm = normalize_tag(tag)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def parse_tags_value(val) -> list[str]:
    """Parse tags from CSV/JSON/CLI/sync payloads into normalized tag strings."""
    if val is None:
        return []
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            out.extend(parse_tags_value(item))
        return _dedupe_tags(out)
    if isinstance(val, (int, float)):
        val = str(val)
    if not isinstance(val, str):
        val = str(val)
    raw = val.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except (ValueError, SyntaxError):
            pass
        inner = raw[1:-1].strip().strip("'\"")
        if inner and ";" not in inner and "," not in inner:
            norm = normalize_tag(inner)
            return [norm] if norm else []
    return _parse_tags(raw)


def _parse_tags(raw_tags: str) -> list[str]:
    """Parse semicolon or comma-separated tags into a deduplicated list."""
    tags: list[str] = []
    seen: set[str] = set()
    for sep in (";", ","):
        if sep in raw_tags:
            for t in raw_tags.split(sep):
                norm = normalize_tag(t)
                if norm and norm not in seen:
                    tags.append(norm)
                    seen.add(norm)
            return tags
    norm = normalize_tag(raw_tags)
    if norm:
        return [norm]
    return []


def parse_headcount_numeric(raw: Optional[str]) -> Optional[int]:
    """Extract a numeric midpoint from headcount strings like '11-50' or '500+'."""
    if not raw:
        return None
    text = re.sub(r'[^\d\-+]', '', str(raw).strip())
    if not text:
        return None
    range_match = re.match(r'(\d+)-(\d+)', text)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        return (lo + hi) // 2
    plus_match = re.match(r'(\d+)\+?$', text)
    if plus_match:
        return int(plus_match.group(1))
    return None


def furthest_stage(stage_a: str, stage_b: str) -> str:
    def rank(s: str) -> int:
        try:
            return PIPELINE_STAGES.index(s)
        except ValueError:
            return 0
    return stage_a if rank(stage_a) >= rank(stage_b) else stage_b
