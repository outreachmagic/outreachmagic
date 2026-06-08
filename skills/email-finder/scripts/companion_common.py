"""Shared helpers for outreachmagic companion skills (lead-enrich, email-finder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
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
    "OUTREACHMAGIC_AGENT_KEY",
})

_POOL_API_KEY_BASES = (
    "SERPER_API_KEY",
    "TRYKITT_API_KEY",
    "ICYPEAS_API_KEY",
    "MILLIONVERIFIER_API_KEY",
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


def load_dotenv_file(
    path: Path,
    *,
    force_api_keys: bool = False,
    override_existing: bool = False,
    override_all: bool = False,
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
    # Dashboard-synced keys first; local .env files only fill gaps (force_api_keys skips set vars).
    _load_synced_agent_secrets(skill_dir)
    home = agent_home()
    for name in (".env", "default.env"):
        load_dotenv_file(home / name, force_api_keys=True)
    profile = active_profile()
    if profile:
        load_dotenv_file(home / "profiles" / profile / ".env", force_api_keys=True)
    repo_env = _monorepo_dotenv(skill_dir)
    if repo_env:
        load_dotenv_file(repo_env, force_api_keys=True)
    if skill_dir:
        load_dotenv_file(skill_dir / "default.env", force_api_keys=True)
    _AGENT_ENV_LOADED = True


def subprocess_env(skill_dir: Optional[Path] = None) -> dict[str, str]:
    ensure_agent_env_loaded(skill_dir)
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


def skill_dir_from_script(script_file: str) -> Path:
    return Path(script_file).resolve().parent.parent


def find_outreachmagic(
    config: dict[str, Any],
    skill_dir: Optional[Path] = None,
) -> Optional[Path]:
    if config.get("outreachmagic_home"):
        home = Path(config["outreachmagic_home"]).expanduser()
        if (home / "scripts" / "pipeline.py").exists():
            return home
        return None
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
    load_dotenv_file(agent_secrets_cloud.agent_secrets_path(), override_all=True)


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
            "Install via: python3 scripts/pipeline.py login"
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
    """Save email-finder batch output to OM (fast apply when every row has lead id)."""
    if not workspace:
        raise RuntimeError("--workspace is required to save batch results to Outreach Magic")
    if not profiles:
        return {}
    if profiles_have_known_lead_ids(profiles):
        return run_apply_email_find_results(
            om_dir,
            profiles,
            workspace=workspace,
            overwrite=overwrite,
            source=source,
            source_detail=source_detail,
            timeout=timeout,
            skill_dir=skill_dir,
        )
    return run_import_profiles(
        om_dir,
        profiles,
        workspace=workspace,
        overwrite=overwrite,
        source=source,
        source_detail=source_detail,
        timeout=timeout,
        skill_dir=skill_dir,
    )


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
