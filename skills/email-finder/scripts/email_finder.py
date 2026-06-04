#!/usr/bin/env python3
"""
Email Finder — trykitt.ai + Icypeas for Hermes / Cursor / Claude Code.

Checks outreachmagic before spending provider credits. Batch mode uses incremental
CSV/JSON saves, bulk dedup (pipeline batch-lead-lookup), and bulk verify-email.

Usage:
    email_finder.py config
    email_finder.py check [--workspace W] "Name" "Company"
    email_finder.py find --name X --domain Y [--linkedin URL] [--save] [--workspace W]
    email_finder.py batch-find [options] input.json
    email_finder.py parallel-find [options] input.json   # alias: batch-find --workers N
    email_finder.py prepare-import --csv PATH [--workspace W] [--output PATH]
    email_finder.py import-to-om --file PATH [--workspace W]
    email_finder.py update [--check] [--tag vX.Y.Z]

batch-find options:
    --workspace W --delay 8 --workers 1 --max 500 --provider trykitt|icypeas
    --output-base PATH --output-csv PATH --no-save --skip-om --dry-run --yes
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# Prefer this skill's scripts/ over Hermes /opt/hermes (may contain other batch_runner.py).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import companion_common as cc
from batch_runner import (
    BatchOptions,
    build_import_profile,
    collect_import_profiles,
    run_batch,
    should_tag_provider_attempt,
)
from normalize import load_people_json, normalize_linkedin, row_fields, validate_domain
from progress import print_om_setup_box
from providers import (
    icypeas_find,
    icypeas_poll_result,
    provider_note_text,
    resolve_provider_names,
    run_find_with_fallback,
    trykitt_find,
    validity_to_verify_status,
)

SKILL_NAME = "email-finder"
GITHUB_REPO = "outreachmagic/email-finder"
GITHUB_RELEASES_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_BASE = "https://raw.githubusercontent.com"
UPDATE_FILES = (
    "SKILL.md",
    "README.md",
    "SECURITY.md",
    "config.example.json",
    "default.env",
    ".gitignore",
    "references/email-finding-research.md",
    "scripts/companion_common.py",
    "scripts/email_finder.py",
    "scripts/normalize.py",
    "scripts/progress.py",
    "scripts/health.py",
    "scripts/providers.py",
    "scripts/batch_runner.py",
)


def _find_skill_dir() -> Path:
    return cc.skill_dir_from_script(__file__)


def ensure_env_loaded() -> None:
    cc.ensure_agent_env_loaded(_find_skill_dir())


def load_config() -> dict[str, Any]:
    ensure_env_loaded()
    skill_dir = _find_skill_dir()
    cfg: dict[str, Any] = {}
    cfg_path = skill_dir / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    key = os.environ.get("TRYKITT_API_KEY", "").strip()
    if key:
        cfg["trykitt_api_key"] = key
    icypeas_key = os.environ.get("ICYPEAS_API_KEY", "").strip()
    if icypeas_key:
        cfg["icypeas_api_key"] = icypeas_key
    if os.environ.get("OUTREACHMAGIC_HOME"):
        cfg["outreachmagic_home"] = os.environ["OUTREACHMAGIC_HOME"]
    cfg.setdefault("trykitt_enabled", True)
    cfg.setdefault("icypeas_enabled", True)
    cfg.setdefault("icypeas_poll_attempts", 8)
    cfg.setdefault("icypeas_poll_delay_seconds", 2)
    cfg.setdefault("batch_delay_seconds", 8)
    cfg.setdefault("max_people_per_run", 500)
    return cfg


def find_outreachmagic(config: dict[str, Any]) -> Optional[Path]:
    return cc.find_outreachmagic(config, skill_dir=_find_skill_dir())


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def cmd_config() -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    key = cfg.get("trykitt_api_key", "")
    icypeas_key = cfg.get("icypeas_api_key", "")
    out: dict[str, Any] = {
        "skill": SKILL_NAME,
        "trykitt_api_key_set": bool(key),
        "trykitt_api_key_preview": _mask_key(key) if key else None,
        "icypeas_api_key_set": bool(icypeas_key),
        "icypeas_api_key_preview": _mask_key(icypeas_key) if icypeas_key else None,
        "outreachmagic_found": om_dir is not None,
        "outreachmagic_home": str(om_dir) if om_dir else None,
        "max_per_run": cfg.get("max_people_per_run", 500),
    }
    if om_dir:
        has_key, source = cc.outreachmagic_agent_key_status(om_dir)
        out["outreachmagic_agent_key"] = {"set": has_key, "source": source}
    print(json.dumps(out, indent=2))


def check_existing_email(
    om_dir: Path,
    name: str,
    company: Optional[str] = None,
    linkedin: Optional[str] = None,
    *,
    workspace: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "not_found",
        "lead_id": None,
        "email": None,
        "linkedin_url": None,
        "name": None,
        "company": None,
    }
    items: list[dict[str, Any]] = [{"index": 0}]
    if linkedin:
        items[0]["linkedin"] = linkedin
    elif name:
        items[0]["name"] = name
    try:
        payload = cc.run_batch_lead_lookup(
            om_dir, items, workspace=workspace or None, skill_dir=_find_skill_dir(),
        )
    except RuntimeError as e:
        result["error"] = str(e)
        return result
    entries = payload.get("results") or []
    if not entries or entries[0].get("status") != "found":
        return result
    lead_entry = entries[0]
    result.update({
        "status": "exists_with_email" if lead_entry.get("email") else "exists_no_email",
        "lead_id": lead_entry.get("lead_id"),
        "email": lead_entry.get("email"),
        "name": lead_entry.get("name"),
        "company": lead_entry.get("company"),
        "linkedin_url": lead_entry.get("linkedin_url"),
        "tags": lead_entry.get("tags") or [],
    })
    return result


def cmd_check(name: str, company: str, workspace: str = "") -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print_om_setup_box()
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)
    print(json.dumps(check_existing_email(om_dir, name, company, workspace=workspace), indent=2))


def batch_import_results(
    om_dir: Path,
    profiles: list[dict[str, Any]],
    *,
    workspace: str = "",
    source_detail: str = "email-finder/batch",
) -> dict[str, Any]:
    if not profiles:
        return {"imported": 0, "profiles": []}
    imported = cc.run_import_profiles(
        om_dir,
        profiles,
        workspace=workspace,
        source_detail=source_detail,
        skill_dir=_find_skill_dir(),
    )
    return {"imported": len(profiles), "import": imported}


def save_find_result(
    om_dir: Path,
    *,
    full_name: str,
    company: str,
    domain: str,
    linkedin: str,
    find_result: dict[str, Any],
    workspace: str = "",
) -> dict[str, Any]:
    email = find_result.get("email")
    if not email:
        return {"saved": False, "reason": "no email to save"}
    profile = build_import_profile(
        full_name=full_name,
        company=company,
        domain=domain,
        linkedin=linkedin,
        find_result=find_result,
        normalize_linkedin_fn=normalize_linkedin,
    )
    provider = str(find_result.get("provider") or "trykitt")
    imported = batch_import_results(
        om_dir, [{k: v for k, v in profile.items() if not str(k).startswith("_verify")}],
        workspace=workspace,
        source_detail=f"email-finder/{provider}",
    )
    imp = imported.get("import") or {}
    lead_id = None
    if isinstance(imp.get("results"), list) and imp["results"]:
        lead_id = imp["results"][0].get("lead_id") or imp["results"][0].get("id")
    if lead_id and email:
        try:
            cc.run_verify_email_batch(
                om_dir,
                [{
                    "lead_id": int(lead_id),
                    "email": email,
                    "status": validity_to_verify_status(
                        str(find_result.get("validity") or ""), provider=provider,
                    ),
                    "source": provider,
                    "source_detail": "email-finder/find",
                }],
                skill_dir=_find_skill_dir(),
            )
        except RuntimeError:
            pass
    return {"saved": True, "import": imp, "lead_id": lead_id}


def tag_provider_attempt(
    om_dir: Path,
    *,
    full_name: str,
    company: str,
    domain: str,
    linkedin: str = "",
    workspace: str = "",
    provider: str = "trykitt",
) -> dict[str, Any]:
    profile = build_import_profile(
        full_name=full_name,
        company=company,
        domain=domain,
        linkedin=linkedin,
        find_result={"provider": provider},
        normalize_linkedin_fn=normalize_linkedin,
    )
    imported = batch_import_results(
        om_dir,
        [{k: v for k, v in profile.items() if not str(k).startswith("_verify")}],
        workspace=workspace,
        source_detail=f"email-finder/{provider}-miss",
    )
    out: dict[str, Any] = {"tagged": True, "import": imported.get("import", {})}
    if not workspace:
        out["warning"] = "tags require --workspace on import-profiles"
    return out


def cmd_find(
    name: str,
    domain: str,
    linkedin: str = "",
    workspace: str = "",
    save: bool = False,
    company: str = "",
) -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if om_dir:
        existing = check_existing_email(om_dir, name, company or domain, linkedin, workspace=workspace)
        if existing.get("email"):
            print(json.dumps({
                "status": "skipped",
                "reason": "email already in outreachmagic",
                "existing": existing,
            }, indent=2))
            return
    result = run_find_with_fallback(cfg, full_name=name, domain=domain, linkedin=linkedin)
    if om_dir and save:
        if result.get("email"):
            result["save"] = save_find_result(
                om_dir,
                full_name=name,
                company=company or domain,
                domain=domain,
                linkedin=linkedin,
                find_result=result,
                workspace=workspace,
            )
        else:
            attempts = result.get("provider_attempts") if isinstance(result.get("provider_attempts"), list) else []
            taggable = [a for a in attempts if isinstance(a, dict) and should_tag_provider_attempt(a)]
            if taggable:
                profile = build_import_profile(
                    full_name=name,
                    company=company or domain,
                    domain=domain,
                    linkedin=linkedin,
                    find_result={
                        "provider": str(taggable[-1].get("provider") or "trykitt"),
                        "provider_attempts": taggable,
                    },
                    normalize_linkedin_fn=normalize_linkedin,
                )
                tag_result = batch_import_results(
                    om_dir,
                    [{k: v for k, v in profile.items() if not str(k).startswith("_verify")}],
                    workspace=workspace,
                    source_detail="email-finder/fallback-miss",
                )
                result["tag_attempt"] = {"tagged": True, "import": tag_result.get("import", {})}
    print(json.dumps(result, indent=2))


def _parse_batch_args(argv: list[str]) -> tuple[BatchOptions, str]:
    opts = BatchOptions()
    path = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--delay" and i + 1 < len(argv):
            opts.delay = float(argv[i + 1])
            i += 2
        elif arg.startswith("--delay="):
            opts.delay = float(arg.split("=", 1)[1])
            i += 1
        elif arg == "--workers" and i + 1 < len(argv):
            opts.workers = int(argv[i + 1])
            i += 2
        elif arg.startswith("--workers="):
            opts.workers = int(arg.split("=", 1)[1])
            i += 1
        elif arg == "--max" and i + 1 < len(argv):
            opts.max_leads = int(argv[i + 1])
            i += 2
        elif arg.startswith("--max="):
            opts.max_leads = int(arg.split("=", 1)[1])
            i += 1
        elif arg == "--workspace" and i + 1 < len(argv):
            opts.workspace = argv[i + 1]
            i += 2
        elif arg.startswith("--workspace="):
            opts.workspace = arg.split("=", 1)[1]
            i += 1
        elif arg == "--provider" and i + 1 < len(argv):
            opts.provider = argv[i + 1]
            i += 2
        elif arg.startswith("--provider="):
            opts.provider = arg.split("=", 1)[1]
            i += 1
        elif arg == "--output-base" and i + 1 < len(argv):
            opts.output_base = argv[i + 1]
            i += 2
        elif arg.startswith("--output-base="):
            opts.output_base = arg.split("=", 1)[1]
            i += 1
        elif arg == "--output-csv" and i + 1 < len(argv):
            opts.output_csv = argv[i + 1]
            i += 2
        elif arg.startswith("--output-csv="):
            opts.output_csv = arg.split("=", 1)[1]
            i += 1
        elif arg in ("--no-save",):
            opts.no_save = True
            i += 1
        elif arg in ("--skip-om",):
            opts.skip_om = True
            i += 1
        elif arg in ("--dry-run",):
            opts.dry_run = True
            i += 1
        elif arg in ("--yes",):
            opts.yes = True
            i += 1
        elif not arg.startswith("-") and not path:
            path = arg
            i += 1
        else:
            i += 1
    return opts, path


def cmd_batch_find(path: str, opts: BatchOptions) -> None:
    cfg = load_config()
    if opts.max_leads == 500:
        opts.max_leads = int(cfg.get("max_people_per_run", 500))
    om_dir = None if opts.skip_om else find_outreachmagic(cfg)
    if not opts.skip_om and not om_dir and not opts.dry_run:
        print_om_setup_box()
        print(json.dumps({"error": "outreachmagic not found — use --skip-om or install"}))
        sys.exit(1)
    try:
        out = run_batch(
            path,
            cfg,
            om_dir,
            opts,
            skill_dir=_find_skill_dir(),
            normalize_linkedin_fn=normalize_linkedin,
            key_status_fn=cc.outreachmagic_agent_key_status,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    if out.get("error"):
        print(json.dumps(out, indent=2))
        sys.exit(1)
    print(json.dumps(out, indent=2))


CSV_COLUMNS = (
    "name", "company", "title", "company_domain", "linkedin",
    "found_email", "validity", "validSMTP", "job_id", "status", "error",
)


def _result_to_csv_row(
    name: str,
    company: str,
    domain: str,
    linkedin: str,
    result: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, str]:
    return {
        "name": name,
        "company": company,
        "title": (row.get("title") or row.get("job_title") or "").strip(),
        "company_domain": domain,
        "linkedin": linkedin,
        "found_email": str(result.get("email") or ""),
        "validity": str(result.get("validity") or ""),
        "validSMTP": str(result.get("validSMTP") or ""),
        "job_id": str(result.get("jobId") or ""),
        "status": str(result.get("status") or ""),
        "error": str(result.get("error") or ""),
    }


def write_results_csv(path: str, rows: list[dict[str, str]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def cmd_prepare_import(csv_path: str, workspace: str = "", output_path: str = "") -> None:
    path = Path(csv_path)
    if not path.is_file():
        print(json.dumps({"error": f"File not found: {csv_path}"}))
        sys.exit(1)
    profiles: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("name") or "").strip()
            domain = (row.get("company_domain") or row.get("domain") or "").strip()
            company = (row.get("company") or domain).strip()
            linkedin = (row.get("linkedin") or row.get("linkedin_url") or "").strip()
            email = (row.get("found_email") or row.get("email") or "").strip()
            validity = (row.get("validity") or "").strip()
            find_result: dict[str, Any] = {"validity": validity}
            if email:
                find_result["email"] = email
            if not name or not domain:
                continue
            profile = build_import_profile(
                full_name=name,
                company=company,
                domain=domain,
                linkedin=linkedin,
                find_result=find_result,
                normalize_linkedin_fn=normalize_linkedin,
            )
            if workspace:
                profile["workspace"] = workspace
            profiles.append(profile)
    payload = {"profiles": profiles, "workspace": workspace or None}
    if output_path:
        Path(output_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"status": "ok", "count": len(profiles), "output": output_path}))
    else:
        print(json.dumps(payload, indent=2))


def cmd_import_to_om(file_path: str, workspace: str = "") -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print_om_setup_box()
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)
    raw = Path(file_path).read_text(encoding="utf-8")
    data = json.loads(raw)
    profiles = data.get("profiles") if isinstance(data, dict) else data
    if not isinstance(profiles, list):
        print(json.dumps({"error": "JSON must be {profiles: [...]} or an array"}))
        sys.exit(1)
    ws = workspace or (data.get("workspace") if isinstance(data, dict) else "") or ""
    result = batch_import_results(
        om_dir,
        [{k: v for k, v in p.items() if not str(k).startswith("_verify")} for p in profiles],
        workspace=ws,
        source_detail="email-finder/import-to-om",
    )
    print(json.dumps({"status": "ok", **result}, indent=2))


# ── Back-compat aliases for tests ───────────────────────────────────────────

_normalize_linkedin = normalize_linkedin
_row_fields = row_fields
_load_people_json = load_people_json
_validity_note_text = lambda validity, found=True: provider_note_text("trykitt", validity, found=found)
_should_tag_provider_attempt = should_tag_provider_attempt
_icypeas_poll_result = icypeas_poll_result
_enabled_providers = resolve_provider_names
_cfg_bool = lambda cfg, key, default=False: bool(cfg.get(key, default))
_split_name = lambda full_name: __import__("providers", fromlist=["split_name"]).split_name(full_name)


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "email-finder-updater", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def cmd_update(*, check_only: bool = False, explicit_tag: str = "") -> None:
    current = _current_skill_version()
    target_tag = _normalize_tag(explicit_tag) if explicit_tag else _normalize_tag(_fetch_latest_tag())
    manifest = json.loads(
        _fetch_url(f"{RAW_BASE}/{GITHUB_REPO}/{target_tag}/update-manifest.json").decode("utf-8")
    )
    target_version = manifest.get("version", target_tag.lstrip("v"))
    if check_only:
        print(json.dumps({
            "current_version": current,
            "latest_version": target_version,
            "tag": target_tag,
            "update_available": _parse_version_tuple(current) != _parse_version_tuple(target_version),
        }, indent=2))
        return
    skill_dir = _find_skill_dir()
    manifest_files = manifest.get("files") or {}
    updated: list[str] = []
    for rel_path in UPDATE_FILES:
        expected = manifest_files.get(rel_path)
        if not expected:
            raise RuntimeError(f"Manifest missing checksum for {rel_path}")
        content = _fetch_url(f"{RAW_BASE}/{GITHUB_REPO}/{target_tag}/{rel_path}")
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError(f"Checksum mismatch for {rel_path}")
        dest = skill_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        updated.append(rel_path)
    print(json.dumps({
        "status": "updated",
        "from_version": current,
        "to_version": target_version,
        "tag": target_tag,
        "files": updated,
    }, indent=2))


def _current_skill_version() -> str:
    text = (_find_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*([^\s]+)\s*$", text, flags=re.M)
    return m.group(1).strip() if m else "unknown"


def _normalize_tag(tag: str) -> str:
    t = tag.strip()
    return t if t.startswith("v") else f"v{t}"


def _parse_version_tuple(version: str) -> Optional[tuple[int, ...]]:
    raw = (version or "").strip()
    if raw.startswith("v"):
        raw = raw[1:]
    if not re.fullmatch(r"\d+(\.\d+)*", raw):
        return None
    return tuple(int(p) for p in raw.split("."))


def _fetch_latest_tag() -> str:
    payload = json.loads(_fetch_url(GITHUB_RELEASES_LATEST).decode("utf-8"))
    tag = str(payload.get("tag_name", "")).strip()
    if not tag:
        raise RuntimeError("Latest release missing tag_name")
    return _normalize_tag(tag)


def _parse_find_args(argv: list[str]) -> tuple[str, str, str, str, str, bool, list[str]]:
    name = domain = linkedin = workspace = company = ""
    save = False
    remaining: list[str] = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--save":
            save = True
            continue
        if arg == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--name="):
            name = arg.split("=", 1)[1]
            continue
        if arg == "--domain" and i + 1 < len(argv):
            domain = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--domain="):
            domain = arg.split("=", 1)[1]
            continue
        if arg == "--company" and i + 1 < len(argv):
            company = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--company="):
            company = arg.split("=", 1)[1]
            continue
        if arg == "--linkedin" and i + 1 < len(argv):
            linkedin = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--linkedin="):
            linkedin = arg.split("=", 1)[1]
            continue
        if arg == "--workspace" and i + 1 < len(argv):
            workspace = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--workspace="):
            workspace = arg.split("=", 1)[1]
            continue
        remaining.append(arg)
    return name, domain, linkedin, workspace, company, save, remaining


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    try:
        if cmd == "config":
            cmd_config()
        elif cmd == "check":
            if len(sys.argv) < 4:
                print('Usage: email_finder.py check [--workspace W] "Name" "Company"')
                sys.exit(1)
            ws = ""
            args = sys.argv[2:]
            if args[0] == "--workspace" and len(args) >= 4:
                ws = args[1]
                args = args[2:]
            cmd_check(args[0], args[1] if len(args) > 1 else "", ws)
        elif cmd == "find":
            name, domain, linkedin, workspace, company, save, _ = _parse_find_args(sys.argv[2:])
            if not name or not domain:
                print("Usage: email_finder.py find --name X --domain Y [--linkedin URL] [--save] [--workspace W]")
                sys.exit(1)
            cmd_find(name, domain, linkedin, workspace, save, company)
        elif cmd in ("batch-find", "parallel-find"):
            opts, path = _parse_batch_args(sys.argv[2:])
            if cmd == "parallel-find" and opts.workers == 1:
                opts.workers = 3
            if not path:
                print("Usage: email_finder.py batch-find [options] input.json")
                sys.exit(1)
            cmd_batch_find(path, opts)
        elif cmd == "prepare-import":
            csv_path = workspace = output_path = ""
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--csv" and i + 1 < len(args):
                    csv_path = args[i + 1]
                    i += 2
                elif args[i].startswith("--csv="):
                    csv_path = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--output" and i + 1 < len(args):
                    output_path = args[i + 1]
                    i += 2
                elif args[i].startswith("--output="):
                    output_path = args[i].split("=", 1)[1]
                    i += 1
                else:
                    i += 1
            if not csv_path:
                print("Usage: email_finder.py prepare-import --csv PATH [--workspace W]")
                sys.exit(1)
            cmd_prepare_import(csv_path, workspace, output_path)
        elif cmd == "import-to-om":
            file_path = workspace = ""
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--file" and i + 1 < len(args):
                    file_path = args[i + 1]
                    i += 2
                elif args[i].startswith("--file="):
                    file_path = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                elif not file_path and not args[i].startswith("-"):
                    file_path = args[i]
                    i += 1
                else:
                    i += 1
            if not file_path:
                print("Usage: email_finder.py import-to-om --file PATH [--workspace W]")
                sys.exit(1)
            cmd_import_to_om(file_path, workspace)
        elif cmd == "update":
            check_only = "--check" in sys.argv
            tag = ""
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--tag" and i + 1 < len(args):
                    tag = args[i + 1]
                    i += 2
                elif args[i].startswith("--tag="):
                    tag = args[i].split("=", 1)[1]
                    i += 1
                else:
                    i += 1
            cmd_update(check_only=check_only, explicit_tag=tag)
        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
            sys.exit(1)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
