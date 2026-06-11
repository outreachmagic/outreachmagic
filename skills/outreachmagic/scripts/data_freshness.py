"""Local data freshness helpers (last_pull age for read commands)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional


_DURATION_RE = re.compile(
    r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)


def parse_duration(value: Optional[str]) -> Optional[int]:
    """Parse duration like 5m, 1h, 2d into seconds. Returns None if invalid/empty."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().lower()
    m = _DURATION_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return n * 60
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        return n * 3600
    return n * 86400


def _parse_last_pull_iso(last_pull: Optional[str]) -> Optional[datetime]:
    if not last_pull:
        return None
    raw = str(last_pull).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def freshness_from_last_pull(last_pull: Optional[str]) -> dict[str, Any]:
    """Build freshness metadata from config last_pull ISO timestamp."""
    dt = _parse_last_pull_iso(last_pull)
    if dt is None:
        return {
            "last_pull": last_pull,
            "stale_minutes": None,
            "freshness": "never",
            "freshness_message": "Data has never been pulled from the relay.",
        }
    now = datetime.now(timezone.utc)
    age_sec = max(0, int((now - dt).total_seconds()))
    stale_minutes = age_sec // 60
    if stale_minutes < 1:
        age_label = "just now"
    elif stale_minutes == 1:
        age_label = "1 minute ago"
    elif stale_minutes < 60:
        age_label = f"{stale_minutes} minutes ago"
    else:
        hours = stale_minutes // 60
        age_label = f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
    iso = dt.isoformat().replace("+00:00", "Z")
    return {
        "last_pull": iso,
        "stale_minutes": stale_minutes,
        "freshness": "ok",
        "freshness_message": f"Data as of {iso} ({age_label}). Run pull for latest webhook events.",
    }


def is_pull_fresh_enough(last_pull: Optional[str], max_age_seconds: int) -> bool:
    dt = _parse_last_pull_iso(last_pull)
    if dt is None:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age < max_age_seconds


def attach_freshness(result: Any, *, last_pull: Optional[str]) -> Any:
    """Merge freshness fields into a dict result or wrap a list."""
    meta = freshness_from_last_pull(last_pull)
    if isinstance(result, dict):
        out = dict(result)
        out.update(meta)
        return out
    return {"data": result, **meta}


def freshness_stderr_line(last_pull: Optional[str]) -> str:
    return freshness_from_last_pull(last_pull)["freshness_message"]


def print_freshness_stderr(last_pull: Optional[str]) -> None:
    import sys

    print(freshness_stderr_line(last_pull), file=sys.stderr)
