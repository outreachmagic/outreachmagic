"""Email provider adapters (trykitt, icypeas)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from normalize import normalize_linkedin, validate_domain

TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"
ICYPEAS_FIND_URL = "https://app.icypeas.com/api/email-search"
ICYPEAS_READ_URL = "https://app.icypeas.com/api/bulk-single-searchs/read"
HTTP_TIMEOUT = 30


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


def trykitt_find(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    api_key = (cfg.get("trykitt_api_key") or "").strip()
    if not api_key:
        return {"error": "TRYKITT_API_KEY not set", "status": "no_key", "provider": "trykitt"}
    domain = domain.strip().lower().lstrip("@")
    if not validate_domain(domain):
        return {"error": "valid domain required", "status": "bad_input", "provider": "trykitt"}
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
            raise CreditsExhaustedError("trykitt out of credits")
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


def icypeas_find(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    api_key = (cfg.get("icypeas_api_key") or "").strip()
    if not api_key:
        return {"error": "ICYPEAS_API_KEY not set", "status": "no_key", "provider": "icypeas"}
    domain = domain.strip().lower().lstrip("@")
    if not validate_domain(domain):
        return {"error": "valid domain required", "status": "bad_input", "provider": "icypeas"}
    firstname, lastname = split_name(full_name)
    if not firstname and not lastname:
        return {"error": "valid name required", "status": "bad_input", "provider": "icypeas"}
    body = build_icypeas_payload(full_name, domain, linkedin)
    req = urllib.request.Request(
        cfg.get("icypeas_endpoint", ICYPEAS_FIND_URL),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
            "User-Agent": "email-finder/2.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {"status": "http_error", "http_status": e.code, "error": err_body[:500], "provider": "icypeas"}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"status": "error", "error": str(e), "provider": "icypeas"}

    if payload.get("success") is False:
        return {
            "status": "error",
            "error": str(payload.get("error") or payload.get("message") or "icypeas request failed"),
            "provider": "icypeas",
        }
    item = payload.get("item") or {}
    search_id = item.get("_id") or payload.get("_id")
    if not search_id:
        return {"status": "error", "error": "icypeas missing search id", "provider": "icypeas"}
    return icypeas_poll_result(cfg, str(search_id), domain=domain, full_name=full_name)


def icypeas_poll_result(
    cfg: dict[str, Any],
    search_id: str,
    *,
    domain: str,
    full_name: str,
) -> dict[str, Any]:
    poll_attempts = max(1, int(cfg.get("icypeas_poll_attempts", 8)))
    poll_delay = float(cfg.get("icypeas_poll_delay_seconds", 2))
    read_url = cfg.get("icypeas_read_endpoint", ICYPEAS_READ_URL)
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
            return {"status": "http_error", "http_status": e.code, "error": err_body[:500], "provider": "icypeas"}
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return {"status": "error", "error": str(e), "provider": "icypeas"}

        if payload.get("success") is False:
            return {
                "status": "error",
                "error": str(payload.get("error") or payload.get("message") or "icypeas read failed"),
                "provider": "icypeas",
            }
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        item = items[0] if items else {}
        status = str(item.get("status") or "").upper()
        processing = status in ("", "NONE", "SCHEDULED", "IN_PROGRESS")
        if processing and attempt + 1 < poll_attempts:
            time.sleep(max(0.0, poll_delay))
            continue

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
            "credits_used": 0.003,
        }
    return {
        "status": "error",
        "error": "icypeas polling exhausted",
        "jobId": search_id,
        "provider": "icypeas",
        "domain": domain,
        "full_name": full_name,
    }


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
        final = dict(attempts[-1]) if attempts else {}
        if final.get("status") == "error" and not final.get("email"):
            pass
        else:
            final.setdefault("status", "not_found")
        final.setdefault("email", None)
        final.setdefault("validity", None)
        final["provider_attempts"] = attempts
        return final
    return {"status": "skipped", "reason": "no providers available", "provider_attempts": attempts}
