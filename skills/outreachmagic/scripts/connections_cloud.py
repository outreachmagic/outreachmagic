"""
Manage webhook connections and fetch agent status via the portal API.
Mirrors the dashboard UI: create/delete tokens, view per-platform health.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


def _request_json(
    method: str,
    url: str,
    token: str,
    *,
    body: Optional[dict] = None,
) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise RuntimeError(f"API {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"API unreachable: {exc.reason}") from exc


def fetch_status(api_base: str, token: str) -> dict[str, Any]:
    return _request_json("GET", f"{api_base}/api/agent/status", token)


def list_tokens(api_base: str, token: str) -> dict[str, Any]:
    return _request_json("GET", f"{api_base}/api/tokens", token)


def create_token(api_base: str, token: str, *, platform: str) -> dict[str, Any]:
    return _request_json("POST", f"{api_base}/api/tokens/{platform}", token)


def delete_token(api_base: str, token: str, *, platform: str, token_id: str) -> dict[str, Any]:
    return _request_json(
        "DELETE",
        f"{api_base}/api/tokens/{platform}",
        token,
        body={"tokenId": token_id},
    )


def revoke_token(api_base: str, token: str, *, platform: str, token_id: str) -> dict[str, Any]:
    return _request_json(
        "PATCH",
        f"{api_base}/api/tokens/{platform}",
        token,
        body={"tokenId": token_id, "status": "revoked"},
    )


def activate_token(api_base: str, token: str, *, platform: str, token_id: str) -> dict[str, Any]:
    return _request_json(
        "PATCH",
        f"{api_base}/api/tokens/{platform}",
        token,
        body={"tokenId": token_id, "status": "active"},
    )
