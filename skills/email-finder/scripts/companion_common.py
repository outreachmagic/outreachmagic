"""Shared helpers for outreachmagic companion skills (lead-enrich, email-finder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

# Avoid OS ARG_MAX when passing large JSON to pipeline.py subprocesses.
JSON_ARG_THRESHOLD = 100_000
PIPELINE_CHUNK_SIZE = 200
PIPELINE_FAST_CHUNK_SIZE = 500
LOOKUP_CHUNK_SIZE = 1000

OUTREACHMAGIC_NAME = "outreachmagic"

SKILL_SEARCH_PATHS = [
    Path.home() / ".hermes" / "skills",
    Path.home() / ".cursor" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".openclaw" / "skills",  # OpenClaw managed/local skills dir
]

_AGENT_ENV_LOADED = False


def agent_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


_API_KEY_VARS = frozenset({
    "SERPER_API_KEY",
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
    "SCRUBBY_API_KEY",
    "OUTREACHMAGIC_AGENT_KEY",
})

_POOL_API_KEY_BASES = (
    "SERPER_API_KEY",
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
    "SCRUBBY_API_KEY",
)


def _is_pooled_api_key_var(key: str) -> bool:
    if key in _API_KEY_VARS:
        return True
    for base in _POOL_API_KEY_BASES:
        prefix = f"{base}__"
        if key.startswith(prefix) and key[len(prefix) :].isdigit():
            return True
    return False


def _env_value_empty(key: str) -> bool:
    val = os.environ.get(key, "")
    if not val or not str(val).strip():
        return True
    if str(val).strip() in ("***", "changeme", "your_key_here"):
        return True
    return False


_PORTAL_ONLY_KEYS = frozenset({
    "SERPER_API_KEY",
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
    "SCRUBBY_API_KEY",
})



def allow_local_api_keys() -> bool:
    """CI/automation escape hatch — not for interactive agent installs."""
    return os.environ.get("OM_ALLOW_LOCAL_API_KEYS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _portal_key_env_vars() -> list[str]:
    """All env var names for portal-managed keys (primary + __N backup slots)."""
    found: list[str] = []
    for base in _POOL_API_KEY_BASES:
        if base in os.environ:
            found.append(base)
        idx = 1
        while True:
            var = f"{base}__{idx}"
            if var in os.environ:
                found.append(var)
                idx += 1
            else:
                break
    return found


def _authorized_portal_keys_from_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_dotenv_line(line)
        if parsed and _is_pooled_api_key_var(parsed[0]):
            keys.add(parsed[0])
    return keys


def _enforce_portal_only_keys(skill_dir: Optional[Path]) -> None:
    """Drop BYOK keys not present in agent_secrets.env (strict mode)."""
    if allow_local_api_keys():
        return
    om = find_outreachmagic({}, skill_dir=skill_dir)
    secrets_path = (om / "config" / "agent_secrets.env") if om else None
    authorized = (
        _authorized_portal_keys_from_file(secrets_path)
        if secrets_path
        else set()
    )
    for var in _portal_key_env_vars():
        if var not in authorized:
            os.environ.pop(var, None)


def load_dotenv_file(
    path: Path,
    *,
    force_api_keys: bool = False,
    override_existing: bool = False,
    override_all: bool = False,
    skip_api_keys: Optional[frozenset[str]] = None,
) -> None:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return
    for line in text.splitlines():
        parsed = parse_dotenv_line(line)
        if not parsed:
            continue
        key, value = parsed
        if not value or value.strip() in ("***", "changeme", "your_key_here"):
            continue
        if override_all:
            os.environ[key] = value
            continue
        if force_api_keys and _is_pooled_api_key_var(key):
            if skip_api_keys and key in skip_api_keys:
                continue
            if override_existing or _env_value_empty(key):
                os.environ[key] = value
            continue
        if override_existing and _is_pooled_api_key_var(key):
            os.environ[key] = value
            continue
        if key not in os.environ:
            os.environ[key] = value


def active_profile() -> Optional[str]:
    """Hermes profile slug from HERMES_PROFILE env (per-profile .env overrides)."""
    return (os.environ.get("HERMES_PROFILE") or "").strip() or None


def _monorepo_dotenv(skill_dir: Optional[Path]) -> Optional[Path]:
    """Dev checkout: skills/<name>/ → repo root .env (install.sh sibling)."""
    if not skill_dir:
        return None
    root = skill_dir.resolve().parent.parent
    env_file = root / ".env"
    if env_file.is_file() and (root / "install.sh").is_file():
        return env_file
    return None


def ensure_agent_env_loaded(skill_dir: Optional[Path] = None, *, reload: bool = False) -> None:
    global _AGENT_ENV_LOADED
    if _AGENT_ENV_LOADED and not reload:
        return
    # Portal-synced keys are the sole runtime source for BYOK providers (strict mode).
    _load_synced_agent_secrets(skill_dir)
    local_keys = allow_local_api_keys()
    skip_portal = frozenset() if local_keys else _PORTAL_ONLY_KEYS
    if local_keys:
        home = agent_home()
        for name in (".env", "default.env"):
            load_dotenv_file(
                home / name,
                force_api_keys=True,
                skip_api_keys=skip_portal,
            )
        profile = active_profile()
        if profile:
            load_dotenv_file(
                home / "profiles" / profile / ".env",
                force_api_keys=True,
                skip_api_keys=skip_portal,
            )
        repo_env = _monorepo_dotenv(skill_dir)
        if repo_env:
            load_dotenv_file(
                repo_env,
                force_api_keys=True,
                skip_api_keys=skip_portal,
            )
        if skill_dir:
            load_dotenv_file(skill_dir / "default.env", force_api_keys=True)
    _enforce_portal_only_keys(skill_dir)
    _AGENT_ENV_LOADED = True


def _dotenv_keys(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        parsed = parse_dotenv_line(line)
        if parsed:
            keys.add(parsed[0])
    return keys


def companion_api_key_source(key: str, skill_dir: Optional[Path] = None) -> Optional[str]:
    """Where a companion provider key was loaded from (for config diagnostics)."""
    if not os.environ.get(key, "").strip():
        return None
    om = find_outreachmagic({}, skill_dir=skill_dir)
    if om:
        secrets = om / "config" / "agent_secrets.env"
        secret_keys = _dotenv_keys(secrets)
        if key in secret_keys or any(k.startswith(f"{key}__") for k in secret_keys):
            return "agent_secrets"
    if skill_dir:
        cfg_path = skill_dir / "config.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cfg = {}
            cfg_key = {
                "SERPER_API_KEY": "serper_api_key",
                "TRYKITT_API_KEY": "trykitt_api_key",
                "ICYPEAS_API_KEY": "icypeas_api_key",
                "MILLIONVERIFIER_API_KEY": "millionverifier_api_key",
                "SCRUBBY_API_KEY": "scrubby_api_key",
            }.get(key)
            if cfg_key and str(cfg.get(cfg_key) or "").strip():
                return "config_json"
        if key in _dotenv_keys(skill_dir / "default.env"):
            return "default_env"
    home_env = agent_home() / ".env"
    if key in _dotenv_keys(home_env):
        return "hermes_env"
    if key in os.environ:
        return "shell"
    return "unknown"


def warn_non_portal_key_sources(
    skill_dir: Optional[Path] = None,
    *,
    keys: Optional[tuple[str, ...]] = None,
    file: Any = None,
) -> list[str]:
    """Return warning lines for keys not sourced from agent_secrets.env."""
    check_keys = keys or tuple(_PORTAL_ONLY_KEYS)
    warnings: list[str] = []
    for env_key in check_keys:
        if not os.environ.get(env_key, "").strip():
            continue
        source = companion_api_key_source(env_key, skill_dir=skill_dir)
        if source != "agent_secrets":
            warnings.append(
                f"⚠  {env_key} loaded from {source or 'unknown'} — portal sync keys not found. "
                "Configure in Outreach Magic portal → Settings → API Keys, then run: "
                "pipeline.py sync-secrets"
            )
    for line in warnings:
        print(line, file=file or sys.stderr)
    return warnings


def maybe_sync_secrets_from_portal(
    skill_dir: Optional[Path] = None,
    *,
    quiet: bool = True,
) -> bool:
    """Refresh agent_secrets.env from portal (e.g. after auth failure)."""
    om = find_outreachmagic({}, skill_dir=skill_dir)
    if not om:
        return False
    pipeline = get_pipeline_path(om)
    if not pipeline.is_file():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(pipeline), "sync-secrets"],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if proc.returncode != 0 and not quiet:
            err = (proc.stderr or proc.stdout or "").strip()
            if err:
                print(err, file=sys.stderr)
        ensure_agent_env_loaded(skill_dir, reload=True)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def subprocess_env(skill_dir: Optional[Path] = None) -> dict[str, str]:
    ensure_agent_env_loaded(skill_dir)
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


def skill_dir_from_script(script_file: str) -> Path:
    return Path(script_file).resolve().parent.parent


def find_outreachmagic(
    config: dict[str, Any],
    skill_dir: Optional[Path] = None,
) -> Optional[Path]:
    configured = (config.get("outreachmagic_home") or "").strip()
    if configured:
        home = Path(configured).expanduser()
        if (home / "scripts" / "pipeline.py").exists():
            return home
    if skill_dir:
        sibling = skill_dir.parent / OUTREACHMAGIC_NAME
        if (sibling / "scripts" / "pipeline.py").exists():
            return sibling
    for skills_dir in SKILL_SEARCH_PATHS:
        candidate = skills_dir / OUTREACHMAGIC_NAME
        if (candidate / "scripts" / "pipeline.py").exists():
            return candidate
    return None


def _load_synced_agent_secrets(skill_dir: Optional[Path] = None) -> None:
    om_home = find_outreachmagic({}, skill_dir=skill_dir)
    if not om_home:
        return
    scripts = om_home / "scripts"
    if not (scripts / "agent_secrets_cloud.py").is_file():
        return
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import agent_secrets_cloud
    except ImportError:
        return
    secrets_path = om_home / "config" / "agent_secrets.env"
    if secrets_path.is_file():
        pools = agent_secrets_cloud.parse_agent_secrets_file(secrets_path)
        agent_secrets_cloud.apply_secrets_to_environ(pools, override=True)
    else:
        agent_secrets_cloud.load_local_agent_secrets_to_environ(override=True)


def get_pipeline_path(om_dir: Path) -> Path:
    return om_dir / "scripts" / "pipeline.py"


def require_api_key_pool():
    """Import api_key_pool from outreachmagic. Raises if unavailable."""
    skill_dir = Path(__file__).resolve().parent.parent
    ensure_agent_env_loaded(skill_dir)
    om = find_outreachmagic({}, skill_dir=skill_dir)
    if not om:
        raise RuntimeError(
            "outreachmagic skill required for API key pools. "
            "Install via: ask Outreach Magic to log in"
        )
    scripts = om / "scripts"
    if not (scripts / "api_key_pool.py").is_file():
        raise RuntimeError(f"api_key_pool.py not found under {scripts}")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from api_key_pool import api_key_pool, call_with_key_pool, call_with_key_pool_results

    return api_key_pool, call_with_key_pool, call_with_key_pool_results


def outreachmagic_agent_key_status(om_dir: Optional[Path]) -> tuple[bool, str]:
    env_key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if env_key:
        return True, "env"
    if not om_dir:
        return False, "missing"
    cfg_path = om_dir / "config" / "outreachmagic_config.json"
    try:
        payload = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False, "missing"
    if isinstance(payload, dict) and str(payload.get("agent_key", "")).strip():
        return True, "outreachmagic_config"
    return False, "missing"


def history_lookup(
    om_dir: Path,
    extra_args: list[str],
    *,
    workspace: Optional[str] = None,
    timeout: int = 10,
    skill_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    pipeline = str(get_pipeline_path(om_dir))
    base_args = ["history", "--json", "--limit", "0"]
    if workspace:
        base_args.extend(["--workspace", workspace])
    try:
        proc = subprocess.run(
            [sys.executable, pipeline, *base_args, *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(skill_dir),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, dict) and data.get("error"):
            return None
        if isinstance(data, dict) and data.get("lead"):
            return data["lead"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    return None


CHUNK_TIMEOUT_PER_ITEM_S = 0.8


def _chunk_timeout(
    item_count: int,
    *,
    per_item: float = CHUNK_TIMEOUT_PER_ITEM_S,
    min_s: int = 30,
    max_s: int = 300,
) -> int:
    return max(min_s, min(max_s, int(item_count * per_item)))


def _resolve_timeout(
    item_count: int,
    *,
    per_item: float = CHUNK_TIMEOUT_PER_ITEM_S,
    max_s: int = 300,
    override: Optional[int] = None,
) -> int:
    if override is not None:
        return override
    return _chunk_timeout(item_count, per_item=per_item, max_s=max_s)


def print_import_failure_recovery(
    exc: BaseException,
    *,
    skill: str,
    recovery_lines: list[str],
    data_paths: Optional[list[str]] = None,
) -> None:
    """Print actionable stderr when OM import subprocess fails (data may still be on disk)."""
    print(f"\n⚠️  Import to Outreach Magic failed ({skill}): {exc}", file=sys.stderr)
    for path in data_paths or []:
        if path:
            print(f"   Data safe at: {path}", file=sys.stderr)
    for line in recovery_lines:
        print(f"   {line}", file=sys.stderr)


def profiles_have_known_lead_ids(profiles: list[dict]) -> bool:
    if not profiles:
        return False
    for profile in profiles:
        lid = profile.get("id") if profile.get("id") is not None else profile.get("lead_id")
        if lid is None or not str(lid).strip().isdigit():
            return False
    return True


def _append_json_or_file(
    cmd: list[str],
    payload: Any,
    *,
    json_flag: str = "--json",
    file_flag: str = "--file",
) -> tuple[list[str], Optional[str]]:
    json_str = json.dumps(payload)
    if len(json_str) <= JSON_ARG_THRESHOLD:
        return [*cmd, json_flag, json_str], None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    try:
        tmp.write(json_str)
        tmp.close()
        return [*cmd, file_flag, tmp.name], tmp.name
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def get_working_export_dir(om_dir: Optional[Path] = None) -> Path:
    """Resolve outreachmagic/exports under project_root or cwd."""
    root = Path.cwd()
    if om_dir:
        cfg_path = Path(om_dir) / "config" / "outreachmagic_config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                project_root = (cfg.get("project_root") or "").strip()
                if project_root:
                    root = Path(project_root).expanduser()
            except (OSError, json.JSONDecodeError):
                pass
    return (root / "outreachmagic" / "exports").resolve()


def fetch_sync_status(om_dir: Path, *, skill_dir: Optional[Path] = None) -> dict[str, Any]:
    """Read pipeline sync --status --json without pushing."""
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "sync",
        "--status",
        "--json",
    ]
    return _run_subprocess_json(cmd, temp_path=None, timeout=120, skill_dir=skill_dir)


def run_sync(om_dir: Path, *, skill_dir: Optional[Path] = None) -> dict[str, Any]:
    """Push pending local changes to the OM relay and return the result."""
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "sync",
    ]
    return _run_subprocess_json(cmd, temp_path=None, timeout=300, skill_dir=skill_dir)


def print_om_setup_box(file=sys.stderr) -> None:
    """Print a prominent box urging the user to install Outreach Magic."""
    print(file=file)
    print("╔" + "═" * 62 + "╗", file=file)
    print(f"║{'OUTREACHMAGIC — NOT CONNECTED':^62}║", file=file)
    print("╠" + "═" * 62 + "╣", file=file)
    print("║  Without Outreach Magic enabled, results are NOT saved to a  ║", file=file)
    print("║  local database. Paid API credits will be wasted re-research- ║", file=file)
    print("║  ing leads that already have emails or LinkedIn profiles.    ║", file=file)
    print("║  This tool recommends installing Outreach Magic to:          ║", file=file)
    print("║  • Dedup before paid API calls (save credits)                ║", file=file)
    print("║  • Persist results in a local SQLite database                ║", file=file)
    print("║  • Sync to the cloud so no data is lost                     ║", file=file)
    print("║                                                              ║", file=file)
    print("║  Tell your AI agent to run this command:                     ║", file=file)
    print("║                                                              ║", file=file)
    print("║    Fetch this file and follow its instructions to install    ║", file=file)
    print("║    the Outreach Magic skill suite on this machine:           ║", file=file)
    print("║                                                              ║", file=file)
    print("║    https://raw.githubusercontent.com/outreachmagic/          ║", file=file)
    print("║    outreachmagic/main/AGENTS-INSTALL.md                      ║", file=file)
    print("║                                                              ║", file=file)
    print("║  After install, re-run this command with --workspace         ║", file=file)
    print("║  and --yes.                                                  ║", file=file)
    print("║                                                              ║", file=file)
    print("║  To skip this check and run without a database (not          ║", file=file)
    print("║  recommended — credits may be wasted):                       ║", file=file)
    print("║    re-run with --skip-om                                     ║", file=file)
    print("╚" + "═" * 62 + "╝", file=file)
    print(file=file)


def _run_subprocess_json(
    cmd: list[str],
    *,
    temp_path: Optional[str],
    timeout: int,
    skill_dir: Optional[Path],
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(skill_dir),
        )
    except subprocess.TimeoutExpired as e:
        cmd_name = cmd[2] if len(cmd) > 2 else "pipeline"
        raise RuntimeError(
            f"{cmd_name} timed out after {timeout}s"
            + (f" ({e.cmd[-2]} payload)" if e.cmd and len(e.cmd) > 2 else "")
        ) from e
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _merge_pipeline_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    if not summaries:
        return {}
    if len(summaries) == 1:
        return summaries[0]
    merged: dict[str, Any] = {}
    sum_keys = (
        "processed",
        "created",
        "matched",
        "enriched",
        "updated",
        "skipped",
        "recorded",
        "personalized",
        "tagged",
        "weak_identity_count",
        "import_key_only_count",
        "skipped_no_identity",
    )
    for key in sum_keys:
        if any(key in s for s in summaries):
            merged[key] = sum(int(s.get(key) or 0) for s in summaries)
    for list_key in ("results", "errors", "identity_conflicts", "skipped_features"):
        merged[list_key] = []
        for s in summaries:
            part = s.get(list_key)
            if isinstance(part, list):
                merged[list_key].extend(part)
    for s in reversed(summaries):
        if s.get("status"):
            merged["status"] = s["status"]
            break
    for s in reversed(summaries):
        if s.get("mode"):
            merged["mode"] = s["mode"]
            break
    merged["chunks"] = len(summaries)
    return merged


def _run_apply_email_find_once(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str,
    overwrite: bool = False,
    source: str = "",
    source_detail: str = "companion",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "apply-email-find-results",
        "--workspace",
        workspace,
        "--source-detail",
        source_detail,
    ]
    if source:
        cmd.extend(["--source", source])
    if overwrite:
        cmd.append("--overwrite")
    cmd, temp_path = _append_json_or_file(cmd, profiles)
    return _run_subprocess_json(cmd, temp_path=temp_path, timeout=timeout, skill_dir=skill_dir)


def run_apply_email_find_results(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str,
    overwrite: bool = False,
    source: str = "",
    source_detail: str = "companion",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not profiles:
        return {}
    if len(profiles) <= PIPELINE_FAST_CHUNK_SIZE:
        chunk_timeout = _resolve_timeout(
            len(profiles), per_item=0.1, max_s=120, override=timeout,
        )
        return _run_apply_email_find_once(
            om_dir,
            profiles,
            workspace=workspace,
            overwrite=overwrite,
            source=source,
            source_detail=source_detail,
            timeout=chunk_timeout,
            skill_dir=skill_dir,
        )
    summaries: list[dict[str, Any]] = []
    total_chunks = (len(profiles) + PIPELINE_FAST_CHUNK_SIZE - 1) // PIPELINE_FAST_CHUNK_SIZE
    for i in range(0, len(profiles), PIPELINE_FAST_CHUNK_SIZE):
        chunk = profiles[i : i + PIPELINE_FAST_CHUNK_SIZE]
        chunk_num = i // PIPELINE_FAST_CHUNK_SIZE + 1
        chunk_timeout = _resolve_timeout(
            len(chunk), per_item=0.1, max_s=120, override=timeout,
        )
        print(
            f"  apply-email-find-results chunk {chunk_num}/{total_chunks} ({len(chunk)} leads)...",
            flush=True,
        )
        t0 = time.monotonic()
        summaries.append(
            _run_apply_email_find_once(
                om_dir,
                chunk,
                workspace=workspace,
                overwrite=overwrite,
                source=source,
                source_detail=source_detail,
                timeout=chunk_timeout,
                skill_dir=skill_dir,
            )
        )
        last = summaries[-1]
        elapsed = time.monotonic() - t0
        print(
            f"    matched={last.get('matched', 0)} enriched={last.get('enriched', 0)} "
            f"recorded={last.get('recorded', 0)} ({elapsed:.1f}s)",
            flush=True,
        )
    return _merge_pipeline_summaries(summaries)


def save_email_find_profiles(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str,
    overwrite: bool = False,
    source: str = "",
    source_detail: str = "email-finder/batch",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Save email-finder batch output to OM (fast apply when every row has lead id).

    After saving (via apply-email-find-results or import-profiles), runs an
    explicit sync to push pending changes to the OM relay.  The caller can
    verify with ``fetch_sync_status()``.
    """
    if not workspace:
        raise RuntimeError("--workspace is required to save batch results to Outreach Magic")
    if not profiles:
        return {}
    if profiles_have_known_lead_ids(profiles):
        result = run_apply_email_find_results(
            om_dir,
            profiles,
            workspace=workspace,
            overwrite=overwrite,
            source=source,
            source_detail=source_detail,
            timeout=timeout,
            skill_dir=skill_dir,
        )
    else:
        result = run_import_profiles(
            om_dir,
            profiles,
            workspace=workspace,
            overwrite=overwrite,
            source=source,
            source_detail=source_detail,
            timeout=timeout,
            skill_dir=skill_dir,
        )
    # Push pending changes to relay (import-profiles with --no-sync avoids
    # double-syncing; apply-email-find-results never auto-syncs).
    try:
        sync_result = run_sync(om_dir, skill_dir=skill_dir)
        result["sync"] = sync_result
        result["sync_hint"] = (
            "Changes pushed to relay via pipeline.py sync."
            if sync_result.get("status") == "ok"
            else f"Sync result: {sync_result.get('status', 'unknown')} — run: pipeline.py sync"
        )
    except RuntimeError as e:
        result["sync"] = {"error": str(e)}
        result["sync_hint"] = "Sync failed — run: pipeline.py sync"
    return result


def _run_import_profiles_once(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str = "",
    overwrite: bool = False,
    source: str = "",
    source_detail: str = "companion",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "import-profiles",
        "--no-sync",
    ]
    if source:
        cmd.extend(["--source", source])
    cmd.extend(["--source-detail", source_detail])
    if workspace:
        cmd.extend(["--workspace", workspace])
    if overwrite:
        cmd.append("--overwrite")
    cmd, temp_path = _append_json_or_file(cmd, profiles)
    return _run_subprocess_json(cmd, temp_path=temp_path, timeout=timeout, skill_dir=skill_dir)


def run_import_profiles(
    om_dir: Path,
    profiles: list[dict],
    *,
    workspace: str = "",
    overwrite: bool = False,
    source: str = "",
    source_detail: str = "companion",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not profiles:
        return {}
    if len(profiles) <= PIPELINE_CHUNK_SIZE:
        chunk_timeout = _resolve_timeout(len(profiles), override=timeout)
        return _run_import_profiles_once(
            om_dir,
            profiles,
            workspace=workspace,
            overwrite=overwrite,
            source=source,
            source_detail=source_detail,
            timeout=chunk_timeout,
            skill_dir=skill_dir,
        )
    summaries: list[dict[str, Any]] = []
    total_chunks = (len(profiles) + PIPELINE_CHUNK_SIZE - 1) // PIPELINE_CHUNK_SIZE
    for i in range(0, len(profiles), PIPELINE_CHUNK_SIZE):
        chunk = profiles[i : i + PIPELINE_CHUNK_SIZE]
        chunk_num = i // PIPELINE_CHUNK_SIZE + 1
        chunk_timeout = _resolve_timeout(len(chunk), override=timeout)
        print(
            f"  import-profiles chunk {chunk_num}/{total_chunks} ({len(chunk)} leads)...",
            flush=True,
        )
        t0 = time.monotonic()
        summaries.append(
            _run_import_profiles_once(
                om_dir,
                chunk,
                workspace=workspace,
                overwrite=overwrite,
                source=source,
                source_detail=source_detail,
                timeout=chunk_timeout,
                skill_dir=skill_dir,
            )
        )
        last = summaries[-1]
        elapsed = time.monotonic() - t0
        print(
            f"    matched={last.get('matched', 0)} enriched={last.get('enriched', 0)} "
            f"created={last.get('created', 0)} ({elapsed:.1f}s)",
            flush=True,
        )
    return _merge_pipeline_summaries(summaries)


def _run_batch_lead_lookup_once(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    workspace: str = "",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "batch-lead-lookup",
    ]
    if workspace:
        cmd.extend(["--workspace", workspace])
    cmd, temp_path = _append_json_or_file(cmd, items)
    return _run_subprocess_json(cmd, temp_path=temp_path, timeout=timeout, skill_dir=skill_dir)


def run_batch_lead_lookup(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    workspace: str = "",
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """pipeline.py batch-lead-lookup (file payload + chunking for large batches)."""
    if not items:
        return {"status": "ok", "results": []}
    if len(items) <= LOOKUP_CHUNK_SIZE:
        lookup_timeout = _resolve_timeout(len(items), per_item=0.15, override=timeout)
        return _run_batch_lead_lookup_once(
            om_dir,
            items,
            workspace=workspace,
            timeout=lookup_timeout,
            skill_dir=skill_dir,
        )
    merged_results: list[dict[str, Any]] = []
    total_chunks = (len(items) + LOOKUP_CHUNK_SIZE - 1) // LOOKUP_CHUNK_SIZE
    for i in range(0, len(items), LOOKUP_CHUNK_SIZE):
        chunk = items[i : i + LOOKUP_CHUNK_SIZE]
        chunk_num = i // LOOKUP_CHUNK_SIZE + 1
        print(
            f"  batch-lead-lookup chunk {chunk_num}/{total_chunks} ({len(chunk)} keys)...",
            flush=True,
        )
        part = _run_batch_lead_lookup_once(
            om_dir,
            chunk,
            workspace=workspace,
            timeout=_resolve_timeout(len(chunk), per_item=0.15, override=timeout),
            skill_dir=skill_dir,
        )
        merged_results.extend(part.get("results") or [])
    return {"status": "ok", "results": merged_results, "chunks": total_chunks}


def run_verification_candidates(
    om_dir: Path,
    workspace: str,
    *,
    max_age_days: int = 30,
    skip_mv_days: int = 7,
    limit: int = 5000,
    never_contacted: bool = False,
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "verification-candidates",
        "--workspace",
        workspace,
        "--max-age",
        str(max_age_days),
        "--skip-mv-days",
        str(skip_mv_days),
        "--limit",
        str(limit),
    ]
    if never_contacted:
        cmd.append("--never-contacted")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_scrubby_deep_candidates(
    om_dir: Path,
    workspace: str,
    *,
    max_age_days: int = 30,
    skip_scrubby_days: int = 7,
    limit: int = 5000,
    never_contacted: bool = False,
    filter_catch_all: bool = False,
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Collect workspace leads due for Scrubby Deep verification."""
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "verification-candidates",
        "--workspace",
        workspace,
        "--source",
        "scrubby_deep",
        "--max-age",
        str(max_age_days),
        "--skip-mv-days",
        str(skip_scrubby_days),
        "--limit",
        str(limit),
    ]
    if never_contacted:
        cmd.append("--never-contacted")
    if filter_catch_all:
        cmd.append("--filter")
        cmd.append("catch_all")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(skill_dir),
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _run_verify_email_batch_once(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(get_pipeline_path(om_dir)),
        "verify-email",
        "--batch",
    ]
    cmd, temp_path = _append_json_or_file(cmd, items)
    return _run_subprocess_json(cmd, temp_path=temp_path, timeout=timeout, skill_dir=skill_dir)


def run_verify_email_batch(
    om_dir: Path,
    items: list[dict[str, Any]],
    *,
    timeout: Optional[int] = None,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not items:
        return {"status": "batch_recorded", "recorded": 0, "errors": []}
    if len(items) <= PIPELINE_CHUNK_SIZE:
        chunk_timeout = _resolve_timeout(len(items), override=timeout)
        return _run_verify_email_batch_once(
            om_dir, items, timeout=chunk_timeout, skill_dir=skill_dir,
        )
    summaries: list[dict[str, Any]] = []
    total_chunks = (len(items) + PIPELINE_CHUNK_SIZE - 1) // PIPELINE_CHUNK_SIZE
    for i in range(0, len(items), PIPELINE_CHUNK_SIZE):
        chunk = items[i : i + PIPELINE_CHUNK_SIZE]
        chunk_num = i // PIPELINE_CHUNK_SIZE + 1
        print(
            f"  verify-email chunk {chunk_num}/{total_chunks} ({len(chunk)} rows)...",
            flush=True,
        )
        summaries.append(
            _run_verify_email_batch_once(
                om_dir,
                chunk,
                timeout=_resolve_timeout(len(chunk), override=timeout),
                skill_dir=skill_dir,
            )
        )
    return _merge_pipeline_summaries(summaries)


SERPER_ATTEMPTED_TAG = "serper_attempted"
MV_ATTEMPTED_TAG = "mv_attempted"
SCRUBBY_DEEP_SUBMITTED_TAG = "scrubby_deep_submitted"
SCRUBBY_DEEP_ATTEMPTED_TAG = "scrubby_deep_attempted"
TAG_BULK_CHUNK_SIZE = 500


def run_tag_bulk(
    om_dir: Path,
    workspace: str,
    lead_ids: list[int],
    tags: list[str],
    *,
    remove: bool = False,
    timeout: int = 120,
    skill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Add or remove workspace tags via pipeline.py tag bulk (chunked for large id lists)."""
    if not workspace or not lead_ids or not tags:
        return {"status": "noop", "changed": 0, "leads": 0, "tags": tags}
    tag_str = ",".join(tags)
    summaries: list[dict[str, Any]] = []
    for i in range(0, len(lead_ids), TAG_BULK_CHUNK_SIZE):
        chunk = lead_ids[i : i + TAG_BULK_CHUNK_SIZE]
        cmd = [
            sys.executable,
            str(get_pipeline_path(om_dir)),
            "tag",
            "bulk",
            "--workspace",
            workspace,
            "--lead-ids",
            ",".join(str(lid) for lid in chunk),
            "--tags",
            tag_str,
        ]
        if remove:
            cmd.append("--remove")
        summaries.append(
            _run_subprocess_json(cmd, temp_path=None, timeout=timeout, skill_dir=skill_dir)
        )
    if len(summaries) == 1:
        return summaries[0]
    changed = sum(int(s.get("changed") or 0) for s in summaries)
    return {
        "status": summaries[-1].get("status", "added"),
        "changed": changed,
        "leads": len(lead_ids),
        "tags": tags,
        "chunks": len(summaries),
    }


def validate_endpoint_url(url: str, allowed_host_suffixes: list[str]) -> str:
    """Validate an endpoint URL hostname matches one of the allowed suffixes.

    Returns the validated URL. Raises ValueError if the URL is malformed or
    the hostname is not in the allowlist. This prevents SSRF via config
    overrides by rejecting private IPs and unexpected hostnames.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"Invalid endpoint URL (no hostname): {url!r}")
    # Block IP addresses (both IPv4 and IPv6) — only named hostnames allowed
    import ipaddress

    is_ip = False
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        pass

    if is_ip:
        raise ValueError(
            f"Endpoint URL {url!r} uses raw IP address; "
            f"only named hostnames are allowed."
        )

    if not host.replace(".", "").replace("-", "").isalnum():
        raise ValueError(
            f"Endpoint URL {url!r} has an invalid hostname: {host!r}"
        )

    # Check against allowed suffixes
    host_lower = host.lower()
    for suffix in allowed_host_suffixes:
        suffix_lower = suffix.lower().lstrip(".")
        if host_lower == suffix_lower or host_lower.endswith("." + suffix_lower):
            return url

    raise ValueError(
        f"Endpoint URL {url!r} hostname {host!r} does not match any "
        f"allowed host suffix: {allowed_host_suffixes}"
    )
