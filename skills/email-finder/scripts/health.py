"""Pre-flight health checks for email-finder batch runs."""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from credits import CREDIT_PER_EMAIL_FOUND, trykitt_findable_from_balance

TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"


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


def probe_trykitt(
    api_key: str,
    timeout: int = 15,
    *,
    live_probe: bool = False,
) -> tuple[float, int, Optional[str]]:
    """Return (remaining_credits, estimated_lookups, error).

    When live_probe is False (default), only validates the key is present — no API call.
    Set trykitt_live_health_probe in config.json to true for a live credit check (~1 lookup).
    """
    if not api_key or len(api_key.strip()) < 8:
        return 0.0, 0, "API key not set"
    if not live_probe:
        return 1.0, 999999, None

    import json

    body = json.dumps({"fullName": "Health Check", "domain": "example.com", "realtime": True}).encode()
    req = urllib.request.Request(
        TRYKITT_FIND_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "User-Agent": "email-finder/2.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 401:
            return 0.0, 0, "invalid API key (401)"
        if e.code == 500 and "out of credits" in err_body.lower():
            return 0.0, 0, "out of credits"
        return 0.0, 0, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return 0.0, 0, str(e)
    credits = payload.get("credits") or {}
    remaining = float(credits.get("remainingCredits") or 0)
    job_cost = float(credits.get("jobCredits") or 0)
    lookups = trykitt_findable_from_balance(remaining, job_cost)
    return remaining, lookups, None


def count_usable_find_providers(
    cfg: dict[str, Any],
    api_providers: list[tuple[str, str]],
    provider_names: list[str],
) -> tuple[int, list[str], list[str]]:
    """Return (usable_count, usable_names, issues)."""
    live = bool(cfg.get("trykitt_live_health_probe", False))
    usable: list[str] = []
    issues: list[str] = []
    for name, api_key in api_providers:
        if name not in provider_names:
            continue
        if not api_key:
            issues.append(f"{name}: API key not set")
            continue
        if name == "trykitt":
            remaining, lookups, err = probe_trykitt(api_key, live_probe=live)
            if err:
                issues.append(f"trykitt: {err}")
            elif live and remaining <= 0:
                issues.append("trykitt: out of credits")
            else:
                usable.append(name)
                if live and lookups < 1:
                    issues.append("trykitt: out of credits")
        else:
            usable.append(name)
    return len(usable), usable, issues


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
    live = bool(cfg.get("trykitt_live_health_probe", False))

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
            remaining, lookups, err = probe_trykitt(api_key, live_probe=live)
            if err:
                issues.append(f"trykitt: {err}")
            elif live and remaining <= 0:
                issues.append("trykitt: out of credits")
            elif live and lookups < batch_size:
                issues.append(
                    f"trykitt: only ~{lookups} email finds left (batch {batch_size}, "
                    f"{CREDIT_PER_EMAIL_FOUND} credit each)"
                )
            elif live:
                ok_msgs.append(
                    f"trykitt: ~{lookups} email finds available "
                    f"({CREDIT_PER_EMAIL_FOUND} credit per found email)"
                )
            else:
                ok_msgs.append("trykitt: API key configured (set trykitt_live_health_probe for live balance)")
        else:
            ok_msgs.append(f"{name}: API key configured")

    usable_n, _usable, _ = count_usable_find_providers(cfg, providers, [p for p, _ in providers])
    if not usable_n and providers:
        issues.append("no find providers available")

    return (len(issues) == 0, issues, ok_msgs)


def icypeas_batch_warnings(
    provider_names: list[str],
    *,
    workers: int,
    delay: float,
    cfg: dict[str, Any],
) -> list[str]:
    """Warn when batch settings are likely to hit IcyPeas rate limits."""
    if "icypeas" not in provider_names:
        return []
    min_delay = float(cfg.get("icypeas_request_delay_seconds", 1.5))
    effective_delay = max(delay, min_delay) if delay > 0 else min_delay
    warnings: list[str] = []
    if workers > 2 and effective_delay < 2:
        warnings.append(
            "icypeas: use --workers 2 --delay 3 (or higher) to avoid rate limits"
        )
    elif workers > 1 and effective_delay < 1.5:
        warnings.append(
            f"icypeas: delay {effective_delay:.1f}s may be too low for {workers} workers"
        )
    return warnings


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
