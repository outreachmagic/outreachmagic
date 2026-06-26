"""
Platform-specific field extraction from relay event `raw` payloads.

Each platform maps canonical field names to one or more dot-paths in the webhook
body (first non-empty wins). Platform specs live in platform_registry.py — run
`pipeline.py platform-map --json` to inspect mappings.
"""

from __future__ import annotations

from typing import Any, Optional

from platform_registry import (
    PLATFORM_BOUNCE_SPECS,
    PLATFORM_SPECS,
    _DEFAULT_BOUNCE_SPEC,
    _DEFAULT_SPEC,
    _IDENTITY_DEFAULT,
)


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _pick(data: dict, paths: tuple[str, ...]) -> Optional[str]:
    for path in paths:
        val = _get_path(data, path) if "." in path else data.get(path)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _extract_block(data: dict, spec: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, paths in spec.items():
        val = _pick(data, paths)
        if val:
            out[key] = val
    return out


def extract_relay_fields(platform: str, raw: dict | None) -> dict[str, dict[str, str]]:
    """Return {lead, event, signals, identity} with canonical string fields."""
    if not raw or not isinstance(raw, dict):
        return {"lead": {}, "event": {}, "signals": {}, "identity": {}}
    spec = PLATFORM_SPECS.get(platform, _DEFAULT_SPEC)
    signals_spec = spec.get("signals", _DEFAULT_SPEC.get("signals", {}))
    identity_spec = spec.get("identity", _IDENTITY_DEFAULT)
    return {
        "lead": _extract_block(raw, spec["lead"]),
        "event": _extract_block(raw, spec["event"]),
        "signals": _extract_block(raw, signals_spec) if signals_spec else {},
        "identity": _extract_block(raw, identity_spec),
    }


def extract_relay_identity(
    platform: str, raw: dict | None, envelope_lead: str = ""
) -> dict[str, str]:
    """Resolve email and LinkedIn from raw payload and relay envelope lead field."""
    fields = extract_relay_fields(platform, raw)
    identity = dict(fields.get("identity") or {})
    env = (envelope_lead or "").strip()
    if env:
        if "@" in env:
            identity.setdefault("email", env)
        else:
            identity.setdefault("linkedin_url", env)
    return identity


def build_display_name(lead: dict[str, str], email: Optional[str] = None) -> Optional[str]:
    first = lead.get("first_name")
    if not first:
        return None
    last = lead.get("last_name", "")
    return f"{first} {last}".strip() if last else first


def name_from_email(email: str) -> str:
    if not email or "@" not in email:
        return email or "Unknown"
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()


def extract_bounce_fields(platform: str, raw: dict | None) -> dict[str, str]:
    """Extract bounce diagnostics from a relay webhook raw payload."""
    if not raw or not isinstance(raw, dict):
        return {}
    spec = PLATFORM_BOUNCE_SPECS.get(platform, _DEFAULT_BOUNCE_SPEC)
    return _extract_block(raw, spec)
