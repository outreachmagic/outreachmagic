"""Local API key pool helpers with HTTP failover and runtime status tracking."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

FAILOVER_HTTP_CODES = frozenset({401, 402, 403, 429})
_VALUE_ERROR_FAILOVER_RE = re.compile(r"\bHTTP\s+(401|402|403|429)\b", re.I)
_SERPER_CREDIT_EXHAUSTED_RE = re.compile(
    r"serper http 400.*(?:not enough credits|insufficient credits)",
    re.I,
)

API_KEY_PROVIDERS: tuple[dict[str, str], ...] = (
    {"provider": "serper", "env_key": "SERPER_API_KEY", "skill": "lead-enrich"},
    {"provider": "trykitt", "env_key": "TRYKITT_API_KEY", "skill": "email-finder"},
    {"provider": "icypeas", "env_key": "ICYPEAS_API_KEY", "skill": "email-finder"},
    {"provider": "millionverifier", "env_key": "MILLIONVERIFIER_API_KEY", "skill": "email-finder"},
)

_STATUS_FILENAME = "api_key_status.json"


def api_key_pool(env_key: str) -> list[str]:
    """Ordered non-empty keys for env_key and env_key__N backups."""
    keys: list[str] = []
    primary = (os.environ.get(env_key) or "").strip()
    if primary:
        keys.append(primary)
    n = 1
    while True:
        backup = (os.environ.get(f"{env_key}__{n}") or "").strip()
        if not backup:
            break
        keys.append(backup)
        n += 1
    return keys


def slot_label(slot: int) -> str:
    if slot == 0:
        return "Primary"
    return f"Backup #{slot}"


def key_fingerprint(key: str) -> tuple[str, str]:
    trimmed = (key or "").strip()
    if len(trimmed) <= 8:
        return "···", "****"
    return trimmed[:6], trimmed[-4:]


def status_file_path() -> Path:
    from om_paths import get_data_root

    return get_data_root() / _STATUS_FILENAME


def load_key_status() -> dict[str, Any]:
    path = status_file_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_key_usage(
    *,
    provider: str,
    slot: int,
    success: bool,
    error: str | None = None,
) -> None:
    path = status_file_path()
    data = load_key_status()
    provider_data = data.setdefault(provider, {})
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entry = {
        "last_used": now,
        "status": "ok" if success else "failed",
        "last_error": None if success else (error or "unknown error"),
    }
    if success:
        entry["last_ok"] = now
    provider_data[str(slot)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_failover_http_status(code: int) -> bool:
    return code in FAILOVER_HTTP_CODES


def value_error_is_failover(exc: BaseException) -> bool:
    msg = str(exc)
    if _VALUE_ERROR_FAILOVER_RE.search(msg):
        return True
    if _SERPER_CREDIT_EXHAUSTED_RE.search(msg):
        return True
    return False


def log_failover(*, provider: str, env_key: str, slot: int, code: int | str) -> None:
    print(
        f"[outreachmagic] {provider}: {env_key} slot {slot} failed ({code}), trying next",
        file=sys.stderr,
        flush=True,
    )


def call_with_key_pool(
    env_key: str,
    fn: Callable[[str], T],
    *,
    provider: str,
) -> T:
    """Call fn(api_key) trying each slot until success or pool exhausted."""
    pool = api_key_pool(env_key)
    if not pool:
        raise ValueError(f"{env_key} not set")
    last_err: BaseException | None = None
    for slot, key in enumerate(pool):
        try:
            result = fn(key)
            record_key_usage(provider=provider, slot=slot, success=True)
            return result
        except urllib.error.HTTPError as exc:
            record_key_usage(provider=provider, slot=slot, success=False, error=f"HTTP {exc.code}")
            if not is_failover_http_status(exc.code):
                raise
            log_failover(provider=provider, env_key=env_key, slot=slot, code=exc.code)
            last_err = exc
        except ValueError as exc:
            record_key_usage(provider=provider, slot=slot, success=False, error=str(exc))
            if not value_error_is_failover(exc):
                raise
            log_failover(provider=provider, env_key=env_key, slot=slot, code="http")
            last_err = exc
    raise ValueError(f"{provider}: all {len(pool)} key(s) for {env_key} failed") from last_err


def result_should_failover(result: dict, *, provider: str) -> bool:
    """Dict-shaped provider errors (email-finder adapters)."""
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "")
    if status == "auth_error":
        return True
    if status == "rate_limited":
        return True
    if status == "http_error":
        code = int(result.get("http_status") or 0)
        if is_failover_http_status(code):
            return True
        err = str(result.get("error") or "").lower()
        if code == 500 and "out of credits" in err:
            return True
    if status in ("no_key",):
        return False
    return False


def call_with_key_pool_results(
    env_key: str,
    fn: Callable[[str], dict],
    *,
    provider: str,
) -> dict:
    """Like call_with_key_pool for functions returning result dicts."""
    pool = api_key_pool(env_key)
    if not pool:
        return {"status": "no_key", "error": f"{env_key} not set", "provider": provider}
    last: dict = {"status": "error", "error": "no result", "provider": provider}
    for slot, key in enumerate(pool):
        result = fn(key)
        if result_should_failover(result, provider=provider):
            error = str(result.get("error") or result.get("status") or "failover")
            record_key_usage(provider=provider, slot=slot, success=False, error=error)
            code = result.get("http_status") or result.get("status")
            log_failover(provider=provider, env_key=env_key, slot=slot, code=code)
            last = result
            continue
        record_key_usage(provider=provider, slot=slot, success=True)
        return result
    return last


def build_api_keys_report() -> dict[str, Any]:
    """Merge configured key slots with last-known runtime status."""
    status_data = load_key_status()
    providers: list[dict[str, Any]] = []
    for spec in API_KEY_PROVIDERS:
        env_key = spec["env_key"]
        provider = spec["provider"]
        pool = api_key_pool(env_key)
        if not pool:
            providers.append({
                "provider": provider,
                "skill": spec["skill"],
                "env_key": env_key,
                "status": "no_keys",
                "keys": [],
            })
            continue
        provider_status = status_data.get(provider, {})
        if not isinstance(provider_status, dict):
            provider_status = {}
        keys: list[dict[str, Any]] = []
        for slot, key in enumerate(pool):
            prefix, suffix = key_fingerprint(key)
            slot_status = provider_status.get(str(slot), {})
            if not isinstance(slot_status, dict):
                slot_status = {}
            runtime_status = slot_status.get("status")
            if runtime_status not in ("ok", "failed"):
                runtime_status = "never_used"
            keys.append({
                "slot": slot,
                "label": slot_label(slot),
                "prefix": prefix,
                "suffix": suffix,
                "status": runtime_status,
                "last_used": slot_status.get("last_used"),
                "last_ok": slot_status.get("last_ok"),
                "last_error": slot_status.get("last_error"),
            })
        providers.append({
            "provider": provider,
            "skill": spec["skill"],
            "env_key": env_key,
            "keys": keys,
        })
    return {"providers": providers}


def build_api_key_status_push_payload(client_id: str) -> dict[str, Any]:
    report = build_api_keys_report()
    return {"clientId": client_id, "providers": report["providers"]}


def maybe_push_api_key_status_to_cloud(
    *,
    load_config_fn: Callable[[], dict],
    get_agent_key_fn: Callable[[], str | None],
    get_client_id_fn: Callable[[], str],
    push_fn: Callable[[str, str, dict[str, Any]], dict],
    quiet: bool = True,
) -> dict[str, Any]:
    """POST aggregate runtime key status (no secret values). Non-fatal."""
    tok = get_agent_key_fn()
    if not tok:
        return {"api_key_status_reported": "skipped_no_key"}
    try:
        import agent_secrets_cloud
        api_base = agent_secrets_cloud.get_api_base(load_config_fn)
        payload = build_api_key_status_push_payload(get_client_id_fn())
        push_fn(api_base, tok, payload)
        if not quiet:
            print(
                f"[outreachmagic] API key runtime status reported ({len(payload['providers'])} providers)",
                file=sys.stderr,
                flush=True,
            )
        return {"api_key_status_reported": "reported", "providers": len(payload["providers"])}
    except Exception as exc:
        if not quiet:
            print(f"[outreachmagic] API key status report failed: {exc}", file=sys.stderr, flush=True)
        return {"api_key_status_reported": "error", "api_key_status_error": str(exc)[:200]}


def format_api_keys_report_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for entry in report.get("providers") or []:
        provider = entry.get("provider", "")
        skill = entry.get("skill", "")
        lines.append(f"{provider.title()} ({skill}):")
        if entry.get("status") == "no_keys":
            lines.append("  (no keys configured)")
            lines.append("")
            continue
        for key in entry.get("keys") or []:
            label = key.get("label", "")
            prefix = key.get("prefix", "")
            suffix = key.get("suffix", "")
            status = key.get("status", "never_used")
            last_used = key.get("last_used")
            last_error = key.get("last_error")
            fingerprint = f"{prefix}…{suffix}" if prefix or suffix else "····"
            detail = status
            if status == "ok" and last_used:
                detail = f"OK (last used {last_used})"
            elif status == "failed":
                detail = f"FAILED (last error: {last_error or 'unknown'})"
            lines.append(f"  Slot {key.get('slot', 0)} ({label}): {fingerprint} — {detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
