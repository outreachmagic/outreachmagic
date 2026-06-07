"""Local API key pool helpers with HTTP failover."""

from __future__ import annotations

import os
import re
import sys
import urllib.error
from typing import Callable, TypeVar

T = TypeVar("T")

FAILOVER_HTTP_CODES = frozenset({401, 402, 403, 429})
_VALUE_ERROR_FAILOVER_RE = re.compile(r"\bHTTP\s+(401|402|403|429)\b", re.I)


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


def is_failover_http_status(code: int) -> bool:
    return code in FAILOVER_HTTP_CODES


def value_error_is_failover(exc: BaseException) -> bool:
    return bool(_VALUE_ERROR_FAILOVER_RE.search(str(exc)))


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
            return fn(key)
        except urllib.error.HTTPError as exc:
            if not is_failover_http_status(exc.code):
                raise
            log_failover(provider=provider, env_key=env_key, slot=slot, code=exc.code)
            last_err = exc
        except ValueError as exc:
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
            code = result.get("http_status") or result.get("status")
            log_failover(provider=provider, env_key=env_key, slot=slot, code=code)
            last = result
            continue
        return result
    return last
