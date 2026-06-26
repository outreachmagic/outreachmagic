"""
Sync org BYOK API keys from the portal to local agent_secrets.env.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from om_paths import get_agent_secrets_path, get_data_root

DEFAULT_API_BASE = "https://app.outreachmagic.io"
CONFIG_API_BASE_KEY = "api_base_url"
CONFIG_AGENT_SECRETS_VERSION_KEY = "agent_secrets_version"
CONFIG_ORGANIZATION_ID_KEY = "organization_id"

CATALOG_ENV_KEYS = (
    "SERPER_API_KEY",
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
)

def _parse_pool_env_key(key: str) -> tuple[str, int] | None:
    if not key or not key[0].isupper():
        return None
    if "__" in key:
        base, suffix = key.rsplit("__", 1)
        if suffix.isdigit():
            return base, int(suffix)
    return key, 0


def get_api_base(load_config_fn: Callable[[], dict]) -> str:
    cfg = load_config_fn()
    return (cfg.get(CONFIG_API_BASE_KEY) or DEFAULT_API_BASE).rstrip("/")


def _request_json(method: str, url: str, token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
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
        raise RuntimeError(f"Agent secrets API {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Agent secrets API unreachable: {exc.reason}") from exc


def fetch_secrets_bundle(api_base: str, token: str) -> dict[str, Any]:
    return _request_json("GET", f"{api_base}/api/agent-secrets", token)


def agent_secrets_path() -> Path:
    """Synced API keys file under skill config (next to outreachmagic_config.json)."""
    return get_agent_secrets_path()


def env_var_for_slot(env_key: str, index: int) -> str:
    if index <= 0:
        return env_key
    return f"{env_key}__{index}"


def write_agent_secrets_env(
    path: Path,
    secrets: dict[str, list[str]],
    *,
    version: int,
) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[tuple[str, str]] = []
    for env_key in sorted(secrets.keys()):
        values = [v.strip() for v in (secrets.get(env_key) or []) if v and str(v).strip()]
        for idx, value in enumerate(values):
            lines.append((env_var_for_slot(env_key, idx), value))

    header = [
        "# Outreach Magic — synced from dashboard (do not commit)",
        "# Re-sync: python3 scripts/pipeline.py sync-secrets",
        f"# version: {version}",
        "",
    ]
    body = [f"{k}={v}" for k, v in lines]
    text = "\n".join(header + body)
    if body:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return [k for k, _ in lines]


def _is_catalog_env_var(key: str) -> bool:
    parsed = _parse_pool_env_key(key)
    if not parsed:
        return False
    base, _ = parsed
    return base in CATALOG_ENV_KEYS


def mirror_agent_secrets_to_data_env(secrets: dict[str, list[str]]) -> Path:
    """Mirror synced companion keys into {data_root}/.env for Hermes shell/agent visibility."""
    path = get_data_root() / ".env"
    preserved: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                preserved.append(line)
                continue
            key = stripped.partition("=")[0].strip()
            if _is_catalog_env_var(key):
                continue
            preserved.append(line)

    lines: list[str] = []
    if preserved:
        lines.extend(preserved)
        if preserved[-1].strip():
            lines.append("")
    lines.extend(
        [
            "# Companion API keys — synced from dashboard (do not commit)",
            "# Re-sync: python3 scripts/pipeline.py sync-secrets",
        ]
    )
    for env_key in sorted(secrets.keys()):
        values = [v.strip() for v in (secrets.get(env_key) or []) if v and str(v).strip()]
        for idx, value in enumerate(values):
            lines.append(f"{env_var_for_slot(env_key, idx)}={value}")
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def apply_secrets_to_environ(secrets: dict[str, list[str]], *, override: bool = True) -> None:
    for env_key in sorted(secrets.keys()):
        values = secrets.get(env_key) or []
        for idx, value in enumerate(values):
            var = env_var_for_slot(env_key, idx)
            val = (value or "").strip()
            if not val:
                continue
            if override or not os.environ.get(var, "").strip():
                os.environ[var] = val


def parse_agent_secrets_file(path: Path) -> dict[str, list[str]]:
    pools: dict[str, list[tuple[int, str]]] = {}
    if not path.is_file():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        parsed_key = _parse_pool_env_key(key)
        if not parsed_key:
            continue
        base, idx = parsed_key
        pools.setdefault(base, []).append((idx, value))
    out: dict[str, list[str]] = {}
    for base, items in pools.items():
        out[base] = [v for _, v in sorted(items, key=lambda x: x[0])]
    return out


def load_local_agent_secrets_to_environ(*, override: bool = True) -> Path:
    """Load synced keys from disk into os.environ (primary + backup __N slots)."""
    path = agent_secrets_path()
    pools = parse_agent_secrets_file(path)
    apply_secrets_to_environ(pools, override=override)
    return path


def pool_sizes_from_pools(pools: dict[str, list[str]]) -> dict[str, int]:
    sizes: dict[str, int] = {k: 0 for k in CATALOG_ENV_KEYS}
    for key, values in pools.items():
        count = len([v for v in values if v.strip()])
        if key in sizes:
            sizes[key] = count
        else:
            sizes[key] = count
    return sizes


def configured_from_pools(pools: dict[str, list[str]]) -> dict[str, bool]:
    return {k: bool(pools.get(k)) for k in CATALOG_ENV_KEYS}


def sync_agent_secrets_from_cloud(
    *,
    api_base: str,
    token: str,
    load_config_fn: Callable[[], dict],
    save_config_fn: Callable[[dict], None],
    quiet: bool = False,
) -> dict[str, Any]:
    bundle = fetch_secrets_bundle(api_base, token)
    org_id = str(bundle.get("organizationId") or "").strip()
    version = int(bundle.get("version") or 0)
    secrets = bundle.get("secrets") or {}
    if not isinstance(secrets, dict):
        secrets = {}

    path = agent_secrets_path()
    keys_written = write_agent_secrets_env(path, secrets, version=version)
    mirror_agent_secrets_to_data_env(secrets)
    apply_secrets_to_environ(secrets, override=True)

    cfg = load_config_fn()
    cfg[CONFIG_AGENT_SECRETS_VERSION_KEY] = version
    if org_id:
        cfg[CONFIG_ORGANIZATION_ID_KEY] = org_id
    save_config_fn(cfg)

    if not quiet:
        print(
            f"API keys synced (v{version}, {len(keys_written)} env vars) → {path}",
            flush=True,
        )
    return {
        "ok": True,
        "version": version,
        "organizationId": org_id or None,
        "path": str(path),
        "keys_written": len(keys_written),
        "keys": keys_written,
    }


def check_agent_secrets_local(
    load_config_fn: Callable[[], dict],
) -> dict[str, Any]:
    cfg = load_config_fn()
    path = load_local_agent_secrets_to_environ(override=True)
    pools = parse_agent_secrets_file(path)
    return {
        "ok": True,
        "version": cfg.get(CONFIG_AGENT_SECRETS_VERSION_KEY),
        "path": str(path),
        "configured": configured_from_pools(pools),
        "pool_sizes": pool_sizes_from_pools(pools),
    }


def cloud_secrets_enabled(load_config_fn: Callable[[], dict], token: Optional[str]) -> bool:
    return bool(token and get_api_base(load_config_fn))


def maybe_sync_agent_secrets_from_cloud(
    *,
    load_config_fn: Callable[[], dict],
    save_config_fn: Callable[[dict], None],
    get_agent_key_fn: Callable[[], Optional[str]],
    quiet: bool = True,
) -> bool:
    token = get_agent_key_fn()
    if not cloud_secrets_enabled(load_config_fn, token):
        return False
    try:
        sync_agent_secrets_from_cloud(
            api_base=get_api_base(load_config_fn),
            token=token or "",
            load_config_fn=load_config_fn,
            save_config_fn=save_config_fn,
            quiet=quiet,
        )
        return True
    except Exception as exc:
        if not quiet:
            print(f"API key sync warning: {exc}", flush=True)
        return False
