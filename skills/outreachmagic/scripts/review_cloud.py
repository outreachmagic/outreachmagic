"""Hosted dedup review API client (app.outreachmagic.io). No local Google credentials."""

from __future__ import annotations

import json
import sys
import time
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
        if exc.code == 403 and "review:write" in str(message).lower():
            message = (
                f"{message}. Ask Outreach Magic to log in again to refresh scopes "
                "(or revoke the key at app.outreachmagic.io/settings/agent and connect again)."
            )
        raise RuntimeError(f"Review API {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Review API unreachable: {exc.reason}") from exc


def review_enabled(load_config_fn: Callable[[], dict], get_agent_key_fn: Callable[[], Optional[str]]) -> bool:
    return bool(get_agent_key_fn() and get_api_base(load_config_fn))


def list_presets(api_base: str, token: str, *, template: str = "lead-review") -> dict[str, Any]:
    return _request_json(
        "GET",
        f"{api_base}/api/review/presets?template={template}",
        token,
    )


def export_review(
    api_base: str,
    token: str,
    *,
    template: str,
    title: str,
    share_email: Optional[str] = None,
    public_link: bool = False,
    sheet_id: Optional[str] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
    detail: Optional[str] = None,
    headers: Optional[list[str]] = None,
    rows: Optional[list[list[Any]]] = None,
    workspace: Optional[str] = None,
    columns: Optional[list[dict[str, Any]]] = None,
    freeze_header: Optional[bool] = None,
    fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "template": template,
        "title": title,
    }
    if sheet_id:
        body["sheet_id"] = sheet_id
    if share_email:
        body["share_email"] = share_email
    if public_link:
        body["public_link"] = True
    if template == "lead-review":
        body["detail"] = detail or "standard"
        body["headers"] = headers or []
        body["rows"] = rows or []
        if workspace:
            body["workspace"] = workspace
        if columns:
            body["columns"] = columns
        if freeze_header is not None:
            body["freeze_header"] = freeze_header
        if fields:
            body["fields"] = fields
    else:
        body["candidates"] = candidates or []
    row_count = len(body.get("rows") or [])
    if row_count:
        print(f"Uploading {row_count} rows to Google Sheets...", file=sys.stderr)
    started = time.monotonic()
    result = _request_json("POST", f"{api_base}/api/review/export", token, body=body)
    elapsed = round(time.monotonic() - started, 1)
    if isinstance(result, dict):
        timings = result.get("timings")
        if isinstance(timings, dict):
            print(
                "Export timing (seconds): "
                + ", ".join(f"{k}={v}" for k, v in timings.items()),
                file=sys.stderr,
            )
        else:
            print(f"Export upload completed in {elapsed}s.", file=sys.stderr)
    return result


def sync_read(
    api_base: str,
    token: str,
    *,
    sheet_id: str,
    template: str = "dedup-review",
    field_keys: Optional[dict[str, str]] = None,
    baseline_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "read",
        "sheet_id": sheet_id,
        "template": template,
    }
    if field_keys:
        body["field_keys"] = field_keys
    if baseline_rows:
        body["baseline_rows"] = baseline_rows
    return _request_json("POST", f"{api_base}/api/review/sync", token, body=body)


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
