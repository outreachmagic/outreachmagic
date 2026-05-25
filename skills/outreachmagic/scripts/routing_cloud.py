"""
Sync pipeline workspace routing config with wbhk-app (Neon source of truth).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_API_BASE = "https://dev.outreachmagic.io"
CONFIG_API_BASE_KEY = "api_base_url"
CONFIG_ROUTING_VERSION_KEY = "routing_config_version"

WORKSPACE_ROUTING_SINGLE = "single"
WORKSPACE_ROUTING_MULTI = "multi"


def get_api_base(load_config_fn) -> str:
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
        raise RuntimeError(f"Routing API {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Routing API unreachable: {exc.reason}") from exc


def fetch_routing_bundle(api_base: str, token: str) -> dict[str, Any]:
    return _request_json("GET", f"{api_base}/api/routing-config", token)


def apply_routing_bundle_to_sqlite(
    conn: sqlite3.Connection,
    bundle: dict[str, Any],
    *,
    org_id: str,
) -> None:
    mode = (bundle.get("mode") or WORKSPACE_ROUTING_SINGLE).strip().lower()
    default_ws_id = bundle.get("defaultWorkspaceId")
    conn.execute(
        """INSERT OR IGNORE INTO organizations (id, name, created_at)
           VALUES (?, 'Default Organization', datetime('now'))""",
        (org_id,),
    )
    conn.execute(
        """UPDATE organizations
           SET workspace_routing_mode = ?, default_workspace_id = ?
           WHERE id = ?""",
        (mode, default_ws_id if mode == WORKSPACE_ROUTING_SINGLE else None, org_id),
    )

    cloud_workspace_ids: list[str] = []
    for ws in bundle.get("workspaces") or []:
        ws_id = ws["id"]
        cloud_workspace_ids.append(ws_id)

        # A locally-generated workspace (e.g. id="ws_default") may already exist
        # with the same (org_id, slug) but a different id. Migrate child-table
        # references first (foreign_keys=ON means the parent update must come last).
        row = conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ? AND slug = ? AND id != ?",
            (org_id, ws["slug"], ws_id),
        ).fetchone()
        if row:
            old_id = row[0]
            for child_table in ("workspace_leads", "campaign_workspace_map"):
                conn.execute(
                    f"UPDATE {child_table} SET workspace_id = ? WHERE workspace_id = ?",
                    (ws_id, old_id),
                )
            conn.execute(
                "UPDATE workspaces SET id = ? WHERE id = ?",
                (ws_id, old_id),
            )

        conn.execute(
            """INSERT INTO workspaces (id, org_id, name, slug, created_at, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 slug = excluded.slug,
                 updated_at = datetime('now')""",
            (ws_id, org_id, ws["name"], ws["slug"]),
        )

    if cloud_workspace_ids:
        placeholders = ",".join("?" for _ in cloud_workspace_ids)
        conn.execute(
            f"""DELETE FROM workspaces
                WHERE org_id = ? AND id NOT IN ({placeholders})""",
            [org_id, *cloud_workspace_ids],
        )
    else:
        conn.execute("DELETE FROM workspaces WHERE org_id = ?", (org_id,))

    cloud_map_ids: list[str] = []
    for item in bundle.get("campaignMaps") or []:
        map_id = item["id"]
        cloud_map_ids.append(map_id)
        conn.execute(
            """INSERT INTO campaign_workspace_map (
                   id, org_id, source_platform, campaign_id, campaign_name_normalized,
                   workspace_id, match_strategy, priority, is_active, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 source_platform = excluded.source_platform,
                 campaign_id = excluded.campaign_id,
                 campaign_name_normalized = excluded.campaign_name_normalized,
                 workspace_id = excluded.workspace_id,
                 match_strategy = excluded.match_strategy,
                 priority = excluded.priority,
                 is_active = 1,
                 updated_at = datetime('now')""",
            (
                map_id,
                org_id,
                item["sourcePlatform"],
                item.get("campaignId"),
                item.get("campaignNameNormalized"),
                item["workspaceId"],
                item.get("matchStrategy") or "id_exact",
                item.get("priority") or 100,
            ),
        )

    if cloud_map_ids:
        placeholders = ",".join("?" for _ in cloud_map_ids)
        conn.execute(
            f"""DELETE FROM campaign_workspace_map
                WHERE org_id = ? AND id NOT IN ({placeholders})""",
            [org_id, *cloud_map_ids],
        )
    else:
        conn.execute("DELETE FROM campaign_workspace_map WHERE org_id = ?", (org_id,))


def sync_routing_from_cloud(
    conn: sqlite3.Connection,
    *,
    api_base: str,
    token: str,
    org_id: str,
    load_config_fn,
    save_config_fn,
    quiet: bool = False,
) -> Optional[dict[str, Any]]:
    bundle = fetch_routing_bundle(api_base, token)
    apply_routing_bundle_to_sqlite(conn, bundle, org_id=org_id)
    conn.commit()
    cfg = load_config_fn()
    cfg[CONFIG_ROUTING_VERSION_KEY] = bundle.get("version")
    cfg["workspace_routing_mode"] = bundle.get("mode")
    save_config_fn(cfg)
    if not quiet:
        ws_count = len(bundle.get("workspaces") or [])
        map_count = len(bundle.get("campaignMaps") or [])
        print(
            f"Routing config synced (v{bundle.get('version')}, "
            f"{ws_count} workspaces, {map_count} campaign maps, mode={bundle.get('mode')})."
        )
    return bundle


def cloud_routing_enabled(load_config_fn, token: Optional[str]) -> bool:
    return bool(token and get_api_base(load_config_fn))


def push_routing_mode(
    api_base: str,
    token: str,
    *,
    mode: str,
    default_workspace_slug: Optional[str] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": mode}
    if mode == WORKSPACE_ROUTING_SINGLE and default_workspace_slug:
        body["defaultWorkspaceSlug"] = default_workspace_slug
    return _request_json("PATCH", f"{api_base}/api/routing-config", token, body=body)


def push_workspace_create(api_base: str, token: str, *, name: str, slug: Optional[str] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if slug:
        body["slug"] = slug
    return _request_json("POST", f"{api_base}/api/routing-config/workspaces", token, body=body)


def push_campaign_map(
    api_base: str,
    token: str,
    *,
    source_platform: str,
    workspace_slug: str,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    match_strategy: Optional[str] = None,
    priority: int = 100,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sourcePlatform": source_platform,
        "workspaceSlug": workspace_slug,
        "priority": priority,
    }
    if campaign_id:
        body["campaignId"] = campaign_id
    if campaign_name:
        body["campaignName"] = campaign_name
    if match_strategy:
        body["matchStrategy"] = match_strategy
    return _request_json("POST", f"{api_base}/api/routing-config/campaign-maps", token, body=body)
