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


# Placeholder strings that mean "we do not know", written by upstream exporters
# and by our own early importers. They are not values: a lead whose title is the
# string "unknown" reads as known-to-be-unknown, sorts and groups as a real
# title, satisfies a NOT NULL check, and -- the reason this exists -- WINS a
# merge, because the merge rule fills an empty field from the other record and
# "unknown" is not empty. Absence is NULL; nothing else.
#
# Deliberately conservative: only strings whose *entire* content is a placeholder.
# "Unknown Systems Ltd" is a company, and "N/A Consulting" is somebody's employer.
SENTINEL_VALUES = frozenset({
    "unknown", "n/a", "n.a.", "na", "none", "null", "nil", "-", "--", "---",
    "not available", "not applicable", "not provided", "not specified",
    "no title", "no data", "undefined", "tbd", "?", "??", "(unknown)", "[unknown]",
})


def normalize_sentinel(value):
    """Return `value`, or None when it is a placeholder rather than a value.

    Whitespace-only and empty strings collapse to None too, so a caller can use
    this as the single "is there anything here?" gate instead of pairing it with
    a separate truthiness check. Non-strings are returned untouched.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    return None if text.casefold() in SENTINEL_VALUES else text


def sentinel_sql_condition(column: str) -> str:
    """SQL that is true when `column` holds a placeholder, empty string or NULL.

    Kept beside the Python list so a backfill and a query can never disagree
    about what counts as absent.
    """
    values = ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(SENTINEL_VALUES))
    return (f"({column} IS NULL OR TRIM({column}) = ''"
            f" OR LOWER(TRIM({column})) IN ({values}))")


def signal_fingerprint(
    name: str = "", domain: str = "", linkedin: str = "",
) -> str:
    """A stable hash of the signal SET an email-finding attempt was made with.

    The dedup question a batch actually needs to answer is not "have we tried
    this lead?" but "have we tried this lead *with these signals*?". Searching
    for someone by name and domain is a materially different query from
    searching for them by name, domain and LinkedIn URL, and a `not_found` on
    the first says nothing about the second.

    `skip_reason_from_lookup` already made exactly this argument for `domain`
    (an attempt recorded with no domain never really searched, so it must not
    block a retry once a domain exists). This generalizes it: any change to the
    signal set produces a different fingerprint, and only an unchanged
    fingerprint blocks a retry.
    """
    parts = []
    if name:
        parts.append(f"name={' '.join(str(name).lower().split())}")
    if domain:
        parts.append(f"domain={str(domain).strip().lower().lstrip('@')}")
    if linkedin:
        parts.append(f"linkedin={normalize_linkedin(str(linkedin)).lower().rstrip('/')}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:16]


def canonicalize_email(raw: Any) -> tuple[Optional[str], list[str]]:
    """Canonical form of an email address, plus the repairs that got it there.

    Returns (address, repairs). `address` is None when nothing salvageable is
    left; `repairs` names each change made, so a caller can report "7 addresses
    reformatted" instead of silently rewriting the operator's data.

    This exists because `.strip().lower()` was the whole of email normalization
    and it is not enough. Seven addresses reached MillionVerifier with a
    trailing dot ("rhinke@example.com.") and came back `invalid` -- not because
    the mailboxes were dead but because the *string* was malformed. Five of the
    seven were live. A malformed address costs a verification credit and
    returns a guaranteed wrong answer, so the repair has to happen before the
    address is stored, not after it fails.
    """
    if raw is None:
        return None, []
    text = str(raw)
    repairs: list[str] = []

    stripped = text.strip()
    if stripped != text:
        repairs.append("whitespace")
    text = stripped

    # "Jane Doe <jane@acme.com>" -- mail clients and CSV exports both do this.
    match = re.search(r"<([^>]+)>", text)
    if match:
        text = match.group(1).strip()
        repairs.append("angle_brackets")

    # Internal whitespace is never legal in the addresses we handle and is
    # nearly always a copy-paste artifact.
    if re.search(r"\s", text):
        text = re.sub(r"\s+", "", text)
        repairs.append("internal_whitespace")

    lowered = text.lower()
    if lowered != text:
        repairs.append("case")
    text = lowered

    # Trailing punctuation picked up from prose -- the finder scrapes snippets,
    # and a sentence-final address keeps the full stop.
    trimmed = text.rstrip(".,;:")
    if trimmed != text:
        repairs.append("trailing_punctuation")
    text = trimmed
    text = text.lstrip(".")

    if text.count("@") != 1:
        return None, repairs
    local, domain = text.split("@", 1)
    domain = domain.strip(".")
    if ".." in domain:
        domain = re.sub(r"\.{2,}", ".", domain)
        repairs.append("double_dot")
    if not local or not validate_domain(domain):
        return None, repairs
    return f"{local}@{domain}", repairs


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
