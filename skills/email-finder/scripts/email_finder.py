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
    email_finder.py import-to-om --file PATH --workspace W [--source trykitt|icypeas]
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
import subprocess
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
    load_profiles_for_om_import,
    run_batch,
    should_tag_provider_attempt,
)
from normalize import load_people_json, normalize_linkedin, row_fields, validate_domain
from millionverifier import MillionVerifierProvider, mv_to_om_status
from progress import print_mv_summary, print_om_setup_box
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
    "scripts/millionverifier.py",
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
    mv_key = os.environ.get("MILLIONVERIFIER_API_KEY", "").strip()
    if mv_key:
        cfg["millionverifier_api_key"] = mv_key
    if os.environ.get("OUTREACHMAGIC_HOME"):
        cfg["outreachmagic_home"] = os.environ["OUTREACHMAGIC_HOME"]
    cfg.setdefault("trykitt_enabled", True)
    cfg.setdefault("icypeas_enabled", True)
    cfg.setdefault("icypeas_poll_attempts", 30)
    cfg.setdefault("icypeas_poll_delay_seconds", 3)
    cfg.setdefault("icypeas_request_delay_seconds", 1.5)
    cfg.setdefault("trykitt_request_delay_seconds", 0.2)
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
    source: str = "",
    source_detail: str = "email-finder/batch",
) -> dict[str, Any]:
    if not profiles:
        return {"imported": 0, "profiles": []}
    if workspace:
        imported = cc.save_email_find_profiles(
            om_dir,
            profiles,
            workspace=workspace,
            source=source,
            source_detail=source_detail,
            skill_dir=_find_skill_dir(),
        )
    else:
        imported = cc.run_import_profiles(
            om_dir,
            profiles,
            source=source,
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
    lead_id: Optional[int] = None,
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
        lead_id=lead_id,
    )
    provider = str(find_result.get("provider") or "trykitt")
    imported = batch_import_results(
        om_dir,
        [profile],
        workspace=workspace,
        source=provider,
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
        source=provider,
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
    existing: dict[str, Any] = {}
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
    existing_lead_id = None
    if om_dir and existing.get("lead_id"):
        existing_lead_id = int(existing["lead_id"])
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
                lead_id=existing_lead_id,
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
                    lead_id=existing_lead_id,
                )
                tag_result = batch_import_results(
                    om_dir,
                    [{k: v for k, v in profile.items() if not str(k).startswith("_verify")}],
                    workspace=workspace,
                    source=str(taggable[-1].get("provider") or "trykitt"),
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


def cmd_import_to_om(file_path: str, workspace: str = "", source: str = "") -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print_om_setup_box()
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)
    try:
        profiles, embedded_ws = load_profiles_for_om_import(
            file_path,
            normalize_linkedin_fn=normalize_linkedin,
        )
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    ws = (workspace or embedded_ws or "").strip()
    if not ws:
        print(json.dumps({"error": "--workspace required for import-to-om"}))
        sys.exit(1)
    if not profiles:
        print(json.dumps({"error": "no importable rows in file"}))
        sys.exit(1)
    batch_source = (source or "").strip()
    result = batch_import_results(
        om_dir,
        profiles,
        workspace=ws,
        source=batch_source,
        source_detail="email-finder/import-to-om",
    )
    print(json.dumps({"status": "ok", **result}, indent=2))


def _mv_provider(cfg: dict[str, Any]) -> MillionVerifierProvider:
    return MillionVerifierProvider(str(cfg.get("millionverifier_api_key") or ""))


def cmd_verify(email: str, workspace: str = "") -> None:
    cfg = load_config()
    mv = _mv_provider(cfg)
    result = mv.verify_single(email)
    if result.get("status") in ("error", "http_error", "no_key"):
        print(json.dumps(result, indent=2))
        sys.exit(1)
    om_dir = find_outreachmagic(cfg)
    if om_dir and workspace:
        try:
            cc.run_verify_email_batch(
                om_dir,
                [{
                    "email": email,
                    "status": result.get("status"),
                    "source": "millionverifier",
                    "source_detail": "email-finder/verify",
                }],
                skill_dir=_find_skill_dir(),
            )
            result["saved_to_om"] = True
        except RuntimeError as e:
            result["save_error"] = str(e)
    print(json.dumps(result, indent=2))


def cmd_verify_bulk(
    *,
    workspace: str = "",
    file_path: str = "",
    output_path: str = "",
    poll: bool = False,
    max_age_days: int = 30,
    skip_mv_days: int = 7,
) -> None:
    cfg = load_config()
    mv = _mv_provider(cfg)
    emails: list[str] = []
    candidate_meta: dict[str, Any] = {}
    if file_path:
        with Path(file_path).open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                em = (row.get("email") or row.get("found_email") or "").strip()
                if em:
                    emails.append(em)
    elif workspace:
        om_dir = find_outreachmagic(cfg)
        if not om_dir:
            print(json.dumps({"error": "outreachmagic not found for --workspace"}))
            sys.exit(1)
        try:
            candidate_meta = cc.run_verification_candidates(
                om_dir,
                workspace,
                max_age_days=max_age_days,
                skip_mv_days=skip_mv_days,
                skill_dir=_find_skill_dir(),
            )
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if candidate_meta.get("status") == "error":
            print(json.dumps(candidate_meta))
            sys.exit(1)
        for lead in candidate_meta.get("leads") or []:
            em = (lead.get("email") or "").strip()
            if em:
                emails.append(em)
    else:
        print(json.dumps({"error": "provide --file or --workspace"}))
        sys.exit(1)
    emails = list(dict.fromkeys(emails))
    if not emails:
        print(json.dumps({"error": "no emails to verify"}))
        sys.exit(1)
    created = mv.create_bulk(emails)
    file_id = str(created.get("file_id") or "")
    if not file_id:
        print(json.dumps({"error": "bulk submit failed", "response": created}))
        sys.exit(1)
    if output_path:
        Path(output_path).write_text(file_id + "\n", encoding="utf-8")
    out: dict[str, Any] = {
        "status": "submitted",
        "file_id": file_id,
        "total_emails": len(emails),
        "output": output_path or None,
        "candidates": candidate_meta.get("count") if candidate_meta else None,
    }
    if poll:
        status = mv.poll_until_complete(file_id)
        out["poll_status"] = status
        if str(status.get("status")).lower() == "completed":
            rows = mv.download_results(file_id)
            out["results_count"] = len(rows)
            om_dir = find_outreachmagic(cfg)
            if om_dir and workspace:
                verify_items = [
                    {
                        "email": (r.get("email") or "").strip(),
                        "status": mv_to_om_status(str(r.get("status") or "")),
                        "source": "millionverifier",
                        "source_detail": "email-finder/verify-bulk",
                    }
                    for r in rows
                    if (r.get("email") or "").strip()
                ]
                vout = cc.run_verify_email_batch(om_dir, verify_items, skill_dir=_find_skill_dir())
                out["verify"] = vout
    print(json.dumps(out, indent=2))


def cmd_verify_status(file_id: str) -> None:
    cfg = load_config()
    print(json.dumps(_mv_provider(cfg).check_status(file_id), indent=2))


def cmd_verify_list() -> None:
    cfg = load_config()
    files = _mv_provider(cfg).list_files()
    print(json.dumps({"files": files, "count": len(files)}, indent=2))


def cmd_verify_download(file_id: str, workspace: str = "") -> None:
    cfg = load_config()
    mv = _mv_provider(cfg)
    rows = mv.download_results(file_id)
    verify_items = [
        {
            "email": (r.get("email") or "").strip(),
            "status": mv_to_om_status(str(r.get("status") or r.get("result") or "")),
            "source": "millionverifier",
            "source_detail": "email-finder/verify-download",
        }
        for r in rows
        if (r.get("email") or "").strip()
    ]
    saved = 0
    om_dir = find_outreachmagic(cfg)
    if om_dir and workspace:
        vout = cc.run_verify_email_batch(om_dir, verify_items, skill_dir=_find_skill_dir())
        saved = int(vout.get("recorded") or 0)
    stats = {"downloaded": len(rows), "saved_to_om": saved}
    for st in ("valid", "catch_all", "invalid", "unknown"):
        stats[st] = sum(1 for v in verify_items if v["status"] == st)
    print_mv_summary(stats, title="MILLIONVERIFIER — VERIFICATION COMPLETE")
    print(json.dumps({"file_id": file_id, "stats": stats}, indent=2))


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
        elif cmd == "batch-find":
            opts, path = _parse_batch_args(sys.argv[2:])
            if not path:
                print("Usage: email_finder.py batch-find [options] input.json")
                sys.exit(1)
            cmd_batch_find(path, opts)
        elif cmd == "import-to-om":
            file_path = workspace = source = ""
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
                elif args[i] == "--source" and i + 1 < len(args):
                    source = args[i + 1]
                    i += 2
                elif args[i].startswith("--source="):
                    source = args[i].split("=", 1)[1]
                    i += 1
                elif not file_path and not args[i].startswith("-"):
                    file_path = args[i]
                    i += 1
                else:
                    i += 1
            if not file_path:
                print(
                    "Usage: email_finder.py import-to-om --file PATH --workspace W "
                    "[--source trykitt|icypeas]"
                )
                sys.exit(1)
            cmd_import_to_om(file_path, workspace, source)
        elif cmd == "verify":
            email = workspace = ""
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--email" and i + 1 < len(args):
                    email = args[i + 1]
                    i += 2
                elif args[i].startswith("--email="):
                    email = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                else:
                    i += 1
            if not email:
                print("Usage: email_finder.py verify --email ADDR [--workspace W]")
                sys.exit(1)
            cmd_verify(email, workspace)
        elif cmd == "verify-bulk":
            workspace = file_path = output_path = ""
            poll = "--poll" in sys.argv
            max_age = 30
            skip_mv = 7
            args = [a for a in sys.argv[2:] if a != "--poll"]
            i = 0
            while i < len(args):
                if args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--file" and i + 1 < len(args):
                    file_path = args[i + 1]
                    i += 2
                elif args[i].startswith("--file="):
                    file_path = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--output" and i + 1 < len(args):
                    output_path = args[i + 1]
                    i += 2
                elif args[i].startswith("--output="):
                    output_path = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--max-age" and i + 1 < len(args):
                    max_age = int(args[i + 1])
                    i += 2
                elif args[i].startswith("--max-age="):
                    max_age = int(args[i].split("=", 1)[1])
                    i += 1
                elif args[i] == "--skip-mv-days" and i + 1 < len(args):
                    skip_mv = int(args[i + 1])
                    i += 2
                elif args[i].startswith("--skip-mv-days="):
                    skip_mv = int(args[i].split("=", 1)[1])
                    i += 1
                else:
                    i += 1
            cmd_verify_bulk(
                workspace=workspace,
                file_path=file_path,
                output_path=output_path,
                poll=poll,
                max_age_days=max_age,
                skip_mv_days=skip_mv,
            )
        elif cmd == "verify-status":
            file_id = ""
            args = sys.argv[2:]
            if args and args[0] == "--file-id" and len(args) > 1:
                file_id = args[1]
            elif args and args[0].startswith("--file-id="):
                file_id = args[0].split("=", 1)[1]
            if not file_id:
                print("Usage: email_finder.py verify-status --file-id ID")
                sys.exit(1)
            cmd_verify_status(file_id)
        elif cmd == "verify-list":
            cmd_verify_list()
        elif cmd == "verify-download":
            file_id = workspace = ""
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--file-id" and i + 1 < len(args):
                    file_id = args[i + 1]
                    i += 2
                elif args[i].startswith("--file-id="):
                    file_id = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                else:
                    i += 1
            if not file_id or not workspace:
                print("Usage: email_finder.py verify-download --file-id ID --workspace W")
                sys.exit(1)
            cmd_verify_download(file_id, workspace)
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
