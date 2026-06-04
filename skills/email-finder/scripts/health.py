"""Pre-flight health checks for email-finder batch runs."""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"
ICYPEAS_FIND_URL = "https://app.icypeas.com/api/email-search"


def check_disk_space(min_mb: int = 100) -> Optional[str]:
    disk = shutil.disk_usage("/")
    free_mb = disk.free // (1024 * 1024)
    if free_mb < min_mb:
        return f"Low disk space: {free_mb}MB free (need {min_mb}MB)"
    return None


def check_outreachmagic(om_dir: Any, key_status_fn: Callable) -> Optional[str]:
    if not om_dir:
        return "OutreachMagic not found — install outreachmagic first"
    has_key, _source = key_status_fn(om_dir)
    if not has_key:
        return "OutreachMagic agent key not configured — run pipeline.py login"
    return None


def probe_trykitt(api_key: str, timeout: int = 15) -> tuple[float, int, Optional[str]]:
    import json

    body = json.dumps({"fullName": "Health Check", "domain": "example.com", "realtime": True}).encode()
    req = urllib.request.Request(
        TRYKITT_FIND_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "User-Agent": "email-finder/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return 0.0, 0, "invalid API key (401)"
        return 0.0, 0, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return 0.0, 0, str(e)
    credits = payload.get("credits") or {}
    remaining = float(credits.get("remainingCredits") or 0)
    job_cost = float(credits.get("jobCredits") or 0.005)
    lookups = int(remaining / job_cost) if job_cost > 0 else 0
    return remaining, lookups, None


def run_health_check(
    cfg: dict[str, Any],
    *,
    om_dir: Any,
    key_status_fn: Callable,
    providers: list[tuple[str, str]],
    batch_size: int,
    skip_om: bool = False,
) -> tuple[bool, list[str], list[str]]:
    """Return (ok, issues, successes)."""
    issues: list[str] = []
    ok_msgs: list[str] = []

    disk_issue = check_disk_space()
    if disk_issue:
        issues.append(disk_issue)

    if not skip_om:
        om_issue = check_outreachmagic(om_dir, key_status_fn)
        if om_issue:
            issues.append(om_issue)

    for name, api_key in providers:
        if not api_key:
            issues.append(f"{name}: API key not set")
            continue
        if name == "trykitt":
            remaining, lookups, err = probe_trykitt(api_key)
            if err:
                issues.append(f"trykitt: {err}")
            elif remaining <= 0:
                issues.append("trykitt: out of credits")
            elif lookups < batch_size:
                issues.append(f"trykitt: only ~{lookups} lookups left (batch {batch_size})")
            else:
                ok_msgs.append(f"trykitt: {remaining:.3f} credits (~{lookups} lookups)")
        else:
            ok_msgs.append(f"{name}: API key configured")

    return (len(issues) == 0, issues, ok_msgs)


def format_health_lines(
    issues: list[str],
    ok_msgs: list[str],
    *,
    skip_om: bool = False,
    om_connected: bool = False,
) -> list[str]:
    lines: list[str] = []
    for msg in ok_msgs:
        lines.append(f"✅ {msg}")
    if not skip_om and om_connected and not any("OutreachMagic" in i for i in issues):
        lines.append("✅ OM connected (will dedup + save)")
    elif skip_om:
        lines.append("⚠️  OM skipped (--skip-om)")
    for issue in issues:
        lines.append(f"❌ {issue}")
    disk = check_disk_space()
    if disk:
        lines.append(f"❌ {disk}")
    else:
        free_mb = shutil.disk_usage("/").free // (1024 * 1024)
        lines.append(f"✅ Disk: {free_mb}MB free")
    return lines
