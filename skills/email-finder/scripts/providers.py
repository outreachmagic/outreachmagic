"""Email provider adapters (trykitt, icypeas)."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from normalize import normalize_linkedin, validate_domain

TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"
ICYPEAS_FIND_URL = "https://app.icypeas.com/api/email-search"
ICYPEAS_READ_URL = "https://app.icypeas.com/api/bulk-single-searchs/read"
HTTP_TIMEOUT = 30
ICYPEAS_CREDIT_COST = 0.003
ICYPEAS_PROCESSING_STATUSES = frozenset(("", "NONE", "SCHEDULED", "IN_PROGRESS"))
ICYPEAS_DEBITED_STATUSES = frozenset(("FOUND", "DEBITED", "DEBITED_NOT_FOUND"))


def _import_key_pool() -> Optional[
    Tuple[Callable[..., dict], Callable[[str], list[str]]]
]:
    for base in (
        Path(__file__).resolve().parent.parent.parent / "outreachmagic" / "scripts",
        Path.home() / ".hermes" / "skills" / "outreachmagic" / "scripts",
        Path.home() / ".cursor" / "skills" / "outreachmagic" / "scripts",
        Path.home() / ".claude" / "skills" / "outreachmagic" / "scripts",
    ):
        if not (base / "api_key_pool.py").is_file():
            continue
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        try:
            from api_key_pool import api_key_pool, call_with_key_pool_results
            return call_with_key_pool_results, api_key_pool
        except ImportError:
            continue
    return None


def is_icypeas_rate_limited(message: str) -> bool:
    m = (message or "").lower()
    return "exceeded the max number of requests" in m or "rate limit" in m


def icypeas_credits_for_status(status: str, *, cfg: Optional[dict[str, Any]] = None) -> float:
    cost = float((cfg or {}).get("icypeas_credit_cost", ICYPEAS_CREDIT_COST))
    return cost if (status or "").upper() in ICYPEAS_DEBITED_STATUSES else 0.0


def icypeas_poll_wait_seconds(attempt: int, base_delay: float) -> float:
    wait = base_delay * (1.5 ** attempt)
    return min(max(0.0, wait), 30.0)


def _icypeas_auth_error_result(*, domain: str, full_name: str, error: str) -> dict[str, Any]:
    return {
        "status": "auth_error",
        "error": error,
        "email": None,
        "provider": "icypeas",
        "domain": domain,
        "full_name": full_name,
        "credits_used": 0.0,
    }


def _icypeas_rate_limited_result(*, domain: str, full_name: str, error: str) -> dict[str, Any]:
    return {
        "status": "rate_limited",
        "error": error,
        "email": None,
        "provider": "icypeas",
        "domain": domain,
        "full_name": full_name,
        "credits_used": 0.0,
    }


class CreditsExhaustedError(RuntimeError):
    pass


def cfg_bool(cfg: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return bool(raw)


def split_name(full_name: str) -> tuple[str, str]:
    cleaned = " ".join((full_name or "").split()).strip()
    if not cleaned:
        return "", ""
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def validity_to_verify_status(validity: str, *, provider: str) -> str:
    v = (validity or "").strip().lower()
    if provider == "icypeas":
        if v in ("ultra_sure", "sure", "valid"):
            return "valid"
        if v in ("probable", "risky", "valid-risky"):
            return "catch_all"
        return "unknown"
    if v == "valid":
        return "valid"
    if v in ("valid-risky", "risky"):
        return "catch_all"
    if v == "invalid":
        return "invalid"
    return "unknown"


def provider_note_text(provider: str, validity: str, *, found: bool) -> str:
    if provider == "icypeas":
        if not found:
            return "icypeas: no email found"
        v = (validity or "").lower()
        if v:
            return f"icypeas certainty: {v}"
        return "icypeas: email found"
    if not found:
        return "trykitt: no email found"
    v = (validity or "").lower()
    if v == "valid":
        return "trykitt verify: valid"
    if v in ("valid-risky", "risky"):
        return "trykitt verify: catch_all"
    if v:
        return f"trykitt verify: {v}"
    return "trykitt verify: unknown"


def build_trykitt_payload(full_name: str, domain: str, linkedin: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {
        "fullName": full_name.strip(),
        "domain": domain.strip().lower().lstrip("@"),
        "realtime": True,
    }
    li = normalize_linkedin(linkedin)
    if li:
        body["linkedinStandardProfileURL"] = li
    return body


def build_icypeas_payload(full_name: str, domain: str, linkedin: str = "") -> dict[str, Any]:
    first, last = split_name(full_name)
    body: dict[str, Any] = {
        "firstname": first,
        "lastname": last,
        "domainOrCompany": domain.strip().lower().lstrip("@"),
    }
    li = normalize_linkedin(linkedin)
    if li:
        body["linkedinUrl"] = li
    return body


def _trykitt_find_with_key(
    api_key: str,
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    body = build_trykitt_payload(full_name, domain, linkedin)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        cfg.get("trykitt_endpoint", TRYKITT_FIND_URL),
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "User-Agent": "email-finder/2.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 500 and "out of credits" in err_body.lower():
            return {
                "status": "http_error",
                "http_status": 402,
                "error": err_body[:500],
                "provider": "trykitt",
            }
        return {"status": "http_error", "http_status": e.code, "error": err_body[:500], "provider": "trykitt"}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"status": "error", "error": str(e), "provider": "trykitt"}

    credits = payload.get("credits") or {}
    job_credits = float(credits.get("jobCredits") or 0)
    if credits:
        credits = dict(credits)
    email = (payload.get("email") or "").strip()
    if email == "no-results-found":
        email = ""
    validity = (payload.get("validity") or "").strip()
    return {
        "status": "found" if email else "not_found",
        "email": email or None,
        "validity": validity or None,
        "validSMTP": payload.get("validSMTP"),
        "jobId": payload.get("jobId"),
        "provider": "trykitt",
        "domain": domain,
        "full_name": full_name,
        "credits_used": job_credits,
        "credits": credits,
    }


def trykitt_find(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    domain = domain.strip().lower().lstrip("@")
    if not validate_domain(domain):
        return {"error": "valid domain required", "status": "bad_input", "provider": "trykitt"}

    pool_fn = _import_key_pool()
    if pool_fn:
        call_with_key_pool_results, api_key_pool = pool_fn
        if api_key_pool("TRYKITT_API_KEY"):
            result = call_with_key_pool_results(
                "TRYKITT_API_KEY",
                lambda key: _trykitt_find_with_key(key, cfg, full_name=full_name, domain=domain, linkedin=linkedin),
                provider="trykitt",
            )
            if result.get("status") == "http_error" and result.get("http_status") == 402:
                raise CreditsExhaustedError("trykitt out of credits")
            return result

    api_key = (cfg.get("trykitt_api_key") or "").strip()
    if not api_key:
        return {"error": "TRYKITT_API_KEY not set", "status": "no_key", "provider": "trykitt"}
    result = _trykitt_find_with_key(api_key, cfg, full_name=full_name, domain=domain, linkedin=linkedin)
    if result.get("status") == "http_error" and result.get("http_status") == 402:
        raise CreditsExhaustedError("trykitt out of credits")
    return result


def _icypeas_find_with_key(
    api_key: str,
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    body = build_icypeas_payload(full_name, domain, linkedin)
    payload: dict[str, Any] = {}
    for post_attempt in range(2):
        req = urllib.request.Request(
            cfg.get("icypeas_endpoint", ICYPEAS_FIND_URL),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": api_key,
                "User-Agent": "email-finder/2.2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (401, 403) or "UserNotFoundError" in err_body:
                return _icypeas_auth_error_result(domain=domain, full_name=full_name, error=err_body[:500])
            if is_icypeas_rate_limited(err_body) and post_attempt == 0:
                time.sleep(float(cfg.get("icypeas_rate_limit_retry_seconds", 3)))
                continue
            if is_icypeas_rate_limited(err_body):
                return _icypeas_rate_limited_result(domain=domain, full_name=full_name, error=err_body[:500])
            return {"status": "http_error", "http_status": e.code, "error": err_body[:500], "provider": "icypeas"}
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return {"status": "error", "error": str(e), "provider": "icypeas"}
    else:
        return {"status": "rate_limited", "error": "icypeas rate limited", "provider": "icypeas"}

    if payload.get("success") is False:
        err = str(payload.get("error") or payload.get("message") or "icypeas request failed")
        if is_icypeas_rate_limited(err):
            return _icypeas_rate_limited_result(domain=domain, full_name=full_name, error=err)
        return {
            "status": "error",
            "error": err,
            "provider": "icypeas",
        }
    item = payload.get("item") or {}
    search_id = item.get("_id") or payload.get("_id")
    if not search_id:
        return {"status": "error", "error": "icypeas missing search id", "provider": "icypeas"}
    cfg_with_key = dict(cfg)
    cfg_with_key["icypeas_api_key"] = api_key
    return icypeas_poll_result(cfg_with_key, str(search_id), domain=domain, full_name=full_name)


def icypeas_find(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    domain = domain.strip().lower().lstrip("@")
    if not validate_domain(domain):
        return {"error": "valid domain required", "status": "bad_input", "provider": "icypeas"}
    firstname, lastname = split_name(full_name)
    if not firstname and not lastname:
        return {"error": "valid name required", "status": "bad_input", "provider": "icypeas"}

    pool_fn = _import_key_pool()
    if pool_fn:
        call_with_key_pool_results, api_key_pool = pool_fn
        if api_key_pool("ICYPEAS_API_KEY"):
            return call_with_key_pool_results(
                "ICYPEAS_API_KEY",
                lambda key: _icypeas_find_with_key(
                    key, cfg, full_name=full_name, domain=domain, linkedin=linkedin
                ),
                provider="icypeas",
            )

    api_key = (cfg.get("icypeas_api_key") or "").strip()
    if not api_key:
        return {"error": "ICYPEAS_API_KEY not set", "status": "no_key", "provider": "icypeas"}
    return _icypeas_find_with_key(api_key, cfg, full_name=full_name, domain=domain, linkedin=linkedin)


def icypeas_poll_result(
    cfg: dict[str, Any],
    search_id: str,
    *,
    domain: str,
    full_name: str,
) -> dict[str, Any]:
    poll_attempts = max(1, int(cfg.get("icypeas_poll_attempts", 30)))
    poll_delay = float(cfg.get("icypeas_poll_delay_seconds", 3))
    read_url = cfg.get("icypeas_read_endpoint", ICYPEAS_READ_URL)
    last_status = ""
    for attempt in range(poll_attempts):
        req = urllib.request.Request(
            read_url,
            data=json.dumps({"id": search_id}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": str(cfg.get("icypeas_api_key") or ""),
                "User-Agent": "email-finder/2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (401, 403) or "UserNotFoundError" in err_body:
                return _icypeas_auth_error_result(domain=domain, full_name=full_name, error=err_body[:500])
            if is_icypeas_rate_limited(err_body):
                return _icypeas_rate_limited_result(domain=domain, full_name=full_name, error=err_body[:500])
            return {"status": "http_error", "http_status": e.code, "error": err_body[:500], "provider": "icypeas"}
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return {"status": "error", "error": str(e), "provider": "icypeas"}

        if payload.get("success") is False:
            err = str(payload.get("error") or payload.get("message") or "icypeas read failed")
            if "UserNotFoundError" in err or "user_not_found" in err.lower():
                return _icypeas_auth_error_result(domain=domain, full_name=full_name, error=err)
            if is_icypeas_rate_limited(err):
                return _icypeas_rate_limited_result(domain=domain, full_name=full_name, error=err)
            return {
                "status": "error",
                "error": err,
                "provider": "icypeas",
            }
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        item = items[0] if items else {}
        status = str(item.get("status") or "").upper()
        last_status = status
        processing = status in ICYPEAS_PROCESSING_STATUSES
        if processing and attempt + 1 < poll_attempts:
            time.sleep(icypeas_poll_wait_seconds(attempt, poll_delay))
            continue
        if processing:
            return {
                "status": "error",
                "error": "icypeas_timeout",
                "email": None,
                "jobId": search_id,
                "provider": "icypeas",
                "icypeas_status": status or None,
                "domain": domain,
                "full_name": full_name,
                "credits_used": 0.0,
            }

        results = item.get("results") if isinstance(item.get("results"), dict) else {}
        emails = results.get("emails") if isinstance(results.get("emails"), list) else []
        first_email = emails[0] if emails and isinstance(emails[0], dict) else {}
        email = str(first_email.get("email") or "").strip()
        certainty = str(first_email.get("certainty") or "").strip()
        credits = icypeas_credits_for_status(status, cfg=cfg)
        return {
            "status": "found" if email else "not_found",
            "email": email or None,
            "validity": certainty or None,
            "validSMTP": None,
            "jobId": search_id,
            "provider": "icypeas",
            "icypeas_status": status or None,
            "domain": domain,
            "full_name": full_name,
            "credits_used": credits,
        }
    return {
        "status": "error",
        "error": "icypeas_timeout",
        "jobId": search_id,
        "provider": "icypeas",
        "icypeas_status": last_status or None,
        "domain": domain,
        "full_name": full_name,
        "credits_used": 0.0,
    }


def provider_request_delay_seconds(
    cfg: dict[str, Any],
    provider_names: list[str],
    *,
    cli_delay: float = 0.0,
) -> float:
    """Per-lead throttle before API calls (applied for all worker counts)."""
    icypeas_delay = float(cfg.get("icypeas_request_delay_seconds", 1.5))
    trykitt_delay = float(cfg.get("trykitt_request_delay_seconds", 0.2))
    if len(provider_names) == 1 and provider_names[0] == "icypeas":
        base = icypeas_delay
    elif len(provider_names) == 1 and provider_names[0] == "trykitt":
        base = trykitt_delay
    elif "icypeas" in provider_names:
        base = icypeas_delay
    else:
        base = trykitt_delay
    if cli_delay > 0:
        return max(base, cli_delay)
    return base


def resolve_provider_names(cfg: dict[str, Any], cli_provider: Optional[str] = None) -> list[str]:
    names: list[str] = []
    if cfg_bool(cfg, "trykitt_enabled", True):
        names.append("trykitt")
    if cfg_bool(cfg, "icypeas_enabled", True):
        names.append("icypeas")
    if cli_provider:
        cli_provider = cli_provider.strip().lower()
        if cli_provider not in names:
            return []
        return [cli_provider]
    return names


def run_find_with_fallback(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
    provider_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    providers = provider_names or resolve_provider_names(cfg)
    if not providers:
        return {"status": "skipped", "reason": "no providers enabled", "provider_attempts": []}
    attempts: list[dict[str, Any]] = []
    last_res: dict[str, Any] = {}
    for provider in providers:
        try:
            if provider == "trykitt":
                res = trykitt_find(cfg, full_name=full_name, domain=domain, linkedin=linkedin)
            elif provider == "icypeas":
                res = icypeas_find(cfg, full_name=full_name, domain=domain, linkedin=linkedin)
            else:
                continue
        except CreditsExhaustedError as e:
            attempts.append({
                "provider": provider,
                "status": "error",
                "error": str(e),
                "attempted": True,
            })
            continue
        last_res = res
        attempt = {
            "provider": provider,
            "status": res.get("status"),
            "error": res.get("error"),
            "attempted": res.get("status") not in ("no_key", "bad_input"),
        }
        attempts.append(attempt)
        if res.get("email"):
            res["provider_attempts"] = attempts
            return res
    if attempts:
        credit_errors = [
            a for a in attempts
            if isinstance(a, dict)
            and a.get("status") == "error"
            and "credit" in str(a.get("error") or "").lower()
        ]
        if credit_errors and len(credit_errors) == len(attempts):
            return {
                "status": "credits_exhausted",
                "error": "all providers exhausted credits",
                "email": None,
                "validity": None,
                "provider_attempts": attempts,
            }
        final = dict(last_res) if last_res else {}
        st = final.get("status")
        if st not in ("error", "rate_limited", "http_error", "auth_error", "not_found", "found"):
            final.setdefault("status", "not_found")
        elif st == "error" and str(final.get("error") or "") != "icypeas_timeout":
            if not final.get("email"):
                final.setdefault("status", "not_found")
        final.setdefault("email", None)
        final.setdefault("validity", None)
        final["provider_attempts"] = attempts
        return final
    return {"status": "skipped", "reason": "no providers available", "provider_attempts": attempts}
