"""
Sync pipeline workspace routing config with wbhk-app (Neon source of truth).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_API_BASE = "https://app.outreachmagic.io"
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


def campaign_map_signature(
    *,
    source_platform: str,
    match_strategy: str,
    campaign_id: Optional[str],
    campaign_name_normalized: Optional[str],
    workspace_slug: str,
) -> tuple[str, str, Optional[str], Optional[str], str]:
    """Stable key for comparing local and cloud routing rules."""
    platform = (source_platform or "*").strip().lower()
    strategy = (match_strategy or "id_exact").strip().lower()
    cid = (campaign_id or "").strip() or None
    cname = (campaign_name_normalized or "").strip().lower() or None
    slug = (workspace_slug or "").strip().lower()
    return (platform, strategy, cid, cname, slug)


def cloud_campaign_map_signatures(bundle: dict[str, Any]) -> set[tuple[str, str, Optional[str], Optional[str], str]]:
    sigs: set[tuple[str, str, Optional[str], Optional[str], str]] = set()
    for item in bundle.get("campaignMaps") or []:
        slug = item.get("workspaceSlug") or ""
        if not slug:
            continue
        sigs.add(
            campaign_map_signature(
                source_platform=item.get("sourcePlatform") or "*",
                match_strategy=item.get("matchStrategy") or "id_exact",
                campaign_id=item.get("campaignId"),
                campaign_name_normalized=item.get("campaignNameNormalized"),
                workspace_slug=slug,
            )
        )
    return sigs


def delete_campaign_map(api_base: str, token: str, map_id: str) -> dict[str, Any]:
    return _request_json(
        "DELETE",
        f"{api_base}/api/routing-config/campaign-maps/{map_id}",
        token,
    )


def apply_routing_bundle_to_sqlite(
    conn: sqlite3.Connection,
    bundle: dict[str, Any],
    *,
    org_id: str,
) -> None:
    # Defer FK checks so workspace-id migrations (child update → parent update)
    # don't fail mid-transaction; constraints are enforced at commit time.
    conn.execute("PRAGMA defer_foreign_keys = ON")

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

        row = conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ? AND slug = ? AND id != ?",
            (org_id, ws["slug"], ws_id),
        ).fetchone()
        if row:
            old_id = row[0]
            for child_table in (
                "workspace_leads",
                "campaign_workspace_map",
                "workspace_lead_tags",
                "workspace_lead_linkedin_status",
            ):
                conn.execute(
                    f"UPDATE {child_table} SET workspace_id = ? WHERE workspace_id = ?",
                    (ws_id, old_id),
                )
            conn.execute(
                "UPDATE workspaces SET id = ? WHERE id = ?",
                (ws_id, old_id),
            )

        conn.execute(
            """INSERT INTO workspaces (id, org_id, name, slug, cloud_synced, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 slug = excluded.slug,
                 cloud_synced = 1,
                 updated_at = datetime('now')""",
            (ws_id, org_id, ws["name"], ws["slug"]),
        )

    if cloud_workspace_ids:
        placeholders = ",".join("?" for _ in cloud_workspace_ids)
        conn.execute(
            f"""DELETE FROM workspaces
                WHERE org_id = ? AND cloud_synced = 1 AND id NOT IN ({placeholders})""",
            [org_id, *cloud_workspace_ids],
        )
    else:
        conn.execute("DELETE FROM workspaces WHERE org_id = ? AND cloud_synced = 1", (org_id,))

    cloud_map_ids: list[str] = []
    for item in bundle.get("campaignMaps") or []:
        if not item.get("campaignId") and not item.get("campaignNameNormalized"):
            continue
        map_id = item["id"]
        cloud_map_ids.append(map_id)
        # Remove any existing row that would conflict on the partial unique index
        # (same org/platform/campaign_id but different id)
        if item.get("campaignId"):
            conn.execute(
                """DELETE FROM campaign_workspace_map
                   WHERE org_id = ? AND source_platform = ? AND campaign_id = ?
                     AND id != ? AND is_active = 1""",
                (org_id, item["sourcePlatform"], item["campaignId"], map_id),
            )
        conn.execute(
            """INSERT INTO campaign_workspace_map (
                   id, org_id, source_platform, campaign_id, campaign_name_normalized,
                   workspace_id, match_strategy, priority, is_active, cloud_synced, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, datetime('now'), datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 source_platform = excluded.source_platform,
                 campaign_id = excluded.campaign_id,
                 campaign_name_normalized = excluded.campaign_name_normalized,
                 workspace_id = excluded.workspace_id,
                 match_strategy = excluded.match_strategy,
                 priority = excluded.priority,
                 is_active = 1,
                 cloud_synced = 1,
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
                WHERE org_id = ? AND cloud_synced = 1 AND id NOT IN ({placeholders})""",
            [org_id, *cloud_map_ids],
        )
    else:
        conn.execute("DELETE FROM campaign_workspace_map WHERE org_id = ? AND cloud_synced = 1", (org_id,))


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


def push_db_health(api_base: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST aggregate local DB health (no lead PII). Explicit sync only."""
    return _request_json("POST", f"{api_base}/api/agent/db-health", token, body=payload)


def push_api_key_status(api_base: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST aggregate runtime API key status (no secret values)."""
    return _request_json("POST", f"{api_base}/api/agent/api-key-status", token, body=payload)


def fetch_portal_config(api_base: str, token: str) -> dict[str, Any]:
    """Fetch portal configuration bundle from the cloud."""
    return _request_json("GET", f"{api_base}/api/portal-config", token)


def _apply_crm_config_to_sqlite(
    conn: sqlite3.Connection,
    crm_config: dict[str, dict[str, dict[str, Any]]],
    *,
    org_id: str,
) -> None:
    """Write CRM workspace config rows from a portal config bundle.

    For each workspace in ``crm_config``, upserts one row per platform.
    Existing rows for the org's workspaces that are not in the incoming
    config are removed (the caller sends the full picture each sync).
    """
    ws_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ?", (org_id,)
        ).fetchall()
    ]
    if not ws_ids:
        return

    seen: set[tuple[str, str]] = set()

    for ws_id, platforms in crm_config.items():
        for platform, cfg in platforms.items():
            seen.add((ws_id, platform))
            stage_mapping = json.dumps(cfg.get("stage_mapping") or {})
            cfm = cfg.get("contact_field_mapping")
            conn.execute(
                """INSERT INTO crm_workspace_config
                   (workspace_id, platform, api_key, location_id, pipeline_id,
                    stage_mapping, contact_field_mapping, overwrite_existing,
                    enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(workspace_id, platform) DO UPDATE SET
                     api_key = excluded.api_key,
                     location_id = excluded.location_id,
                     pipeline_id = excluded.pipeline_id,
                     stage_mapping = excluded.stage_mapping,
                     contact_field_mapping = excluded.contact_field_mapping,
                     overwrite_existing = excluded.overwrite_existing,
                     enabled = excluded.enabled,
                     updated_at = datetime('now')""",
                (
                    ws_id,
                    platform,
                    cfg.get("api_key", ""),
                    cfg.get("location_id"),
                    cfg.get("pipeline_id"),
                    stage_mapping,
                    json.dumps(cfm) if cfm else None,
                    cfg.get("overwrite_existing", 0),
                    cfg.get("enabled", 1),
                ),
            )

    # Remove rows for this org's workspaces that are no longer in the config
    existing = conn.execute(
        """SELECT DISTINCT workspace_id, platform
           FROM crm_workspace_config
           WHERE workspace_id IN ({})""".format(
            ",".join("?" for _ in ws_ids)
        ),
        ws_ids,
    ).fetchall()
    for (ws_id, platform) in existing:
        if (ws_id, platform) not in seen:
            conn.execute(
                "DELETE FROM crm_workspace_config WHERE workspace_id = ? AND platform = ?",
                (ws_id, platform),
            )


def sync_org_config_from_cloud(
    conn: sqlite3.Connection,
    *,
    api_base: str,
    token: str,
    org_id: str,
    load_config_fn,
    save_config_fn,
    quiet: bool = False,
) -> Optional[dict[str, Any]]:
    """Fetch portal config and apply routing + CRM config + agent secrets to local DB."""
    from agent_secrets_cloud import (
        apply_secrets_to_environ,
        mirror_agent_secrets_to_data_env,
        write_agent_secrets_env,
    )

    bundle = fetch_portal_config(api_base, token)
    apply_routing_bundle_to_sqlite(conn, bundle, org_id=org_id)

    crm_configs = bundle.get("crmConfigs") or {}
    _apply_crm_config_to_sqlite(conn, crm_configs, org_id=org_id)

    agent_secrets = bundle.get("agentSecrets")
    if agent_secrets:
        try:
            from agent_secrets_cloud import agent_secrets_path
            path = agent_secrets_path()
            secrets_data = agent_secrets.get("secrets", {})
            version = agent_secrets.get("version", 1)
            keys_written = write_agent_secrets_env(path, secrets_data, version=version)
            if keys_written:
                mirror_agent_secrets_to_data_env(secrets_data)
                apply_secrets_to_environ(secrets_data)
        except Exception:
            if not quiet:
                print("Warning: agent secrets processing failed (non-fatal)", file=__import__("sys").stderr)

    conn.commit()

    cfg = load_config_fn()
    cfg["org_config_version"] = bundle.get("version")
    save_config_fn(cfg)
    if not quiet:
        ws_count = len(bundle.get("workspaces") or [])
        print(
            f"Portal config synced (v{bundle.get('version')}, "
            f"{ws_count} workspaces)."
        )
    return bundle


def push_crm_sync_status(api_base: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST CRM sync status to cloud."""
    return _request_json("POST", f"{api_base}/api/crm-sync-status", token, body=payload)
