"""
RFC 8628-style device authorization against the Outreach Magic portal.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

from routing_cloud import get_api_base

_PLATFORM_FROM_DIR = {
    ".hermes": "hermes",
    ".cursor": "cursor",
    ".claude": "claude-code",
}

_PLATFORM_LABELS = {
    "hermes": "Hermes",
    "cursor": "Cursor",
    "claude-code": "Claude Code",
}


def _normalize_platform_name(raw: str) -> Optional[str]:
    key = raw.strip().lower()
    if key in ("hermes", "cursor", "claude-code"):
        return key
    if key in ("claude", "claude_code"):
        return "claude-code"
    return None


def detect_client_platform(*, override: Optional[str] = None) -> str:
    """Prefer explicit override, then the skill's install path, then env."""
    if override:
        normalized = _normalize_platform_name(override)
        if normalized:
            return normalized

    env = _normalize_platform_name(os.environ.get("OUTREACHMAGIC_PLATFORM") or "")
    if env:
        return env

    # Source of truth: where this script lives (e.g. ~/.hermes/skills/outreachmagic/scripts)
    try:
        for parent in Path(__file__).resolve().parents:
            plat = _PLATFORM_FROM_DIR.get(parent.name)
            if plat:
                return plat
    except OSError:
        pass

    return "unknown"


def default_client_label(*, platform: Optional[str] = None) -> str:
    host = socket.gethostname().split(".")[0] or "Computer"
    plat = platform or detect_client_platform()
    if plat == "unknown":
        try:
            skill_root = Path(__file__).resolve().parent.parent.name
            if skill_root and skill_root != "outreachmagic":
                return f"{host} ({skill_root})"
        except OSError:
            pass
        return host
    name = _PLATFORM_LABELS.get(plat, plat)
    return f"{host} ({name})"


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    bearer: Optional[str] = None,
    allow_errors: Optional[frozenset[str]] = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {}
        err = parsed.get("error") or parsed.get("message") or detail or exc.reason
        if allow_errors and err in allow_errors:
            return {"error": err}
        raise RuntimeError(f"HTTP {exc.code}: {err}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def start_device_authorization(
    load_config_fn: Callable[[], dict],
    *,
    platform: Optional[str] = None,
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    api_base = get_api_base(load_config_fn)
    client_platform = detect_client_platform(override=platform)
    client_label = default_client_label(platform=client_platform)

    body: dict[str, Any] = {
        "client_label": client_label,
        "client_platform": client_platform,
        "client_hostname": socket.gethostname(),
    }
    # Stable machine identity lets server-side login records be updated/reused.
    if client_id:
        body["client_id"] = client_id

    start = _post_json(f"{api_base}/api/device/code", body)

    device_code = start.get("device_code")
    user_code = start.get("user_code")
    verification_uri = start.get("verification_uri") or f"{api_base}/connect"
    expires_in = int(start.get("expires_in") or 900)
    interval = int(start.get("interval") or 5)

    if not device_code or not user_code:
        raise RuntimeError("Device authorization failed to start (invalid server response).")

    connect_url = f"{verification_uri}?user_code={str(user_code).replace('-', '')}"
    return {
        "api_base": api_base,
        "client_platform": client_platform,
        "client_label": client_label,
        "device_code": str(device_code),
        "user_code": str(user_code),
        "verification_uri": str(verification_uri),
        "connect_url": connect_url,
        "expires_in": expires_in,
        "interval": interval,
    }


def claim_device_token(
    api_base: str,
    *,
    device_code: str,
    wait_seconds: int = 30,
    interval: int = 5,
) -> dict[str, Optional[str]]:
    if wait_seconds < 0:
        wait_seconds = 0
    if interval <= 0:
        interval = 5

    deadline = time.time() + wait_seconds
    first_try = True

    while first_try or time.time() < deadline:
        first_try = False
        token_resp = _post_json(
            f"{api_base}/api/device/token",
            {
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            allow_errors=frozenset(
                {"authorization_pending", "expired_token", "access_denied", "invalid_grant"}
            ),
        )

        error = token_resp.get("error")
        if error == "authorization_pending":
            if wait_seconds == 0 or time.time() >= deadline:
                return {"status": "pending", "access_token": None, "error": error}
            sleep_for = min(interval, max(0.0, deadline - time.time()))
            if sleep_for > 0:
                time.sleep(sleep_for)
            continue
        if error:
            return {"status": str(error), "access_token": None, "error": str(error)}

        access_token = token_resp.get("access_token")
        if not access_token or not str(access_token).startswith("om_agent_"):
            raise RuntimeError("Invalid token received from server.")
        return {"status": "success", "access_token": str(access_token), "error": None}

    return {"status": "pending", "access_token": None, "error": "authorization_pending"}


def run_device_login(
    load_config_fn: Callable[[], dict],
    *,
    platform: Optional[str] = None,
    client_id: Optional[str] = None,
) -> str:
    flow = start_device_authorization(load_config_fn, platform=platform, client_id=client_id)
    device_code = str(flow["device_code"])
    connect_url = str(flow["connect_url"])
    user_code = str(flow["user_code"])
    expires_in = int(flow["expires_in"])
    interval = int(flow["interval"])
    api_base = str(flow["api_base"])

    print()
    print("  Connect Outreach Magic to this computer")
    print()
    print(f"  1. Open: {connect_url}")
    print(f"  2. Confirm code: {user_code}")
    print()
    print("  Waiting for authorization in your browser…")
    print()

    opened = False
    try:
        opened = bool(webbrowser.open(connect_url))
    except Exception:
        opened = False
    if not opened:
        try:
            system = platform.system().lower()
            if system == "darwin":
                subprocess.run(["open", connect_url], check=False)
                opened = True
            elif system == "windows":
                os.startfile(connect_url)  # type: ignore[attr-defined]
                opened = True
            else:
                subprocess.run(["xdg-open", connect_url], check=False)
                opened = True
        except Exception:
            opened = False

    if opened:
        print("  Opened your default browser.")
        print()

    claim = claim_device_token(
        api_base,
        device_code=device_code,
        wait_seconds=expires_in,
        interval=interval,
    )
    if claim.get("status") == "success":
        return str(claim["access_token"])
    if claim.get("status") and claim.get("status") != "pending":
        raise RuntimeError(str(claim.get("status")))

    raise RuntimeError(
        "Authorization timed out — open the link in your browser and confirm the code before it "
        "expires (this step cannot be completed in chat). Run: pipeline.py login"
    )
