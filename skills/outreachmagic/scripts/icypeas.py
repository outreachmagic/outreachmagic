"""Icypeas email finding provider."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import shared as cc
from credits import icypeas_credits_for_status
from normalize import normalize_linkedin, validate_domain
from waterfall import split_name

ICYPEAS_FIND_URL = "https://app.icypeas.com/api/email-search"
ICYPEAS_READ_URL = "https://app.icypeas.com/api/bulk-single-searchs/read"
HTTP_TIMEOUT = 30
ICYPEAS_PROCESSING_STATUSES = frozenset(("", "NONE", "SCHEDULED", "IN_PROGRESS"))
ICYPEAS_DEBITED_STATUSES = frozenset(("FOUND", "DEBITED", "DEBITED_NOT_FOUND"))


def is_icypeas_rate_limited(message: str) -> bool:
    m = (message or "").lower()
    return "exceeded the max number of requests" in m or "rate limit" in m


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
        endpoint = cc.validate_endpoint_url(
            cfg.get("icypeas_endpoint", ICYPEAS_FIND_URL),
            allowed_host_suffixes=["icypeas.com"],
        )
        req = urllib.request.Request(
            endpoint,
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

    _, _, call_with_key_pool_results = cc.require_api_key_pool()
    return call_with_key_pool_results(
        "ICYPEAS_API_KEY",
        lambda key: _icypeas_find_with_key(
            key, cfg, full_name=full_name, domain=domain, linkedin=linkedin
        ),
        provider="icypeas",
    )


def icypeas_poll_result(
    cfg: dict[str, Any],
    search_id: str,
    *,
    domain: str,
    full_name: str,
) -> dict[str, Any]:
    poll_attempts = max(1, int(cfg.get("icypeas_poll_attempts", 30)))
    poll_delay = float(cfg.get("icypeas_poll_delay_seconds", 3))
    read_url = cc.validate_endpoint_url(
        cfg.get("icypeas_read_endpoint", ICYPEAS_READ_URL),
        allowed_host_suffixes=["icypeas.com"],
    )
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
            "credits_used": icypeas_credits_for_status(status, email=email),
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
