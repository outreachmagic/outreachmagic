"""
RFC 8628-style device authorization against the Outreach Magic portal.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Callable, Optional

from routing_cloud import get_api_base


def detect_client_platform() -> str:
    env = (os.environ.get("OUTREACHMAGIC_PLATFORM") or "").strip().lower()
    if env in ("hermes", "cursor", "claude-code", "claude"):
        return "claude-code" if env == "claude" else env
    home = os.path.expanduser("~")
    if os.path.isdir(os.path.join(home, ".cursor", "skills", "outreachmagic")):
        return "cursor"
    if os.path.isdir(os.path.join(home, ".claude", "skills", "outreachmagic")):
        return "claude-code"
    if os.path.isdir(os.path.join(home, ".hermes", "skills", "outreachmagic")):
        return "hermes"
    return "unknown"


def default_client_label() -> str:
    host = socket.gethostname().split(".")[0] or "Computer"
    plat = detect_client_platform()
    if plat == "unknown":
        return host
    name = {"cursor": "Cursor", "claude-code": "Claude Code", "hermes": "Hermes"}.get(plat, plat)
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


def run_device_login(load_config_fn: Callable[[], dict]) -> str:
    api_base = get_api_base(load_config_fn)
    client_platform = detect_client_platform()
    client_label = default_client_label()

    start = _post_json(
        f"{api_base}/api/device/code",
        {
            "client_label": client_label,
            "client_platform": client_platform,
            "client_hostname": socket.gethostname(),
        },
    )

    device_code = start.get("device_code")
    user_code = start.get("user_code")
    verification_uri = start.get("verification_uri") or f"{api_base}/connect"
    expires_in = int(start.get("expires_in") or 900)
    interval = int(start.get("interval") or 5)

    if not device_code or not user_code:
        raise RuntimeError("Device authorization failed to start (invalid server response).")

    connect_url = f"{verification_uri}?user_code={user_code.replace('-', '')}"
    print()
    print("  Connect Outreach Magic to this computer")
    print()
    print(f"  1. Open: {connect_url}")
    print(f"  2. Confirm code: {user_code}")
    print()
    print("  Waiting for authorization in your browser…")
    print()

    try:
        webbrowser.open(connect_url)
    except Exception:
        pass

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
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

        if token_resp.get("error") == "authorization_pending":
            continue
        if token_resp.get("error"):
            raise RuntimeError(token_resp.get("error"))

        access_token = token_resp.get("access_token")
        if not access_token or not str(access_token).startswith("om_agent_"):
            raise RuntimeError("Invalid token received from server.")
        return str(access_token)

    raise RuntimeError("Authorization timed out. Run login again.")
