#!/usr/bin/env python3
"""
Email Finder — trykitt.ai + Icypeas for Hermes / Cursor / Claude Code.

Checks outreachmagic before spending provider credits. Batch mode uses incremental
CSV/JSON saves, bulk dedup (pipeline batch-lead-lookup), and bulk verify-email.

Usage:
    email_finder.py config
    email_finder.py check [--workspace W] "Name" "Company"
    email_finder.py find --name X --domain Y [--linkedin URL] [--dry-run] [--no-save] [--workspace W]
    email_finder.py batch-find [options] input.json
    email_finder.py import-to-om --file PATH --workspace W [--source trykitt|icypeas]
    email_finder.py update                    # Deprecated — use `pipeline.py update` instead

batch-find options:
    --workspace W --delay 8 --workers 1 --max 500 --provider trykitt|icypeas
    --abandon-after N   stop calling a domain after N consecutive misses (default 3, 0=off)
    --output-base PATH --output-csv PATH --no-save --skip-om --dry-run --yes

MillionVerifier (bulk email verification):
    email_finder.py verify EMAIL
    email_finder.py verify-bulk [--workspace W | --file emails.csv] [--poll] [--output PATH]
                                 [--dry-run] [--force] [--max-age N] [--skip-mv-days N]
    email_finder.py verify-status --file-id ID
    email_finder.py verify-list
    email_finder.py verify-download --file-id ID [--workspace W]
    email_finder.py verify-credits

Scrubby (deep verification):
    email_finder.py scrubby-deep-submit [--workspace W | --file emails.csv] [--dry-run] [--force]
    email_finder.py scrubby-deep-fetch IDENTIFIER [--workspace W] [--poll]
    email_finder.py scrubby-deep-status IDENTIFIER
    email_finder.py scrubby-deep-list
    email_finder.py scrubby-deep-credits
    email_finder.py verify-with-scrubby ...
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Prefer this skill's scripts/ over Hermes /opt/hermes (may contain other batch_runner.py).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import shared as cc
from batch_runner import (
    BatchOptions,
    build_import_profile,
    collect_import_profiles,
    load_profiles_for_om_import,
    run_batch,
    should_tag_provider_attempt,
)
from normalize import load_people_json, normalize_linkedin, row_fields, validate_domain
from credits import mv_credit_summary, verify_credits_used
from millionverifier import MillionVerifierProvider, mv_to_om_status
from scrubby import ScrubbyProvider, scrubby_deep_to_om_status
from progress import print_mv_summary, print_om_setup_box, print_verify_bulk_plan
from trykitt import trykitt_find
from icypeas import icypeas_find, icypeas_poll_result
from waterfall import (
    provider_note_text,
    resolve_provider_names,
    run_find_with_fallback,
    validity_to_verify_status,
)

SKILL_NAME = "outreachmagic"


def _find_skill_dir() -> Path:
    return cc.skill_dir_from_script(__file__)


def ensure_env_loaded() -> None:
    cc.ensure_agent_env_loaded(_find_skill_dir())


def load_config() -> dict[str, Any]:
    ensure_env_loaded()
    skill_dir = _find_skill_dir()
    cfg: dict[str, Any] = {}
    cfg_path = skill_dir / "config" / "outreachmagic_config.json"
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
    scrubby_key = os.environ.get("SCRUBBY_API_KEY", "").strip()
    if scrubby_key:
        cfg["scrubby_api_key"] = scrubby_key
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
    mv_key = cfg.get("millionverifier_api_key") or ""
    out: dict[str, Any] = {
        "skill": SKILL_NAME,
        "trykitt_api_key_set": bool(key),
        "trykitt_api_key_preview": _mask_key(key) if key else None,
        "trykitt_api_key_source": cc.companion_api_key_source("TRYKITT_API_KEY", _find_skill_dir()),
        "icypeas_api_key_set": bool(icypeas_key),
        "icypeas_api_key_preview": _mask_key(icypeas_key) if icypeas_key else None,
        "icypeas_api_key_source": cc.companion_api_key_source("ICYPEAS_API_KEY", _find_skill_dir()),
        "millionverifier_api_key_set": bool(mv_key),
        "millionverifier_api_key_preview": _mask_key(mv_key) if mv_key else None,
        "millionverifier_api_key_source": cc.companion_api_key_source(
            "MILLIONVERIFIER_API_KEY", _find_skill_dir(),
        ),
        "outreachmagic_found": om_dir is not None,
        "outreachmagic_home": str(om_dir) if om_dir else None,
        "max_per_run": cfg.get("max_people_per_run", 500),
    }
    if om_dir:
        has_key, source = cc.outreachmagic_agent_key_status(om_dir)
        out["outreachmagic_agent_key"] = {"set": has_key, "source": source}
    print(json.dumps(out, indent=2))
    cc.warn_non_portal_key_sources(_find_skill_dir())


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
    """Import (or match) a lead with no found email, then record the miss.

    Unlike build_import_profile()'s _provider_attempts (consumed by
    apply-email-find-results, which needs a known lead_id up front), this
    path goes through import-profiles -- the lead_id isn't known until AFTER
    import resolves it, so the provider-attempt write happens as a separate
    client-side bulk call once the resolved lead_id(s) come back.
    """
    profile = build_import_profile(
        full_name=full_name,
        company=company,
        domain=domain,
        linkedin=linkedin,
        find_result={"provider": provider, "status": "not_found"},
        normalize_linkedin_fn=normalize_linkedin,
    )
    clean_profile = {
        k: v for k, v in profile.items()
        if not str(k).startswith("_verify") and k != "_provider_attempts"
    }
    imported = batch_import_results(
        om_dir,
        [clean_profile],
        workspace=workspace,
        source=provider,
        source_detail=f"email-finder/{provider}-miss",
    )
    out: dict[str, Any] = {"tagged": True, "import": imported.get("import", {})}
    if not workspace:
        out["warning"] = "tags require --workspace on import-profiles"
    lead_ids = [
        int(r["id"]) for r in (imported.get("import", {}).get("results") or [])
        if isinstance(r, dict) and r.get("id")
    ]
    if lead_ids:
        try:
            out["provider_attempt"] = cc.run_provider_attempt_bulk(
                om_dir, lead_ids, provider, status="not_found", skill_dir=_find_skill_dir(),
            )
        except RuntimeError as exc:
            out["provider_attempt"] = {"status": "error", "error": str(exc)}
    return out


def cmd_find(
    name: str,
    domain: str,
    linkedin: str = "",
    workspace: str = "",
    save: bool = True,
    company: str = "",
    dry_run: bool = False,
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
    if not om_dir and save and not dry_run:
        cc.print_om_setup_box()
        print(
            "⚠️  Outreach Magic is not connected.\n"
            "   The email provider API will run, but results will NOT be saved.\n"
            "   Install Outreach Magic to save results and avoid wasting credits on\n"
            "   leads that may already have an email in your database.\n"
            "   Install: https://github.com/outreachmagic/outreachmagic\n"
            "   Or run: pipeline.py update (if already installed)\n",
            file=sys.stderr,
        )
    elif not om_dir and not dry_run:
        print(
            "⚠️  Outreach Magic is not connected. Results will NOT be saved.\n"
            "   Install from: https://github.com/outreachmagic/outreachmagic\n",
            file=sys.stderr,
        )
    result = run_find_with_fallback(cfg, full_name=name, domain=domain, linkedin=linkedin)
    existing_lead_id = None
    if om_dir and existing.get("lead_id"):
        existing_lead_id = int(existing["lead_id"])
    if om_dir and save and not dry_run:
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
        elif arg == "--abandon-after" and i + 1 < len(argv):
            opts.abandon_after = int(argv[i + 1])
            i += 2
            continue
        elif arg.startswith("--abandon-after="):
            opts.abandon_after = int(arg.split("=", 1)[1])
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
        elif arg in ("--retry-errors",):
            opts.retry_errors = True
            i += 1
        elif not arg.startswith("-") and not path:
            path = arg
            i += 1
        else:
            i += 1
    return opts, path


def _crash_log_path(path: str, opts: BatchOptions) -> str:
    """Sidecar log next to the checkpoint output (or the input file, if no
    checkpoint was ever configured) -- so a process that disappears (OOM,
    SIGTERM, an unhandled exception) leaves a trail instead of no error
    message at all."""
    base = opts.output_base or path
    return f"{base}.crash.log"


def _write_crash_log(path: str, opts: BatchOptions, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(_crash_log_path(path, opts), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass  # best-effort -- crash logging must never itself crash the process


def cmd_batch_find(path: str, opts: BatchOptions) -> None:
    cfg = load_config()
    if opts.max_leads == 500:
        opts.max_leads = int(cfg.get("max_people_per_run", 500))
    om_dir = None if opts.skip_om else find_outreachmagic(cfg)
    if not opts.skip_om and not om_dir and not opts.dry_run:
        print_om_setup_box()
        print(json.dumps({"error": "outreachmagic not found — use --skip-om or install"}))
        sys.exit(1)

    def _on_terminate(signum, _frame):
        _write_crash_log(
            path, opts, f"Terminated by signal {signum} ({signal.Signals(signum).name})",
        )
        sys.exit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _on_terminate)
    except (ValueError, OSError):
        pass  # not running on the main thread, or platform doesn't support it

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
    except Exception as exc:
        _write_crash_log(
            path, opts, f"Crashed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
        raise
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


def _mv_verify_single(email: str, cfg: dict[str, Any]) -> dict[str, Any]:
    _, _, call_with_key_pool_results = cc.require_api_key_pool()
    return call_with_key_pool_results(
        "MILLIONVERIFIER_API_KEY",
        lambda key: MillionVerifierProvider(key).verify_single(email),
        provider="millionverifier",
    )


def cmd_verify(email: str, workspace: str = "") -> None:
    cfg = load_config()
    result = _mv_verify_single(email, cfg)
    if result.get("status") in ("error", "http_error", "no_key"):
        print(json.dumps(result, indent=2))
        sys.exit(1)
    om_dir = find_outreachmagic(cfg)
    if om_dir and workspace:
        em = (email or "").strip().lower()
        lead_id = _lead_id_map_for_emails(om_dir, workspace, [em]).get(em)
        if not lead_id:
            result["saved_to_om"] = False
            result["save_error"] = "no matching lead found for this email in workspace"
        else:
            try:
                vout = cc.run_verify_email_batch(
                    om_dir,
                    [{
                        "lead_id": lead_id,
                        "email": email,
                        "status": result.get("status"),
                        "source": "millionverifier",
                        "source_detail": "email-finder/verify",
                    }],
                    skill_dir=_find_skill_dir(),
                )
                result["saved_to_om"] = bool(vout.get("recorded"))
                if not vout.get("recorded"):
                    result["save_error"] = vout.get("errors") or "not recorded"
            except RuntimeError as e:
                result["saved_to_om"] = False
                result["save_error"] = str(e)
    print(json.dumps(result, indent=2))


def _tag_mv_attempted(
    om_dir: Optional[Path],
    workspace: str,
    lead_ids: list[int],
) -> dict[str, Any]:
    if not om_dir or not workspace or not lead_ids:
        return {"status": "skipped", "tagged": 0}
    try:
        return cc.run_provider_attempt_bulk(
            om_dir,
            list(dict.fromkeys(lead_ids)),
            "millionverifier",
            status="unknown",
            skill_dir=_find_skill_dir(),
        )
    except RuntimeError as exc:
        return {"status": "error", "error": str(exc)}


def _lead_id_map_for_emails(om_dir: Path, workspace: str, emails: list[str]) -> dict[str, int]:
    """Resolve email -> lead_id with one batched pipeline.py batch-lead-lookup call
    (run_batch_lead_lookup already chunks internally for large lists — no need to
    spawn one subprocess per email)."""
    items = [
        {"index": i, "email": (e or "").strip().lower()}
        for i, e in enumerate(emails)
        if (e or "").strip()
    ]
    if not items:
        return {}
    try:
        payload = cc.run_batch_lead_lookup(
            om_dir, items, workspace=workspace, skill_dir=_find_skill_dir(),
        )
    except RuntimeError:
        return {}
    out: dict[str, int] = {}
    for row in payload.get("results") or []:
        if row.get("status") == "found" and row.get("lead_id"):
            email = (row.get("email") or "").strip().lower()
            if email:
                out[email] = int(row["lead_id"])
    return out


def _lead_ids_for_emails(om_dir: Path, workspace: str, emails: list[str]) -> list[int]:
    return list(_lead_id_map_for_emails(om_dir, workspace, emails).values())


def cmd_verify_credits() -> None:
    cfg = load_config()
    mv = _mv_provider(cfg)
    if not str(cfg.get("millionverifier_api_key") or "").strip():
        print(json.dumps({"error": "MILLIONVERIFIER_API_KEY not set"}))
        sys.exit(1)
    remaining, err = mv.check_credits()
    print(
        json.dumps(
            {
                "credits_remaining": int(remaining),
                "credits_per_email_verified": verify_credits_used(count=1),
                "error": err,
                "credit_model": "1 credit per email verified",
            },
            indent=2,
        )
    )


def _collect_verify_emails(
    *,
    workspace: str = "",
    file_path: str = "",
    max_age_days: int = 30,
    skip_mv_days: int = 7,
    cfg: Optional[dict[str, Any]] = None,
) -> tuple[list[str], dict[str, Any]]:
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
        om_dir = find_outreachmagic(cfg or load_config())
        if not om_dir:
            return [], {"error": "outreachmagic not found for --workspace"}
        try:
            candidate_meta = cc.run_verification_candidates(
                om_dir,
                workspace,
                max_age_days=max_age_days,
                skip_mv_days=skip_mv_days,
                skill_dir=_find_skill_dir(),
            )
        except RuntimeError as e:
            return [], {"error": str(e)}
        if candidate_meta.get("status") == "error":
            return [], candidate_meta
        for lead in candidate_meta.get("leads") or []:
            em = (lead.get("email") or "").strip()
            if em:
                emails.append(em)
    else:
        return [], {"error": "provide --file or --workspace"}
    emails = list(dict.fromkeys(emails))
    lead_ids = {
        int(lead["lead_id"])
        for lead in (candidate_meta.get("leads") or [])
        if lead.get("lead_id")
    }
    candidate_meta["unique_lead_ids"] = len(lead_ids)
    return emails, candidate_meta


def cmd_verify_bulk(
    *,
    workspace: str = "",
    file_path: str = "",
    output_path: str = "",
    poll: bool = False,
    dry_run: bool = False,
    force: bool = False,
    max_age_days: int = 30,
    skip_mv_days: int = 7,
) -> None:
    cfg = load_config()
    mv = _mv_provider(cfg)
    emails, candidate_meta = _collect_verify_emails(
        workspace=workspace,
        file_path=file_path,
        max_age_days=max_age_days,
        skip_mv_days=skip_mv_days,
        cfg=cfg,
    )
    if candidate_meta.get("error"):
        print(json.dumps({"error": candidate_meta["error"]}))
        sys.exit(1)
    if not emails:
        print(json.dumps({"error": "no emails to verify"}))
        sys.exit(1)
    if dry_run:
        remaining, err = mv.check_credits()
        plan = mv_credit_summary(
            email_count=len(emails),
            credits_remaining=remaining,
            error=err,
        )
        plan["unique_lead_ids"] = candidate_meta.get("unique_lead_ids")
        plan["status"] = "dry_run"
        plan["candidates"] = candidate_meta.get("count")
        print_verify_bulk_plan(plan)
        print(json.dumps(plan, indent=2))
        return

    om_dir = find_outreachmagic(cfg)
    item_hash = cc.hash_item_set(emails)
    if om_dir and not force:
        existing = cc.find_pending_batch_job(
            om_dir, provider="millionverifier", item_set_hash=item_hash, skill_dir=_find_skill_dir(),
        )
        if existing:
            print(json.dumps({
                "error": "duplicate batch: this exact email set was already submitted",
                "existing_job_id": existing.get("job_id"),
                "existing_status": existing.get("status"),
                "submitted_at": existing.get("submitted_at"),
                "hint": (
                    f"Use --force to resubmit, or check results with: "
                    f"verify-status --file-id {existing.get('job_id')}"
                ),
            }, indent=2))
            sys.exit(1)

    created = mv.create_bulk(emails)
    file_id = str(created.get("file_id") or "")
    if not file_id:
        print(json.dumps({"error": "bulk submit failed", "response": created}))
        sys.exit(1)
    # Surface the file_id immediately — before polling — so it's never lost
    # if --poll gets killed by an external timeout.
    print(
        f"Submitted as file_id {file_id}. If this times out, run: "
        f"verify-download --file-id {file_id}" + (f" --workspace {workspace}" if workspace else ""),
        file=sys.stderr, flush=True,
    )
    if om_dir:
        cc.record_batch_job(
            om_dir, provider="millionverifier", kind="email_verification", job_id=file_id,
            item_count=len(emails), item_set_hash=item_hash, workspace=workspace,
            skill_dir=_find_skill_dir(),
        )
    if output_path:
        Path(output_path).write_text(file_id + "\n", encoding="utf-8")
    out: dict[str, Any] = {
        "status": "submitted",
        "file_id": file_id,
        "total_emails": len(emails),
        "credits_used": verify_credits_used(count=len(emails)),
        "output": output_path or None,
        "candidates": candidate_meta.get("count") if candidate_meta else None,
    }
    if poll:
        status = mv.poll_until_complete(file_id)
        out["poll_status"] = status
        if str(status.get("status")).lower() == "in_progress":
            out["poll_status_note"] = (
                "ok/catch_all/invalid breakdown stays 0 until MillionVerifier marks "
                "the file fully complete — this is upstream API behavior, not a bug here."
            )
        if str(status.get("status")).lower() in ("completed", "finished"):
            rows = mv.download_results(file_id)
            out["results_count"] = len(rows)
            if om_dir:
                cc.mark_batch_job_status(
                    om_dir, provider="millionverifier", job_id=file_id, status="downloaded",
                    skill_dir=_find_skill_dir(),
                )
            if om_dir and workspace:
                result_emails = [
                    (r.get("email") or "").strip() for r in rows if (r.get("email") or "").strip()
                ]
                lead_id_map = _lead_id_map_for_emails(om_dir, workspace, result_emails)
                verify_items = [
                    {
                        "lead_id": lead_id_map[em.lower()],
                        "email": em,
                        "status": mv_to_om_status(str(r.get("status") or r.get("result") or "")),
                        "source": "millionverifier",
                        "source_detail": "email-finder/verify-bulk",
                    }
                    for r in rows
                    for em in [(r.get("email") or "").strip()]
                    if em and em.lower() in lead_id_map
                ]
                vout = cc.run_verify_email_batch(om_dir, verify_items, skill_dir=_find_skill_dir())
                out["verify"] = vout
                lead_ids = [
                    int(lead["lead_id"])
                    for lead in (candidate_meta.get("leads") or [])
                    if lead.get("lead_id")
                ]
                if not lead_ids and workspace:
                    lead_ids = list(lead_id_map.values()) or _lead_ids_for_emails(om_dir, workspace, emails)
                out["mv_tag"] = _tag_mv_attempted(om_dir, workspace, lead_ids)
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
    tag_result: dict[str, Any] = {"status": "skipped", "tagged": 0}
    if om_dir and workspace:
        lead_id_map = _lead_id_map_for_emails(om_dir, workspace, [v["email"] for v in verify_items])
        save_items = [
            {**item, "lead_id": lead_id_map[item["email"].lower()]}
            for item in verify_items
            if item["email"].lower() in lead_id_map
        ]
        vout = cc.run_verify_email_batch(om_dir, save_items, skill_dir=_find_skill_dir())
        saved = int(vout.get("recorded") or 0)
        tag_result = _tag_mv_attempted(om_dir, workspace, list(lead_id_map.values()))
    stats = {
        "downloaded": len(rows),
        "emails_verified": len(verify_items),
        "credits_used": verify_credits_used(count=len(verify_items)),
        "saved_to_om": saved,
        "mv_tag": tag_result,
    }
    for st in ("valid", "catch_all", "invalid", "unknown"):
        stats[st] = sum(1 for v in verify_items if v["status"] == st)
    print_mv_summary(stats, title="MILLIONVERIFIER — VERIFICATION COMPLETE")
    print(json.dumps({"file_id": file_id, "stats": stats}, indent=2))


# ── Scrubby Deep Verification ──────────────────────────────────────────────


def _scrubby_provider(cfg: dict[str, Any]) -> ScrubbyProvider:
    return ScrubbyProvider(str(cfg.get("scrubby_api_key") or ""))




def _collect_scrubby_deep_emails(
    *,
    workspace: str = "",
    file_path: str = "",
    max_age_days: int = 30,
    skip_scrubby_days: int = 7,
    filter_catch_all: bool = False,
    cfg: Optional[dict[str, Any]] = None,
) -> tuple[list[str], dict[str, Any]]:
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
        om_dir = find_outreachmagic(cfg or load_config())
        if not om_dir:
            return [], {"error": "outreachmagic not found for --workspace"}
        try:
            candidate_meta = cc.run_scrubby_deep_candidates(
                om_dir,
                workspace,
                max_age_days=max_age_days,
                skip_scrubby_days=skip_scrubby_days,
                filter_catch_all=filter_catch_all,
                skill_dir=_find_skill_dir(),
            )
        except RuntimeError as e:
            return [], {"error": str(e)}
        if candidate_meta.get("status") == "error":
            return [], candidate_meta
        for lead in candidate_meta.get("leads") or []:
            em = (lead.get("email") or "").strip()
            if em:
                emails.append(em)
    else:
        return [], {"error": "provide --file or --workspace"}
    emails = list(dict.fromkeys(emails))
    lead_ids = [
        int(lead["lead_id"])
        for lead in (candidate_meta.get("leads") or [])
        if lead.get("lead_id")
    ]
    candidate_meta["unique_lead_ids"] = len(set(lead_ids))
    return emails, candidate_meta


def cmd_scrubby_deep_submit(
    *,
    workspace: str = "",
    file_path: str = "",
    dry_run: bool = False,
    force: bool = False,
    filter_catch_all: bool = False,
) -> None:
    """Submit a batch to Scrubby Deep Verification and tag leads."""
    cfg = load_config()
    scrubby = _scrubby_provider(cfg)
    if not str(cfg.get("scrubby_api_key") or "").strip():
        print(json.dumps({"error": "SCRUBBY_API_KEY not set"}))
        sys.exit(1)
    emails, candidate_meta = _collect_scrubby_deep_emails(
        workspace=workspace,
        file_path=file_path,
        filter_catch_all=filter_catch_all,
        cfg=cfg,
    )
    if candidate_meta.get("error"):
        print(json.dumps({"error": candidate_meta["error"]}))
        sys.exit(1)
    if not emails:
        print(json.dumps({"error": "no emails to verify"}))
        sys.exit(1)
    if dry_run:
        remaining, err = scrubby.check_credits()
        plan = {
            "email_count": len(emails),
            "credits_per_email": 3,
            "credits_needed": len(emails) * 3,
            "credits_remaining": remaining,
            "error": err,
            "unique_lead_ids": candidate_meta.get("unique_lead_ids"),
            "status": "dry_run",
        }
        print(json.dumps(plan, indent=2))
        return

    om_dir = find_outreachmagic(cfg)
    item_hash = cc.hash_item_set(emails)
    skill_dir = _find_skill_dir()
    if om_dir and not force:
        existing = cc.find_pending_batch_job(
            om_dir, provider="scrubby_deep", item_set_hash=item_hash, skill_dir=skill_dir,
        )
        if existing:
            print(json.dumps({
                "error": "duplicate batch: this exact email set was already submitted",
                "existing_job_id": existing.get("job_id"),
                "existing_status": existing.get("status"),
                "submitted_at": existing.get("submitted_at"),
                "hint": (
                    f"Use --force to resubmit, or check results with: "
                    f"scrubby-deep-status {existing.get('job_id')}"
                ),
            }, indent=2))
            sys.exit(1)

    print(
        f"Submitting {len(emails)} email(s) to Scrubby Deep Verification — "
        "this validates synchronously and can take a while for large batches...",
        file=sys.stderr,
        flush=True,
    )
    result = scrubby.submit_deep(emails)
    identifier = str(result.get("identifier") or "")
    if not identifier:
        print(json.dumps({"error": "scrubby submit failed", "response": result}))
        sys.exit(1)
    if om_dir:
        cc.record_batch_job(
            om_dir, provider="scrubby_deep", kind="email_verification", job_id=identifier,
            item_count=len(emails), item_set_hash=item_hash, workspace=workspace,
            metadata={"candidates": candidate_meta.get("count"), "unique_lead_ids": candidate_meta.get("unique_lead_ids")},
            skill_dir=skill_dir,
        )
    tag_result: dict[str, Any] = {"status": "skipped", "tagged": 0}
    if om_dir and workspace:
        lead_ids = [
            int(lead["lead_id"])
            for lead in (candidate_meta.get("leads") or [])
            if lead.get("lead_id")
        ]
        if not lead_ids:
            lead_ids = _lead_ids_for_emails(om_dir, workspace, emails)
        tag_result = _tag_scrubby_deep_submitted(om_dir, workspace, lead_ids)
    out: dict[str, Any] = {
        "status": "submitted",
        "identifier": identifier,
        "total_emails": len(emails),
        "credits_required": result.get("credits_used", len(emails) * 3),
        "retry_after_seconds": result.get("retry_after_seconds"),
        "tag": tag_result,
    }
    print(json.dumps(out, indent=2))


def cmd_scrubby_deep_fetch(
    identifier: str,
    *,
    workspace: str = "",
    poll: bool = False,
) -> None:
    """Poll Scrubby for deep verification results and save to Outreach Magic."""
    cfg = load_config()
    scrubby = _scrubby_provider(cfg)
    if poll:
        raw = scrubby.poll_until_complete(identifier)
    else:
        raw = scrubby.fetch_results(identifier)
    status = str(raw.get("status") or "").lower()
    if status not in ("completed",):
        print(json.dumps({"status": "pending", "identifier": identifier, "response": raw}, indent=2))
        return
    aggregated = scrubby.aggregate_results(raw)
    verify_items: list[dict[str, Any]] = []
    for email, entry in aggregated.items():
        result = str(entry.get("result") or "")
        verify_items.append({
            "email": email.strip() if email else "",
            "status": scrubby_deep_to_om_status(result),
            "source": "scrubby_deep",
            "source_detail": "email-finder/scrubby-deep-fetch",
            "sub_status": result,
        })
    email_count = len(verify_items)
    saved = 0
    tag_result: dict[str, Any] = {"status": "skipped", "tagged": 0}
    if verify_items and workspace:
        om_dir = find_outreachmagic(cfg)
        if om_dir:
            lead_id_map = _lead_id_map_for_emails(om_dir, workspace, [v["email"] for v in verify_items])
            save_items = [
                {**item, "lead_id": lead_id_map[item["email"].lower()]}
                for item in verify_items
                if item["email"] and item["email"].lower() in lead_id_map
            ]
            vout = cc.run_verify_email_batch(om_dir, save_items, skill_dir=_find_skill_dir())
            saved = int(vout.get("recorded") or 0)
            tag_result = _tag_scrubby_deep_attempted(om_dir, workspace, list(lead_id_map.values()))
    stats: dict[str, Any] = {
        "identifier": identifier,
        "fetched": email_count,
        "credits_per_email": 3,
        "saved_to_om": saved,
        "scrubby_tag": tag_result,
    }
    for st in ("valid", "catch_all", "invalid", "unknown"):
        stats[st] = sum(1 for v in verify_items if v["status"] == st)
    print(json.dumps(stats, indent=2))


def cmd_scrubby_deep_status(identifier: str) -> None:
    """Check the current status of a Scrubby deep verification job."""
    cfg = load_config()
    result = _scrubby_provider(cfg).fetch_results(identifier)
    print(json.dumps(result, indent=2))


def cmd_scrubby_deep_list() -> None:
    """List all active/completed Scrubby deep verification jobs."""
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)
    jobs = cc.list_batch_jobs(om_dir, provider="scrubby_deep", skill_dir=_find_skill_dir())
    scrubby = _scrubby_provider(cfg)
    enriched: list[dict[str, Any]] = []
    for j in jobs:
        j["identifier"] = j.get("job_id")
        status_payload = scrubby.fetch_results(j["identifier"])
        j["current_status"] = (status_payload.get("status") or "unknown")
        enriched.append(j)
    print(json.dumps({"jobs": enriched, "count": len(enriched)}, indent=2))


def cmd_scrubby_deep_credits() -> None:
    """Check remaining Scrubby credits."""
    cfg = load_config()
    scrubby = _scrubby_provider(cfg)
    if not str(cfg.get("scrubby_api_key") or "").strip():
        print(json.dumps({"error": "SCRUBBY_API_KEY not set"}))
        sys.exit(1)
    remaining, err = scrubby.check_credits()
    print(json.dumps({
        "credits_remaining": remaining,
        "credits_per_deep_verification": 3,
        "error": err,
        "credit_model": "3 credits per deep verification email",
    }, indent=2))


def cmd_verify_with_scrubby(
    *,
    workspace: str = "",
    file_path: str = "",
    dry_run: bool = False,
    max_age_days: int = 30,
    skip_mv_days: int = 7,
) -> None:
    """Combined workflow: MV bulk verify then Scrubby Deep on catch_all/unknown."""
    cfg = load_config()
    has_mv = bool(str(cfg.get("millionverifier_api_key") or "").strip())
    has_scrubby = bool(str(cfg.get("scrubby_api_key") or "").strip())
    if not has_mv and not has_scrubby:
        print(json.dumps({"error": "No verification API keys configured (MILLIONVERIFIER_API_KEY or SCRUBBY_API_KEY)"}))
        sys.exit(1)
    if not has_mv and has_scrubby:
        # Scrubby-only: deep verify all unverified/stale emails
        cmd_scrubby_deep_submit(
            workspace=workspace,
            file_path=file_path,
            dry_run=dry_run,
            filter_catch_all=False,
        )
        return
    # MV + Scrubby: run MV first, then Scrubby on catch_all/unknown
    cmd_verify_bulk(
        workspace=workspace,
        file_path=file_path,
        poll=True,
        dry_run=dry_run,
        max_age_days=max_age_days,
        skip_mv_days=skip_mv_days,
    )
    if has_scrubby and not dry_run:
        print("\n--- Starting Scrubby Deep pass on catch_all/unknown ---\n")
        cmd_scrubby_deep_submit(
            workspace=workspace,
            file_path="",
            dry_run=False,
            filter_catch_all=True,
        )


def _tag_scrubby_deep_submitted(
    om_dir: Path,
    workspace: str,
    lead_ids: list[int],
) -> dict[str, Any]:
    if not om_dir or not workspace or not lead_ids:
        return {"status": "skipped", "tagged": 0}
    try:
        return cc.run_tag_bulk(
            om_dir,
            workspace,
            lead_ids,
            [cc.SCRUBBY_DEEP_SUBMITTED_TAG],
            skill_dir=_find_skill_dir(),
        )
    except RuntimeError:
        return {"status": "skipped", "tagged": 0}


def _tag_scrubby_deep_attempted(
    om_dir: Path,
    workspace: str,
    lead_ids: list[int],
) -> dict[str, Any]:
    if not om_dir or not workspace or not lead_ids:
        return {"status": "skipped", "tagged": 0}
    try:
        return cc.run_provider_attempt_bulk(
            om_dir, lead_ids, "scrubby", status="unknown", skill_dir=_find_skill_dir(),
        )
    except RuntimeError:
        return {"status": "skipped", "tagged": 0}


def cmd_update(*, check_only: bool = False, explicit_tag: str = "") -> None:
    print(json.dumps({
        "status": "deprecated",
        "message": (
            "email_finder.py is bundled inside the consolidated outreachmagic skill and no "
            "longer has its own release channel (the standalone email-finder repo is retired). "
            "Run `pipeline.py update` instead."
        ),
    }, indent=2))


def _parse_find_args(argv: list[str]) -> tuple[str, str, str, str, str, bool, bool, list[str]]:
    name = domain = linkedin = workspace = company = ""
    save = True
    dry_run = False
    remaining: list[str] = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--save":
            # Save is the default now; kept as a no-op for backward compatibility.
            continue
        if arg == "--no-save":
            save = False
            continue
        if arg == "--dry-run":
            dry_run = True
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
    return name, domain, linkedin, workspace, company, save, dry_run, remaining


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
            name, domain, linkedin, workspace, company, save, dry_run, _ = _parse_find_args(sys.argv[2:])
            if not name or not domain:
                print(
                    "Usage: email_finder.py find --name X --domain Y [--linkedin URL] "
                    "[--dry-run] [--no-save] [--workspace W]"
                )
                sys.exit(1)
            cmd_find(name, domain, linkedin, workspace, save, company, dry_run=dry_run)
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
            dry_run = "--dry-run" in sys.argv
            force = "--force" in sys.argv
            max_age = 30
            skip_mv = 7
            args = [a for a in sys.argv[2:] if a not in ("--poll", "--dry-run", "--force")]
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
                dry_run=dry_run,
                force=force,
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
        elif cmd == "verify-credits":
            cmd_verify_credits()
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
        elif cmd == "scrubby-deep-submit":
            workspace = file_path = ""
            dry_run = "--dry-run" in sys.argv
            force = "--force" in sys.argv
            filter_catch_all = "--filter=catch_all" in sys.argv or "--filter" in sys.argv
            args = sys.argv[2:]
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
                else:
                    i += 1
            if not workspace and not file_path:
                print("Usage: email_finder.py scrubby-deep-submit --workspace W [--dry-run] [--filter=catch_all]")
                sys.exit(1)
            cmd_scrubby_deep_submit(
                workspace=workspace,
                file_path=file_path,
                dry_run=dry_run,
                force=force,
                filter_catch_all=filter_catch_all,
            )
        elif cmd == "scrubby-deep-fetch":
            identifier = workspace = ""
            poll = "--poll" in sys.argv
            args = sys.argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--identifier" and i + 1 < len(args):
                    identifier = args[i + 1]
                    i += 2
                elif args[i].startswith("--identifier="):
                    identifier = args[i].split("=", 1)[1]
                    i += 1
                elif args[i] == "--workspace" and i + 1 < len(args):
                    workspace = args[i + 1]
                    i += 2
                elif args[i].startswith("--workspace="):
                    workspace = args[i].split("=", 1)[1]
                    i += 1
                else:
                    i += 1
            if not identifier:
                print("Usage: email_finder.py scrubby-deep-fetch --identifier ID [--workspace W] [--poll]")
                sys.exit(1)
            cmd_scrubby_deep_fetch(identifier, workspace=workspace, poll=poll)
        elif cmd == "scrubby-deep-status":
            identifier = ""
            args = sys.argv[2:]
            if args and args[0].startswith("--identifier="):
                identifier = args[0].split("=", 1)[1]
            elif len(args) >= 2 and args[0] == "--identifier":
                identifier = args[1]
            if not identifier:
                print("Usage: email_finder.py scrubby-deep-status --identifier ID")
                sys.exit(1)
            cmd_scrubby_deep_status(identifier)
        elif cmd == "scrubby-deep-list":
            cmd_scrubby_deep_list()
        elif cmd == "scrubby-deep-credits":
            cmd_scrubby_deep_credits()
        elif cmd == "verify-with-scrubby":
            workspace = file_path = ""
            dry_run = "--dry-run" in sys.argv
            args = sys.argv[2:]
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
                else:
                    i += 1
            if not workspace and not file_path:
                print("Usage: email_finder.py verify-with-scrubby --workspace W [--dry-run]")
                sys.exit(1)
            cmd_verify_with_scrubby(
                workspace=workspace,
                file_path=file_path,
                dry_run=dry_run,
            )
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
