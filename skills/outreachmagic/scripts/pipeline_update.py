"""
Update/rollback + Config functions extracted from pipeline.py.

Bundled together to avoid circular import (Config functions like
load_config/save_config are needed by update functions).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import urllib.parse as urllib_parse

from constants import (
    RELAY_BULK_THRESHOLD,
    RELAY_PULL_PAGE_SIZE,
    RELAY_PUSH_BATCH_SIZE,
    RELAY_PUSH_EVENTS_BULK,
    RELAY_PUSH_MAX_ATTEMPTS,
    RELAY_PUSH_MAX_BULK,
    RELAY_PUSH_RETRY_BASE_SECONDS,
    RELAY_PUSH_ROUTINE_MAX,
    RELAY_PUSH_SNAPSHOT_BULK,
    RELAY_PUSH_TIMEOUT_SECONDS,
)
from data_freshness import (
    freshness_from_last_pull,
    is_pull_fresh_enough,
    parse_duration,
)
from db_conn import get_conn
from om_paths import (
    check_duplicate_installs,
    get_config_path,
    get_data_root,
    get_install_dir,
)
from workspace_routing import (
    DEFAULT_ORG_ID,
    VALID_WORKSPACE_ROUTING_MODES,
    WORKSPACE_ROUTING_MULTI,
    WORKSPACE_ROUTING_SINGLE,
    ensure_default_org_workspace,
    ensure_organization,
)


# ── Version / release helpers ────────────────────────────────────────


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.strip().split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts) or (0,)


def skill_scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def normalize_release_tag(tag: str) -> str:
    tag = tag.strip()
    if tag and not tag.startswith(("v", "V")):
        return f"v{tag}"
    return tag


def release_tag_version(tag: str) -> str:
    return normalize_release_tag(tag).lstrip("vV")


def effective_update_target() -> tuple[str, str]:
    from pipeline import GITHUB_REPO, SKILL_REPO_PATH

    return GITHUB_REPO, SKILL_REPO_PATH


def update_release_candidates() -> list[tuple[str, str]]:
    return [effective_update_target()]


def raw_repo_base_for_tag(
    tag: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    from pipeline import SKILL_REPO_PATH

    repo, _path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    return f"https://raw.githubusercontent.com/{repo}/{normalize_release_tag(tag)}"


def scripts_base_for_tag(
    tag: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    from pipeline import SKILL_REPO_PATH

    repo, path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    base = f"https://raw.githubusercontent.com/{repo}/{normalize_release_tag(tag)}"
    if path == ".":
        return f"{base}/scripts"
    return f"{base}/{path}/scripts"


def raw_repo_base_for_branch(
    branch: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    from pipeline import SKILL_REPO_PATH

    repo, _path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    return f"https://raw.githubusercontent.com/{repo}/{branch.strip()}"


def scripts_base_for_branch(
    branch: str,
    *,
    github_repo: Optional[str] = None,
    skill_repo_path: Optional[str] = None,
) -> str:
    from pipeline import SKILL_REPO_PATH

    repo, path = effective_update_target() if github_repo is None else (github_repo, skill_repo_path or SKILL_REPO_PATH)
    base = raw_repo_base_for_branch(branch, github_repo=repo, skill_repo_path=path)
    if path == ".":
        return f"{base}/scripts"
    return f"{base}/{path}/scripts"


def update_manifest_url(repo_base: str, skill_repo_path: str) -> str:
    if skill_repo_path == ".":
        return f"{repo_base.rstrip('/')}/update-manifest.json"
    return f"{repo_base.rstrip('/')}/{skill_repo_path}/update-manifest.json"


def skill_md_url_for_repo(repo_base: str, skill_repo_path: str) -> str:
    if skill_repo_path == ".":
        return f"{repo_base.rstrip('/')}/SKILL.md"
    return f"{repo_base.rstrip('/')}/{skill_repo_path}/SKILL.md"


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    from pipeline import __version__

    req = urllib.request.Request(url, headers={"User-Agent": f"Outreach Magic/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_latest_release() -> Optional[dict]:
    from pipeline import __version__

    for github_repo, skill_path in update_release_candidates():
        releases_url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        try:
            req = urllib.request.Request(
                releases_url,
                headers={
                    "User-Agent": f"Outreach Magic/{__version__}",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
            continue

        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            continue
        return {
            "tag": normalize_release_tag(tag),
            "version": release_tag_version(tag),
            "base": scripts_base_for_tag(tag, github_repo=github_repo, skill_repo_path=skill_path),
            "github_repo": github_repo,
            "skill_repo_path": skill_path,
        }
    return None


def dev_update_base_url() -> Optional[str]:
    """Dev-only override via config key dev_update_url (not env)."""
    cfg = load_config() if get_config_path().exists() else {}
    url = (cfg.get("dev_update_url") or "").strip()
    return url.rstrip("/") if url else None


def fetch_remote_version() -> Optional[str]:
    """Latest published release version, or None if no release is available."""
    release = fetch_latest_release()
    if release:
        return release["version"]
    if dev_update_base_url():
        try:
            return _fetch_url(f"{dev_update_base_url()}/VERSION").decode().strip()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return None
    return None


UPDATE_CHECK_INTERVAL_DEFAULT = 3600


def get_update_check_interval() -> int:
    cfg = load_config() if get_config_path().exists() else {}
    raw = cfg.get("update_check_interval_seconds", UPDATE_CHECK_INTERVAL_DEFAULT)
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return UPDATE_CHECK_INTERVAL_DEFAULT


def update_check_due() -> bool:
    cfg = load_config() if get_config_path().exists() else {}
    last = cfg.get("update_checked_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age >= get_update_check_interval()
    except (ValueError, TypeError):
        return True


def record_update_check():
    cfg = load_config()
    cfg["update_checked_at"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)


def notify_update_available(quiet: bool = False) -> None:
    """Check-only: inform the user when a newer release exists (never downloads)."""
    from pipeline import __version__

    if not update_check_due():
        return
    record_update_check()
    remote = fetch_remote_version()
    if not remote or parse_version(remote) <= parse_version(__version__):
        return
    if quiet:
        return
    print(
        f"outreachmagic: update available {__version__} → {remote} "
        "(ask Outreach Magic to update)",
        file=sys.stderr,
    )


def check_skill_update(quiet: bool = False) -> bool:
    """Return True if installed scripts match or exceed the latest release."""
    from pipeline import __version__

    remote = fetch_remote_version()
    cfg = load_config()
    installed_tag = (cfg.get("installed_from_tag") or "").strip() or "unknown"
    if not quiet:
        print(f"Installed: {__version__} (source: {installed_tag})")
        if remote:
            print(f"Latest release: {remote}")
    if not remote or parse_version(remote) <= parse_version(__version__):
        return True
    if not quiet:
        print(
            f"Update available: {__version__} → {remote} "
            "(ask Outreach Magic to update)"
        )
    return False


def sync_skill_md_version():
    """Align SKILL.md frontmatter version with scripts/VERSION."""
    from pipeline import _read_version_file

    dest = skill_scripts_dir()
    ver = _read_version_file(dest / "VERSION")
    skill_md = dest.parent / "SKILL.md"
    if skill_md.exists():
        text = skill_md.read_text()
        skill_md.write_text(re.sub(r"^version: .*", f"version: {ver}", text, count=1, flags=re.M))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_update_manifest(repo_base: str, skill_repo_path: Optional[str] = None) -> Optional[dict]:
    _, default_path = effective_update_target()
    path = skill_repo_path if skill_repo_path is not None else default_path
    url = update_manifest_url(repo_base, path)
    try:
        manifest_raw = _fetch_url(url)
        payload = json.loads(manifest_raw.decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        sums_url = f"{repo_base.rstrip('/')}/SHA256SUMS"
        sums_raw = _fetch_url(sums_url).decode()
        manifest_hash = _sha256_hex(manifest_raw)
        found = False
        for line in sums_raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == "update-manifest.json":
                if parts[0].strip() == manifest_hash:
                    found = True
                break
        if not found and any("update-manifest.json" in line for line in sums_raw.splitlines()):
            raise RuntimeError(
                "update-manifest.json hash does not match SHA256SUMS. "
                "Refusing to install. Report at security@outreachmagic.io."
            )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass

    return payload


def update_download_names(manifest: Optional[dict] = None) -> list[str]:
    from pipeline import UPDATE_MANIFEST_FILES

    files = (manifest or {}).get("files") if isinstance(manifest, dict) else None
    if isinstance(files, dict) and files:
        return sorted(
            name for name in files
            if name != "manifest.json" and not name.endswith((".md", ".sh", ".txt"))
            and (name.endswith((".py", ".html")) or name == "VERSION")
        )
    return list(UPDATE_MANIFEST_FILES)


def scripts_rollback_dir() -> Path:
    return get_config_path().parent / "scripts-rollback"


def backup_scripts_for_rollback(dest: Path) -> None:
    """Snapshot scripts/ before update so pipeline.py rollback can restore."""
    from pipeline import _read_version_file
    from pipeline import scripts_rollback_dir

    backup = scripts_rollback_dir()
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(dest, backup)
    meta = {
        "version": _read_version_file(dest / "VERSION"),
        "installed_from_tag": load_config().get("installed_from_tag"),
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
    }
    (get_config_path().parent / "scripts-rollback-meta.json").write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )


def _skill_scripts_in_git_checkout() -> Optional[Path]:
    from pipeline import skill_scripts_dir

    p = skill_scripts_dir().resolve()
    for parent in (p, *p.parents):
        if (parent / ".git").exists():
            return parent
    return None


def rollback_skill() -> dict:
    """Restore skill scripts from the pre-update backup."""
    from pipeline import _load_json_dict, _read_version_file, sync_skill_md_version
    from pipeline import _skill_scripts_in_git_checkout, scripts_rollback_dir, skill_scripts_dir
    from pipeline import load_config, save_config

    backup = scripts_rollback_dir()
    if not backup.is_dir():
        return {
            "status": "error",
            "error": "no_rollback_snapshot",
            "message": "No rollback snapshot found. Run pipeline.py update first.",
        }
    repo_root = _skill_scripts_in_git_checkout()
    if repo_root is not None:
        return {
            "status": "error",
            "error": "dev_checkout_protected",
            "message": (
                "Refusing to roll back skill scripts inside a git working tree "
                f"({repo_root}). This is a development checkout — use git to manage "
                "changes instead of update/rollback."
            ),
        }
    dest = skill_scripts_dir()
    meta_path = get_config_path().parent / "scripts-rollback-meta.json"
    meta = _load_json_dict(meta_path) if meta_path.is_file() else {}
    for path in dest.iterdir():
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    for item in backup.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    if meta.get("installed_from_tag"):
        cfg = load_config()
        cfg["installed_from_tag"] = meta["installed_from_tag"]
        save_config(cfg)
    sync_skill_md_version()
    return {
        "status": "rolled_back",
        "version": _read_version_file(dest / "VERSION"),
        "restored_from_tag": meta.get("installed_from_tag"),
        "path": str(dest),
    }


def _resolve_git_tag(repo_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--exact-match"],
            capture_output=True, text=True, timeout=5,
        )
        tag = result.stdout.strip()
        return tag if tag else None
    except Exception:
        return None


def resolve_update_source(
    explicit_tag: Optional[str] = None,
    *,
    channel: str = "release",
) -> tuple[Optional[Path], str, str, str]:
    from pipeline import SKILL_REPO_PATH

    dev_repo = (load_config().get("dev_repo") or "").strip() if get_config_path().exists() else ""
    if dev_repo:
        src = Path(dev_repo) / SKILL_REPO_PATH / "scripts"
        if not src.is_dir():
            raise FileNotFoundError(
                f"dev_repo in config has no {SKILL_REPO_PATH}/scripts/: {src}"
            )
        label = _resolve_git_tag(Path(dev_repo)) or f"dev@{Path(dev_repo).name}"
        return src, "", str(Path(dev_repo)), label

    dev_base = dev_update_base_url()
    if dev_base:
        repo_base = dev_base.rsplit(f"/{SKILL_REPO_PATH}/scripts", 1)[0]
        return None, dev_base, repo_base, "dev_update_url"

    github_repo, skill_path = effective_update_target()

    if explicit_tag:
        norm = normalize_release_tag(explicit_tag)
        return (
            None,
            scripts_base_for_tag(norm, github_repo=github_repo, skill_repo_path=skill_path),
            raw_repo_base_for_tag(norm, github_repo=github_repo, skill_repo_path=skill_path),
            norm,
        )

    if channel == "main":
        return (
            None,
            scripts_base_for_branch("main", github_repo=github_repo, skill_repo_path=skill_path),
            raw_repo_base_for_branch("main", github_repo=github_repo, skill_repo_path=skill_path),
            "main",
        )

    release = fetch_latest_release()
    if not release:
        raise RuntimeError(
            "No GitHub release found on the platform update repo for this install. "
            "Publish a release (see docs/RELEASING.md), run "
            "pipeline.py update --tag vX.Y.Z, or set dev_repo in config."
        )
    rel_repo = release.get("github_repo") or github_repo
    rel_path = release.get("skill_repo_path") or skill_path
    repo_base = raw_repo_base_for_tag(release["tag"], github_repo=rel_repo, skill_repo_path=rel_path)
    return None, release["base"], repo_base, release["tag"]


def _migrate_db_in_subprocess(dest: Path) -> None:
    """Run init_db() in a brand-new interpreter, against the just-copied code.

    update_skill() overwrites this install's scripts on disk, then (formerly)
    called init_db() in the *same* process. Python never re-reads a module
    once it is in sys.modules -- `from pipeline_migration import init_db`
    here hands back whatever `pipeline_migration` looked like when this
    process started (before the copy), not the fresh-off-disk version.

    That stale module can still reach *fresh* code, though: anything it
    lazily imports for the first time in this process (e.g. `pipeline_
    migration` calling `from sync_contract import SYNC_MAP` inside a
    function body, not at module top) gets read off disk on demand -- which
    by then is the new version. A stale migrate_db() paired with a fresh
    sync_contract.SYNC_MAP is exactly how this broke in practice: the old
    migrate_db() never created `lead_provider_observations`, but the new
    SYNC_MAP told the (still-old-process) outbox-trigger builder to install a
    trigger on it anyway -- `no such table: main.lead_provider_observations`.

    A fresh interpreter can't have this split: every module it imports comes
    from the files on disk right now, so migrate_db() and everything it
    touches are either all-old or all-new, never a mix. This is also exactly
    what happens naturally on the *next* CLI invocation anyway (main() calls
    migrate_db() unconditionally) -- this just does it now, in a way that
    can't observe half-updated state, instead of leaving the DB unmigrated
    until whatever command happens to run next.
    """
    env = {**os.environ, "OUTREACHMAGIC_DATA_ROOT": str(get_data_root())}
    result = subprocess.run(
        [sys.executable, "-c", "from pipeline_migration import init_db; init_db()"],
        cwd=str(dest),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Database migration failed after updating scripts:\n{result.stderr.strip()}"
        )


def update_skill(explicit_tag: Optional[str] = None, *, channel: str = "release") -> dict:
    """Download or copy a tagged release into this skill install, then migrate DB."""
    from pipeline import ROOT_SKILL_FILES, _fetch_url, _read_version_file
    from pipeline import _sha256_hex, _skill_scripts_in_git_checkout, backup_scripts_for_rollback, effective_update_target, fetch_update_manifest, resolve_update_source, skill_scripts_dir, skill_md_url_for_repo, update_download_names

    dest = skill_scripts_dir()
    repo_root = _skill_scripts_in_git_checkout()
    if repo_root is not None:
        return {
            "status": "error",
            "error": "dev_checkout_protected",
            "message": (
                "Refusing to update skill scripts inside a git working tree "
                f"({repo_root}). Update the installed copy, not the dev checkout."
            ),
        }
    backup_scripts_for_rollback(dest)
    local_src, scripts_base, repo_base, source_label = resolve_update_source(
        explicit_tag, channel=channel,
    )
    updated: list[str] = []
    _, skill_path = effective_update_target()
    manifest: Optional[dict] = None
    if local_src:
        local_manifest = local_src.parent / "update-manifest.json"
        if local_manifest.is_file():
            try:
                manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = None
    else:
        manifest = fetch_update_manifest(repo_base, skill_path)

    download_names = update_download_names(manifest)

    if local_src:
        for name in download_names:
            src = local_src / name
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / name)
            updated.append(name)
        skill_md_src = local_src.parent / "SKILL.md"
        if skill_md_src.is_file():
            shutil.copy2(skill_md_src, dest.parent / "SKILL.md")
            updated.append("SKILL.md")
        for name in ROOT_SKILL_FILES:
            root_src = local_src.parent / name
            if root_src.is_file():
                shutil.copy2(root_src, dest.parent / name)
                updated.append(name)
    else:
        for name in download_names:
            content = _fetch_url(f"{scripts_base}/{name}")
            expected = (manifest or {}).get("files", {}).get(name)
            if expected and _sha256_hex(content) != expected:
                raise RuntimeError(
                    f"Checksum mismatch for {name} from {source_label}. "
                    "Refusing to install. Try again or report at security@outreachmagic.io."
                )
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(content)
            updated.append(name)
        try:
            skill_md_url = skill_md_url_for_repo(repo_base, skill_path)
            skill_content = _fetch_url(skill_md_url)
            expected_md = (manifest or {}).get("files", {}).get("SKILL.md")
            if expected_md and _sha256_hex(skill_content) != expected_md:
                raise RuntimeError("Checksum mismatch for SKILL.md. Refusing to install.")
            (dest.parent / "SKILL.md").write_bytes(skill_content)
            updated.append("SKILL.md")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        for name in ROOT_SKILL_FILES:
            url = f"{repo_base.rstrip('/')}/{skill_path}/{name}"
            try:
                content = _fetch_url(url)
                expected = (manifest or {}).get("files", {}).get(name)
                if expected and _sha256_hex(content) != expected:
                    raise RuntimeError(f"Checksum mismatch for {name}. Refusing to install.")
                (dest.parent / name).write_bytes(content)
                updated.append(name)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError):
                pass

    _migrate_db_in_subprocess(dest)
    sync_skill_md_version()
    cfg = load_config()
    cfg["auto_update"] = False
    cfg["installed_from_tag"] = source_label
    cfg.pop("update_url", None)
    save_config(cfg)
    new_version = _read_version_file(dest / "VERSION")
    return {
        "status": "updated",
        "version": new_version,
        "files": updated,
        "path": str(dest),
        "source": source_label,
    }


def record_install_source(source_label: str) -> dict:
    """Record which release tag or branch installed this skill (install.sh / update)."""
    label = (source_label or "").strip() or "main"

    from pipeline_migration import init_db

    init_db()
    cfg = load_config()
    cfg["installed_from_tag"] = label
    save_config(cfg)
    return {"status": "ok", "installed_from_tag": label}


# ── Config functions ─────────────────────────────────────────────────


def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    if get_config_path().exists():
        return _load_json_dict(get_config_path())
    return {}


def _read_positive_int(raw: object, fallback: int) -> int:
    try:
        val = int(str(raw).strip())
        return val if val > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _cloud_snapshot_pending_count() -> int:
    conn = get_conn()
    try:
        from pipeline import get_last_sync

        last_sync = get_last_sync()
        if last_sync:
            core = conn.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE updated_at > ?", (last_sync,)
            ).fetchone()["n"]
            ws = conn.execute(
                "SELECT COUNT(*) AS n FROM workspace_leads WHERE updated_at > ?", (last_sync,)
            ).fetchone()["n"]
            companies = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE updated_at > ?", (last_sync,)
            ).fetchone()["n"]
        else:
            core = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
            ws = conn.execute("SELECT COUNT(*) AS n FROM workspace_leads").fetchone()["n"]
            companies = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    finally:
        conn.close()
    return int(core) + int(ws) + int(companies)


def _use_bulk_transport(
    pending_count: int,
    *,
    force_bulk: Optional[bool] = None,
) -> dict:
    if force_bulk is not None:
        bulk = force_bulk
    else:
        bulk = pending_count >= RELAY_BULK_THRESHOLD
    batch_size = get_relay_push_settings(bulk=bulk, snapshot_bulk=True)["batch_size"]
    return {
        "bulk": bulk,
        "push_batch_size": batch_size,
        "pull_limit": RELAY_PULL_PAGE_SIZE,
    }


def _sync_events_only() -> bool:
    phase = os.environ.get("OM_SYNC_PHASE", "").strip().lower()
    if phase == "events":
        return True
    flag = os.environ.get("OM_SYNC_EVENTS_ONLY", "").strip().lower()
    return flag in ("1", "true", "yes")


def get_relay_push_settings(*, bulk: bool = False, snapshot_bulk: bool = False) -> dict:
    cfg = load_config()
    if bulk and _sync_events_only():
        default_batch = RELAY_PUSH_EVENTS_BULK
        batch_cap = RELAY_PUSH_MAX_BULK
    elif bulk and snapshot_bulk:
        default_batch = RELAY_PUSH_SNAPSHOT_BULK
        batch_cap = RELAY_PUSH_MAX_BULK
    else:
        default_batch = RELAY_PUSH_MAX_BULK if bulk else RELAY_PUSH_BATCH_SIZE
        batch_cap = RELAY_PUSH_MAX_BULK if bulk else RELAY_PUSH_ROUTINE_MAX
    batch_size = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_BATCH_SIZE", cfg.get("sync_batch_size", default_batch)),
        default_batch,
    )
    timeout_seconds = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS", cfg.get("sync_timeout_seconds", RELAY_PUSH_TIMEOUT_SECONDS)),
        RELAY_PUSH_TIMEOUT_SECONDS,
    )
    max_attempts = _read_positive_int(
        os.environ.get("OUTREACHMAGIC_SYNC_MAX_ATTEMPTS", cfg.get("sync_max_attempts", RELAY_PUSH_MAX_ATTEMPTS)),
        RELAY_PUSH_MAX_ATTEMPTS,
    )
    retry_base_seconds = _read_positive_int(
        os.environ.get(
            "OUTREACHMAGIC_SYNC_RETRY_BASE_SECONDS",
            cfg.get("sync_retry_base_seconds", RELAY_PUSH_RETRY_BASE_SECONDS),
        ),
        RELAY_PUSH_RETRY_BASE_SECONDS,
    )
    batch_size = max(10, min(batch_size, batch_cap))
    timeout_seconds = max(10, min(timeout_seconds, 300))
    max_attempts = max(1, min(max_attempts, 10))
    retry_base_seconds = max(1, min(retry_base_seconds, 60))
    return {
        "batch_size": batch_size,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "bulk": bulk,
    }


def _chmod_best_effort(path: Path, mode: int):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def save_config(cfg: dict):
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)
    path.write_text(json.dumps(cfg, indent=2))
    _chmod_best_effort(path, 0o600)


def _warn_duplicate_installs() -> None:
    if load_config().get("suppress_duplicate_warning"):
        return
    duplicates = check_duplicate_installs()
    if not duplicates:
        return
    active = get_install_dir()
    print("", file=sys.stderr)
    print("\u26a0  Outreach Magic is installed in multiple agent directories.", file=sys.stderr)
    for dup in duplicates:
        print(f"   - {dup['path']}", file=sys.stderr)
    print("", file=sys.stderr)
    print("   Run `pipeline.py init --agent <name>` to choose which one to use.", file=sys.stderr)
    print("   Options: cursor, agents, claude, hermes", file=sys.stderr)
    print("", file=sys.stderr)
    print("   Already set up? Symlink extras to avoid duplicates:", file=sys.stderr)
    print(f"     rm -rf '{duplicates[0]['path']}' && ln -s '{active}' '{duplicates[0]['path']}'", file=sys.stderr)


def get_agent_key() -> Optional[str]:
    return os.environ.get("OUTREACHMAGIC_AGENT_KEY") or load_config().get("agent_key")


def get_last_pull() -> Optional[str]:
    return load_config().get("last_pull")


def pull_if_stale_skip_result(if_stale: Optional[str], *, force: bool = False) -> Optional[dict]:
    if force or not if_stale:
        return None
    max_sec = parse_duration(if_stale)
    if max_sec is None:
        raise ValueError(
            f"Invalid --if-stale duration {if_stale!r}; use forms like 5m, 1h, 2d"
        )
    last = get_last_pull()
    if is_pull_fresh_enough(last, max_sec):
        meta = freshness_from_last_pull(last)
        return {
            "skipped": True,
            "reason": "fresh",
            "if_stale": if_stale,
            **meta,
        }
    return None


def set_last_pull(ts: str):
    cfg = load_config()
    if ts and "T" in ts:
        ts = ts.replace("T", " ").split(".")[0].split("+")[0].split("Z")[0]
    cfg["last_pull"] = ts
    save_config(cfg)


def get_last_sync() -> Optional[str]:
    from pipeline import load_config

    cfg = load_config()
    raw = cfg.get("last_sync") or cfg.get("last_pull")
    if raw:
        raw = raw.replace("T", " ").split(".")[0].split("+")[0].split("Z")[0]
    return raw


def set_last_sync(ts: str):
    from pipeline import load_config, save_config

    cfg = load_config()
    if ts and "T" in ts:
        ts = ts.replace("T", " ").split(".")[0].split("+")[0].split("Z")[0]
    cfg["last_sync"] = ts
    save_config(cfg)


def get_last_max_id() -> Optional[int]:
    return load_config().get("last_max_id")


_SNAPSHOT_CURSOR_KEYS = {
    "core": "last_snapshot_core_after_id",
    "workspace": "last_snapshot_workspace_after_id",
    "company": "last_snapshot_company_after_id",
}


def get_snapshot_cursor(kind: str = "workspace") -> int:
    key = _SNAPSHOT_CURSOR_KEYS.get(kind, _SNAPSHOT_CURSOR_KEYS["workspace"])
    cfg = load_config()
    return int(cfg.get(key) or 0)


def set_snapshot_cursor(snapshot_id: int, kind: str = "workspace") -> None:
    key = _SNAPSHOT_CURSOR_KEYS.get(kind, _SNAPSHOT_CURSOR_KEYS["workspace"])
    cfg = load_config()
    cfg[key] = int(snapshot_id)
    save_config(cfg)


def clear_snapshot_cursors() -> None:
    cfg = load_config()
    for key in _SNAPSHOT_CURSOR_KEYS.values():
        cfg.pop(key, None)
    cfg.pop("last_snapshot_after_id", None)
    save_config(cfg)


def snapshot_as_of() -> str:
    """The 'as of' clock stamped on every snapshot entry: when we serialized it.

    This is what orders two writes on the relay (its stale-write guard compares it),
    and it must be monotone. The obvious candidates are not:

      * leads.created_at is CONSTANT across every version of an entity, so two pushes
        of the same lead are indistinguishable.
      * leads.updated_at is corrupt -- 60,942 of 149,753 rows (40.7%) have an
        updated_at OLDER than their own created_at.

    The moment of serialization has neither problem: a payload built later reflects
    later state, by construction.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def normalize_relay_timestamp(ts: Optional[str]) -> str:
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    s = str(ts).strip()
    if "T" in s:
        if s.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", s):
            return s
        return s + "+00:00"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(\.\d+)?$", s)
    if m:
        frac = m.group(3) or ".000000"
        return f"{m.group(1)}T{m.group(2)}{frac}+00:00"
    return s


def _parse_timestamp_to_utc(ts: str) -> Optional[datetime]:
    """Parse an epoch, ISO-8601, or naive "YYYY-MM-DD HH:MM:SS" string into an
    aware UTC datetime. Naive input is assumed to already be UTC -- the same
    convention SQLite's own ``datetime('now')`` uses. Returns None if ``ts``
    doesn't match any recognized shape.
    """
    s = ts.strip()
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    if "T" not in iso:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(\.\d+)?$", iso)
        if not m:
            return None
        iso = f"{m.group(1)}T{m.group(2)}{m.group(3) or ''}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def utc_now_for_storage() -> str:
    """Current UTC time in the exact shape SQLite's ``datetime('now')`` produces.

    Every ``*_at`` column in the schema defaults to and is compared against
    that shape (``datetime('now', '-N days')`` etc). Writing anything else --
    e.g. ``datetime.now(timezone.utc).isoformat()`` -- into one of those
    columns sorts incorrectly against such filters even though it encodes a
    valid, correct instant, because the comparison is a plain text comparison.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_relay_timestamp_for_storage(ts: Optional[str]) -> str:
    """Normalize a relay/webhook timestamp for writing into a ``*_at`` column.

    Unlike ``normalize_relay_timestamp`` (which produces ISO-8601 for outbound
    export payloads), this always returns SQLite's own ``datetime('now')``
    shape so lexicographic ``datetime('now', ...)`` range filters stay valid
    regardless of which code path wrote the row. Unparseable input is passed
    through unchanged rather than silently replaced with "now", so bad data
    stays visible instead of being masked.
    """
    if not ts:
        return utc_now_for_storage()
    dt = _parse_timestamp_to_utc(str(ts))
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else str(ts).strip()


def set_last_max_id(max_id: int):
    cfg = load_config()
    cfg["last_max_id"] = max_id
    save_config(cfg)


def get_or_create_client_id() -> str:
    cfg = load_config()
    cid = cfg.get("client_id")
    if cid:
        return cid
    cid = str(uuid.uuid4())
    cfg["client_id"] = cid
    save_config(cfg)
    return cid


def get_workspace_routing_mode_from_config() -> Optional[str]:
    raw = str(load_config().get("workspace_routing_mode") or "").strip().lower()
    if raw in VALID_WORKSPACE_ROUTING_MODES:
        return raw
    return None


def sync_workspace_routing_mode_from_config(org_id: str = DEFAULT_ORG_ID):
    mode = get_workspace_routing_mode_from_config()
    if not mode:
        return
    conn = get_conn()
    ensure_organization(conn, org_id)
    row = conn.execute(
        "SELECT workspace_routing_mode, default_workspace_id FROM organizations WHERE id = ?",
        (org_id,),
    ).fetchone()
    current_mode = (row["workspace_routing_mode"] or "").strip().lower() if row else ""
    current_ws_id = (row["default_workspace_id"] or "").strip() if row else ""
    if mode == WORKSPACE_ROUTING_SINGLE:
        ws_id = current_ws_id or ensure_default_org_workspace(conn)
        if current_mode != WORKSPACE_ROUTING_SINGLE or current_ws_id != ws_id:
            conn.execute(
                """UPDATE organizations
                   SET workspace_routing_mode = ?, default_workspace_id = ? WHERE id = ?""",
                (WORKSPACE_ROUTING_SINGLE, ws_id, org_id),
            )
            conn.commit()
    else:
        if current_mode != WORKSPACE_ROUTING_MULTI or current_ws_id:
            conn.execute(
                """UPDATE organizations
                   SET workspace_routing_mode = ?, default_workspace_id = NULL WHERE id = ?""",
                (WORKSPACE_ROUTING_MULTI, org_id),
            )
            conn.commit()
    conn.close()
