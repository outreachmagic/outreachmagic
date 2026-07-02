"""Trykitt email finding provider."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import shared as cc
from credits import find_credits_used
from normalize import normalize_linkedin, validate_domain
from waterfall import CreditsExhaustedError

TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"
HTTP_TIMEOUT = 30


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
    endpoint = cc.validate_endpoint_url(
        cfg.get("trykitt_endpoint", TRYKITT_FIND_URL),
        allowed_host_suffixes=["trykitt.ai"],
    )
    req = urllib.request.Request(
        endpoint,
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
        "credits_used": find_credits_used(found=bool(email)),
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

    _, _, call_with_key_pool_results = cc.require_api_key_pool()
    result = call_with_key_pool_results(
        "TRYKITT_API_KEY",
        lambda key: _trykitt_find_with_key(key, cfg, full_name=full_name, domain=domain, linkedin=linkedin),
        provider="trykitt",
    )
    if result.get("status") == "http_error" and result.get("http_status") == 402:
        raise CreditsExhaustedError("trykitt out of credits")
    return result
