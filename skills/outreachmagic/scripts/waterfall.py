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


def run_find_with_domain_fallback(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domains: list[str],
    linkedin: str = "",
    provider_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Try each candidate domain in ranked-best-first order, running the
    existing provider waterfall (run_find_with_fallback) against each one,
    and stop at the first domain+provider combination that returns an email
    -- the same "stop at first success" shape run_find_with_fallback already
    uses across providers, just one level up across domains. Does NOT try
    every domain once one succeeds (deliberately, for cost: N candidate
    domains would otherwise mean N x the API spend per lead).

    provider_attempts in the result covers every domain actually tried, each
    tagged with its own "domain" -- build_import_profile() (batch_runner.py)
    reads that per-attempt domain when present, so lead_provider_attempts
    ends up with the correct domain for every attempt, not just the winner.
    """
    if not domains:
        return {"status": "skipped", "reason": "no domains available", "provider_attempts": []}
    all_attempts: list[dict[str, Any]] = []
    last_result: dict[str, Any] = {}
    for domain in domains:
        result = run_find_with_fallback(
            cfg, full_name=full_name, domain=domain, linkedin=linkedin, provider_names=provider_names,
        )
        last_result = result
        for attempt in result.get("provider_attempts") or []:
            tagged = dict(attempt)
            tagged["domain"] = domain
            all_attempts.append(tagged)
        if result.get("email"):
            out = dict(result)
            out["provider_attempts"] = all_attempts
            out["winning_domain"] = domain
            return out
        if result.get("status") == "credits_exhausted":
            # No provider has credits left -- trying another domain can't
            # help, same stop condition run_find_with_fallback() itself uses
            # once every provider is exhausted.
            break
    out = dict(last_result) if last_result else {"status": "not_found"}
    out["provider_attempts"] = all_attempts
    out.setdefault("email", None)
    out.setdefault("validity", None)
    return out
