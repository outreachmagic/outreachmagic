"""Hosted dedup review API client (app.outreachmagic.io). No local Google credentials."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_API_BASE = "https://app.outreachmagic.io"
CONFIG_API_BASE_KEY = "api_base_url"


def get_api_base(load_config_fn: Callable[[], dict]) -> str:
    cfg = load_config_fn()
    return (cfg.get(CONFIG_API_BASE_KEY) or DEFAULT_API_BASE).rstrip("/")


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
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise RuntimeError(f"Review API {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Review API unreachable: {exc.reason}") from exc


def review_enabled(load_config_fn: Callable[[], dict], get_agent_key_fn: Callable[[], Optional[str]]) -> bool:
    return bool(get_agent_key_fn() and get_api_base(load_config_fn))


def export_review(
    api_base: str,
    token: str,
    *,
    template: str,
    candidates: list[dict[str, Any]],
    title: str,
    share_email: Optional[str] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "template": template,
        "title": title,
        "candidates": candidates,
    }
    if share_email:
        body["share_email"] = share_email
    return _request_json("POST", f"{api_base}/api/review/export", token, body=body)


def sync_read(api_base: str, token: str, *, sheet_id: str, template: str = "dedup-review") -> dict[str, Any]:
    return _request_json(
        "POST",
        f"{api_base}/api/review/sync",
        token,
        body={"action": "read", "sheet_id": sheet_id, "template": template},
    )


def sync_write_results(
    api_base: str,
    token: str,
    *,
    sheet_id: str,
    results: list[dict[str, Any]],
    template: str = "dedup-review",
) -> dict[str, Any]:
    return _request_json(
        "POST",
        f"{api_base}/api/review/sync",
        token,
        body={
            "action": "write_results",
            "sheet_id": sheet_id,
            "template": template,
            "results": results,
        },
    )
