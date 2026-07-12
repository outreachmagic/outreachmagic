"""Input normalization and validation for outreachmagic email-finding."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$",
    re.I,
)
_LINKEDIN_RE = re.compile(r"^https?://", re.I)
_LINKEDIN_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?linkedin\.com/", re.I)


def normalize_linkedin(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return u
    if _LINKEDIN_RE.match(u):
        return u
    # Already has a linkedin.com/ path (Sales Navigator exports omit the
    # protocol) -- just add https, don't blind-prepend linkedin.com/in/ or
    # the result double-paths: linkedin.com/in/linkedin.com/in/handle.
    if _LINKEDIN_DOMAIN_RE.match(u):
        return f"https://{u.lstrip('/')}"
    if u.startswith("/in/") or u.startswith("in/"):
        return f"https://linkedin.com/{u.lstrip('/')}"
    return f"https://linkedin.com/in/{u.strip('/')}"


def row_fields(row: dict[str, Any]) -> tuple[str, str, str, str, Optional[int]]:
    """Return name, domain, company, linkedin, lead_id."""
    name = (
        row.get("fullName")
        or row.get("full_name")
        or row.get("name")
        or ""
    )
    if isinstance(name, str):
        name = name.strip()
    domain = (row.get("company_domain") or row.get("domain") or "").strip().lower().lstrip("@")
    company = (row.get("company_name") or row.get("company") or "").strip()
    linkedin = (row.get("linkedin_url") or row.get("linkedin") or "").strip()
    lead_id_raw = row.get("lead_id") or row.get("id")
    lead_id: Optional[int] = None
    if lead_id_raw is not None and str(lead_id_raw).strip().isdigit():
        lead_id = int(str(lead_id_raw).strip())
    return name, domain, company, linkedin, lead_id


def lead_resume_key(row: dict[str, Any], *, index: int) -> str:
    """Stable key for resume CSV (prefer lead_id)."""
    _name, domain, _company, linkedin, lead_id = row_fields(row)
    if lead_id is not None:
        return f"id:{lead_id}"
    parts = [_name.lower(), domain.lower(), linkedin.lower()]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def validate_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip("@")
    return bool(d and "." in d and _DOMAIN_RE.match(d))


def sanitize_input_path(path: str, *, base: Optional[Path] = None) -> Path:
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise ValueError("path must not contain '..'")
    resolved = raw.resolve()
    if base is not None:
        base_resolved = base.expanduser().resolve()
        try:
            resolved.relative_to(base_resolved)
        except ValueError as exc:
            raise ValueError("path escapes allowed directory") from exc
    return resolved


def load_people_json(path: str) -> list[dict[str, Any]]:
    p = sanitize_input_path(path)
    raw = p.read_text(encoding="utf-8")
    people = json.loads(raw)
    if isinstance(people, dict):
        people = people.get("people") or people.get("rows") or []
    if not isinstance(people, list):
        raise ValueError("input must be a JSON array or {people: [...]}")
    return [r for r in people if isinstance(r, dict)]
