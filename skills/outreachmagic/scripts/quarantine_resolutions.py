"""Cloud-synced quarantine resolutions (relay queue_resolutions table)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from db_conn import get_conn
from workspace_routing import DEFAULT_ORG_ID

RELAY_RESOLVE_PATH = "/resolve-queue"
SNAPSHOT_RELAY_ID_BASE = 1_000_000_000


def parse_queue_resolutions(raw: list | None) -> dict[int, dict[str, Any]]:
    """relay_id -> {status, workspace_slug?}."""
    out: dict[int, dict[str, Any]] = {}
    for item in raw or []:
        try:
            relay_id = int(item.get("relay_id"))
        except (TypeError, ValueError):
            continue
        if relay_id <= 0 or relay_id >= SNAPSHOT_RELAY_ID_BASE:
            continue
        status = str(item.get("status") or "").lower()
        if status not in ("skipped", "assigned"):
            continue
        if status == "assigned":
            slug = str(item.get("workspace_slug") or "").strip()
            if not slug:
                continue
            out[relay_id] = {"status": status, "workspace_slug": slug}
        else:
            out[relay_id] = {"status": status}
    return out


class WorkspaceSlugCache:
    def __init__(self, org_id: str = DEFAULT_ORG_ID):
        self.org_id = org_id
        self._by_slug: dict[str, str] = {}

    def workspace_id(self, slug: str) -> Optional[str]:
        slug = (slug or "").strip()
        if not slug:
            return None
        if slug in self._by_slug:
            return self._by_slug[slug]
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
            (self.org_id, slug),
        ).fetchone()
        conn.close()
        if not row:
            return None
        self._by_slug[slug] = row["id"]
        return row["id"]


def push_resolutions_to_relay(
    relay_url: str,
    agent_key: str,
    resolves: list[dict],
    *,
    version: str,
    timeout: int = 60,
) -> dict:
    if not resolves:
        return {"status": "ok", "synced": 0, "errors": []}
    body = json.dumps({"resolves": resolves}).encode()
    req = urllib.request.Request(
        f"{relay_url}{RELAY_RESOLVE_PATH}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {agent_key}",
            "User-Agent": f"Outreach Magic/{version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode() if e.fp else str(e)
        return {"status": "error", "error": msg, "http_status": e.code}
    except urllib.error.URLError as e:
        return {"status": "error", "error": str(e.reason)}
