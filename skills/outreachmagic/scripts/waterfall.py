"""Email provider orchestration and utilities."""

from __future__ import annotations

from typing import Any, Optional


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
    prov = (provider or "").strip().lower()
    if prov == "icypeas":
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
    from trykitt import trykitt_find
    from icypeas import icypeas_find

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
