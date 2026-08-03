"""Local API key pool helpers with HTTP failover and runtime status tracking."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
from collections import defaultdict
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
    # `provider` is a cross-repo contract: wbhk-app's ENV_KEY_TO_RUNTIME_PROVIDER
    # maps FIRECRAWL_API_KEY to this exact string, and on a mismatch the portal's
    # health chip renders nothing rather than erroring.
    {"provider": "firecrawl", "env_key": "FIRECRAWL_API_KEY", "skill": "contact-sourcing"},
)

_STATUS_FILENAME = "api_key_status.json"

# ── Session-level dead slot tracking ──────────────────────────────────────────
# After CONSECUTIVE_FAILURES_LIMIT consecutive failures on a slot within one
# Python process lifetime, the slot is "retired" and skipped for the rest of the
# session.  This prevents the primary key from being retried (and failing) on
# every single call when it is known to be dead.
CONSECUTIVE_FAILURES_LIMIT = 3

_session_failures: dict[tuple[str, int], int] = defaultdict(int)
"""{(provider, slot): consecutive_failure_count}"""

_session_retired: set[tuple[str, int]] = set()
"""{(provider, slot)} — slots permanently skipped for this session."""


def _record_failure(provider: str, slot: int) -> None:
    key = (provider, slot)
    _session_failures[key] += 1
    if _session_failures[key] >= CONSECUTIVE_FAILURES_LIMIT:
        _session_retired.add(key)
        _session_failures.pop(key, None)


def _record_success(provider: str, slot: int) -> None:
    """Reset failure count on success (a working slot stays working)."""
    _session_failures.pop((provider, slot), None)


def _slot_is_retired(provider: str, slot: int) -> bool:
    return (provider, slot) in _session_retired


def _retired_slots_for_provider(provider: str) -> list[int]:
    return sorted(s for p, s in _session_retired if p == provider)


def session_retired_slots_report(provider: str) -> str:
    """Human-readable summary of retired slots for this provider."""
    retired = _retired_slots_for_provider(provider)
    if not retired:
        return ""
    lines: list[str] = []
    for slot in retired:
        label = "Primary" if slot == 0 else f"Backup #{slot}"
        lines.append(
            f"[outreachmagic] {provider}: {label} (slot {slot}) retired for this session "
            f"after {CONSECUTIVE_FAILURES_LIMIT}+ consecutive failures."
        )
    return "\n".join(lines)


def clear_session_state() -> None:
    """Reset session-level failure tracking (e.g. after a fresh sync-secrets)."""
    _session_failures.clear()
    _session_retired.clear()


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
    from om_paths import get_config_path

    return get_config_path().parent / _STATUS_FILENAME


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
    """Record one call's outcome for one slot. Never raises.

    Written atomically (temp file + os.replace). This is a read-modify-write of
    a single file holding EVERY provider's history, so a torn write is not a
    lost update -- `load_key_status()` swallows the JSONDecodeError and returns
    `{}`, and the next write then persists that empty dict as the new truth.
    One interrupted write silently erases every provider's status, which is
    exactly the state the dashboard was rendering as "not used yet" for a key
    that had just been called a thousand times.

    `last_ok` is preserved across a failure so the panel can still show when a
    key last worked -- "failed now, last worked 3 days ago" and "failed now,
    never worked" call for different actions.
    """
    try:
        path = status_file_path()
        data = load_key_status()
        provider_data = data.setdefault(provider, {})
        previous = provider_data.get(str(slot))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        entry = {
            "last_used": now,
            "status": "ok" if success else "failed",
            "last_error": None if success else (error or "unknown error"),
        }
        entry["last_ok"] = now if success else (
            (previous or {}).get("last_ok") if isinstance(previous, dict) else None)
        provider_data[str(slot)] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- bookkeeping must never break a real call
        pass


# ── Preflight: ask the provider what's left before spending the batch ────────
#
# call_with_key_pool() is reactive -- it discovers a dead slot by failing on it.
# That is correct per-call and useless per-batch: a 1,452-company find-domains
# run on 2026-08-03 burned through all three Serper slots and then wrote 1,023
# error observations, one per company it could not search. Asking first costs
# one cheap request per slot and turns that into a refusal at the door.
#
# provider -> (url_template, header_builder, list of response keys holding the
# remaining balance). A provider absent here simply has no preflight; callers
# treat "unknown" as "proceed", never as "block".
_BALANCE_ENDPOINTS: dict[str, dict[str, Any]] = {
    "serper": {
        "url": "https://google.serper.dev/account",
        "headers": lambda key: {"X-API-KEY": key},
        "keys": ("balance", "credit", "credits", "creditsLeft"),
    },
    "millionverifier": {
        "url": "https://api.millionverifier.com/api/v3/credits?api={key}",
        "headers": lambda _key: {},
        "keys": ("credits", "balance"),
    },
}


def _slot_balance(provider: str, key: str) -> dict[str, Any]:
    """Remaining balance for one key. Never raises: a preflight that fails must
    not be able to stop work the real call might well have completed."""
    spec = _BALANCE_ENDPOINTS.get(provider)
    if not spec:
        return {"supported": False, "remaining": None, "error": None}
    import urllib.request

    url = str(spec["url"]).replace("{key}", key)
    req = urllib.request.Request(url, headers=spec["headers"](key))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 -- any failure is "unknown", not fatal
        return {"supported": True, "remaining": None, "error": str(exc)[:200]}
    if not isinstance(payload, dict):
        return {"supported": True, "remaining": None, "error": "unexpected response shape"}
    for name in spec["keys"]:
        if isinstance(payload.get(name), (int, float)):
            return {"supported": True, "remaining": float(payload[name]), "error": None}
    return {"supported": True, "remaining": None, "error": "no balance field in response"}


def preflight(provider: str, env_key: str, *, need: int = 0) -> dict[str, Any]:
    """Live balance across every non-retired slot, before a batch commits.

    `sufficient` is deliberately optimistic on missing information: it is False
    only when we positively know the total is short. An unreachable balance
    endpoint must not block a run that would have worked.
    """
    pool = api_key_pool(env_key)
    if not pool:
        # `total_remaining: None` (unknown), not 0. A missing key is not a
        # credit shortfall, and callers gate on a *known* shortfall -- letting
        # this read as "0 credits" would turn "no key configured" into a
        # credit error, which is a worse message than the one the first real
        # call already gives.
        return {"provider": provider, "sufficient": True, "total_remaining": None,
                "need": need, "by_slot": [], "error": f"{env_key} not set"}

    by_slot: list[dict[str, Any]] = []
    total = 0.0
    known = False
    for slot, key in enumerate(pool):
        if _slot_is_retired(provider, slot):
            by_slot.append({"slot": slot, "label": slot_label(slot), "retired": True,
                            "remaining": None, "error": "retired this session"})
            continue
        info = _slot_balance(provider, key)
        if info["remaining"] is not None:
            total += info["remaining"]
            known = True
        by_slot.append({"slot": slot, "label": slot_label(slot), "retired": False,
                        "remaining": info["remaining"], "error": info["error"]})

    return {
        "provider": provider,
        "supported": any(s.get("remaining") is not None or s.get("error") for s in by_slot)
                     and provider in _BALANCE_ENDPOINTS,
        "by_slot": by_slot,
        "total_remaining": int(total) if known else None,
        "need": need,
        "sufficient": True if not known else total >= need,
    }


def format_preflight(report: dict[str, Any]) -> str:
    """One-screen slot table for a hard-fail message."""
    lines = [f"{report['provider']}: "
             + (f"{report['total_remaining']} credits across "
                f"{len(report['by_slot'])} slot(s)"
                if report.get("total_remaining") is not None else "balance unknown")
             + (f" — need {report['need']}" if report.get("need") else "")]
    for s in report.get("by_slot") or []:
        remaining = "retired" if s.get("retired") else (
            "unknown" if s.get("remaining") is None else f"{int(s['remaining'])}")
        note = f"  ({s['error']})" if s.get("error") and not s.get("retired") else ""
        lines.append(f"  {s['label']:<12} {remaining:>10}{note}")
    return "\n".join(lines)


def _ordered_slots(provider: str, pool: list[str]) -> list[tuple[int, str]]:
    """Slots to try, best first.

    Always starting at slot 0 means an exhausted primary costs every caller a
    doomed request until three consecutive failures retire it -- and retirement
    is per-process, so a fresh process pays again. The status file already
    records which slot last worked; start there and wrap. Slot 0 still gets
    tried, just not first, so a refilled primary is picked back up.
    """
    slots = list(enumerate(pool))
    if len(slots) < 2:
        return slots
    entry = (load_key_status().get(provider) or {})
    best, best_ok = 0, ""
    for raw_slot, info in entry.items():
        if not isinstance(info, dict) or info.get("status") != "ok":
            continue
        last_ok = str(info.get("last_ok") or "")
        if last_ok > best_ok and str(raw_slot).isdigit() and int(raw_slot) < len(slots):
            best, best_ok = int(raw_slot), last_ok
    if not best_ok or best == 0:
        return slots
    return slots[best:] + slots[:best]


def is_failover_http_status(code: int) -> bool:
    return code in FAILOVER_HTTP_CODES


def value_error_is_failover(exc: BaseException) -> bool:
    msg = str(exc)
    if _VALUE_ERROR_FAILOVER_RE.search(msg):
        return True
    if _SERPER_CREDIT_EXHAUSTED_RE.search(msg):
        return True
    return False


# (provider, slot) pairs already announced as a working fallback this process.
_ANNOUNCED_FALLBACKS: set = set()


def log_failover(*, provider: str, env_key: str, slot: int, code: int | str) -> None:
    # Once a slot is retired the loop skips it, so this stops repeating on its
    # own; the noise the operator sees is the run-up to retirement.
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
    """Call fn(api_key) trying each slot until success or pool exhausted.

    Session dead-slot tracking: after ``CONSECUTIVE_FAILURES_LIMIT`` consecutive
    failures, a slot is "retired" for the rest of the process lifetime.  Retired
    slots are skipped without attempting the call.
    """
    pool = api_key_pool(env_key)
    if not pool:
        raise ValueError(f"{env_key} not set")
    last_err: BaseException | None = None
    for slot, key in _ordered_slots(provider, pool):
        if _slot_is_retired(provider, slot):
            continue
        try:
            result = fn(key)
            record_key_usage(provider=provider, slot=slot, success=True)
            _record_success(provider, slot)
            # Only failures used to print, so a run surviving on a backup key
            # looked identical to a run that was simply broken -- pages of
            # "slot 0 failed, trying next" and no indication anything worked.
            # Announced once per provider+slot per process: the operator needs
            # to know the fallback took over, not to be told on every call.
            if slot > 0 and (provider, slot) not in _ANNOUNCED_FALLBACKS:
                _ANNOUNCED_FALLBACKS.add((provider, slot))
                print(
                    f"[outreachmagic] {provider}: backup key (slot {slot}) is working "
                    f"— proceeding normally.",
                    file=sys.stderr,
                    flush=True,
                )
            return result
        except urllib.error.HTTPError as exc:
            record_key_usage(provider=provider, slot=slot, success=False, error=f"HTTP {exc.code}")
            _record_failure(provider, slot)
            if _slot_is_retired(provider, slot):
                label = "Primary" if slot == 0 else f"Backup #{slot}"
                print(
                    f"[outreachmagic] {provider}: {label} (slot {slot}) retired "
                    f"after {CONSECUTIVE_FAILURES_LIMIT}+ failures. Skipping for rest of session.",
                    file=sys.stderr,
                    flush=True,
                )
            if not is_failover_http_status(exc.code):
                raise
            log_failover(provider=provider, env_key=env_key, slot=slot, code=exc.code)
            last_err = exc
        except ValueError as exc:
            record_key_usage(provider=provider, slot=slot, success=False, error=str(exc))
            _record_failure(provider, slot)
            if _slot_is_retired(provider, slot):
                label = "Primary" if slot == 0 else f"Backup #{slot}"
                print(
                    f"[outreachmagic] {provider}: {label} (slot {slot}) retired "
                    f"after {CONSECUTIVE_FAILURES_LIMIT}+ failures. Skipping for rest of session.",
                    file=sys.stderr,
                    flush=True,
                )
            if not value_error_is_failover(exc):
                raise
            log_failover(provider=provider, env_key=env_key, slot=slot, code="http")
            last_err = exc
        except BaseException as exc:
            # Anything else -- a KeyError from a missing config field, a socket
            # timeout, a bug in the provider adapter -- used to propagate
            # straight out of the loop WITHOUT recording anything, so the key
            # panel reported "never used" for a key that had just been called
            # and crashed. That is the most misleading of the three states it
            # can show. Record, then re-raise unchanged: this is an honesty
            # fix, not a new failover path, and a non-failover error must still
            # reach the caller.
            record_key_usage(
                provider=provider, slot=slot, success=False,
                error=f"{type(exc).__name__}: {str(exc)[:180]}")
            raise
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
    """Like call_with_key_pool for functions returning result dicts.

    Session dead-slot tracking: after ``CONSECUTIVE_FAILURES_LIMIT`` consecutive
    failures, a slot is retired for the rest of the process lifetime.
    """
    pool = api_key_pool(env_key)
    if not pool:
        return {"status": "no_key", "error": f"{env_key} not set", "provider": provider}
    last: dict = {"status": "error", "error": "no result", "provider": provider}
    for slot, key in _ordered_slots(provider, pool):
        if _slot_is_retired(provider, slot):
            continue
        try:
            result = fn(key)
        except BaseException as exc:
            # Same honesty fix as call_with_key_pool: an adapter that raises
            # instead of returning an error dict must not leave the slot
            # looking untouched.
            record_key_usage(
                provider=provider, slot=slot, success=False,
                error=f"{type(exc).__name__}: {str(exc)[:180]}")
            raise
        if result_should_failover(result, provider=provider):
            error = str(result.get("error") or result.get("status") or "failover")
            record_key_usage(provider=provider, slot=slot, success=False, error=error)
            _record_failure(provider, slot)
            if _slot_is_retired(provider, slot):
                label = "Primary" if slot == 0 else f"Backup #{slot}"
                print(
                    f"[outreachmagic] {provider}: {label} (slot {slot}) retired "
                    f"after {CONSECUTIVE_FAILURES_LIMIT}+ failures. Skipping for rest of session.",
                    file=sys.stderr,
                    flush=True,
                )
            code = result.get("http_status") or result.get("status")
            log_failover(provider=provider, env_key=env_key, slot=slot, code=code)
            last = result
            continue
        record_key_usage(provider=provider, slot=slot, success=True)
        _record_success(provider, slot)
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
                # "never_used" now means exactly that: a key is configured and
                # nothing has called it. It used to also absorb "called and
                # crashed" (the exception escaped before recording) and read
                # identically to a healthy idle key -- which is why the portal
                # showed Serper as "not used yet" after 1,023 calls.
                runtime_status = "never_used"
            keys.append({
                "slot": slot,
                "label": slot_label(slot),
                "prefix": prefix,
                "suffix": suffix,
                "status": runtime_status,
                # Both dates travel. "failing now, last worked 3 days ago" and
                # "failing now, never worked" are different problems, and the
                # panel cannot tell them apart from `status` alone.
                "last_used": slot_status.get("last_used"),
                "last_ok": slot_status.get("last_ok"),
                "last_error": slot_status.get("last_error"),
                "configured": True,
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
            last_ok = key.get("last_ok")
            detail = status
            if status == "ok" and last_used:
                detail = f"OK (last used {last_used})"
            elif status == "failed":
                # Say when it last worked, not only that it is broken now. That
                # is the difference between "the key expired on Tuesday" and
                # "this key has never worked", which need different responses.
                worked = f", last worked {last_ok}" if last_ok else ", never worked"
                detail = f"FAILED at {last_used}{worked} (last error: {last_error or 'unknown'})"
            elif status == "never_used":
                detail = "configured, not yet used"
            lines.append(f"  Slot {key.get('slot', 0)} ({label}): {fingerprint} — {detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
