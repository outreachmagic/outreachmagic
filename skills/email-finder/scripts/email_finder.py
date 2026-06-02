#!/usr/bin/env python3
"""
Email Finder — trykitt.ai email finding for Hermes / Cursor / Claude Code.

Checks outreachmagic before spending trykitt credits. Saves via import-profiles
and verify-email (record-only).

Usage:
    email_finder.py config
    email_finder.py check "Jane Doe" "Acme Corp"
    email_finder.py find --name "Jane Doe" --domain acme.com [--linkedin URL] [--save] [--workspace W]
    email_finder.py batch-find input.json [--delay 8] [--workspace W] [--no-save] [--output-csv PATH]
    email_finder.py parallel-find input.json [--workers 3] [--delay 3] [--output-csv PATH]
    email_finder.py prepare-import --csv PATH [--workspace W] [--output PATH]
    email_finder.py import-to-om --file PATH [--workspace W]
    email_finder.py update [--check] [--tag v1.0.0]
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import hashlib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import companion_common as cc

SKILL_NAME = "email-finder"
GITHUB_REPO = "outreachmagic/email-finder"
GITHUB_RELEASES_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_BASE = "https://raw.githubusercontent.com"
TRYKITT_FIND_URL = "https://api.trykitt.ai/job/find_email"
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
)


def _find_skill_dir() -> Path:
    return cc.skill_dir_from_script(__file__)


def ensure_env_loaded() -> None:
    cc.ensure_agent_env_loaded(_find_skill_dir())


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "email-finder-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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
    if os.environ.get("OUTREACHMAGIC_HOME"):
        cfg["outreachmagic_home"] = os.environ["OUTREACHMAGIC_HOME"]
    cfg.setdefault("trykitt_endpoint", TRYKITT_FIND_URL)
    cfg.setdefault("batch_delay_seconds", 8)
    cfg.setdefault("max_people_per_run", 50)
    cfg.setdefault("outreachmagic_home", "")
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
    out: dict[str, Any] = {
        "skill": SKILL_NAME,
        "trykitt_api_key_set": bool(key),
        "trykitt_api_key_preview": _mask_key(key) if key else None,
        "outreachmagic_found": om_dir is not None,
        "outreachmagic_home": str(om_dir) if om_dir else None,
        "batch_delay_seconds": cfg.get("batch_delay_seconds", 8),
    }
    if om_dir:
        has_key, source = cc.outreachmagic_agent_key_status(om_dir)
        out["outreachmagic_agent_key"] = {"set": has_key, "source": source}
    print(json.dumps(out, indent=2))


def _normalize_linkedin(url: str) -> str:
    u = (url or "").strip()
    if u and not u.startswith("http"):
        u = f"https://linkedin.com/in/{u.strip('/')}"
    return u


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
    if linkedin:
        slug = linkedin.strip("/").split("/")[-1] if "/" in linkedin else linkedin
        lead = cc.history_lookup(
            om_dir, ["--linkedin", slug], workspace=workspace or None, skill_dir=_find_skill_dir()
        )
        if lead:
            result.update(
                lead_id=lead.get("id"),
                email=lead.get("email"),
                linkedin_url=lead.get("linkedin_url"),
                name=lead.get("name"),
                company=lead.get("company_display") or lead.get("company"),
            )
            result["status"] = "exists_with_email" if lead.get("email") else "exists_no_email"
            return result
    if name:
        lead = cc.history_lookup(
            om_dir, ["--name", name], workspace=workspace or None, skill_dir=_find_skill_dir()
        )
        if lead:
            result.update(
                lead_id=lead.get("id"),
                email=lead.get("email"),
                linkedin_url=lead.get("linkedin_url"),
                name=lead.get("name"),
                company=lead.get("company_display") or lead.get("company"),
            )
            result["status"] = "exists_with_email" if lead.get("email") else "exists_no_email"
    return result


def cmd_check(name: str, company: str, workspace: str = "") -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print(json.dumps({"error": "outreachmagic not found — install outreachmagic first"}))
        sys.exit(1)
    print(json.dumps(check_existing_email(om_dir, name, company, workspace=workspace), indent=2))


def trykitt_find(
    cfg: dict[str, Any],
    *,
    full_name: str,
    domain: str,
    linkedin: str = "",
) -> dict[str, Any]:
    api_key = (cfg.get("trykitt_api_key") or "").strip()
    if not api_key:
        return {"error": "TRYKITT_API_KEY not set", "status": "no_key"}
    domain = domain.strip().lower().lstrip("@")
    if not domain or "." not in domain:
        return {"error": "valid --domain required", "status": "bad_input"}
    body = {
        "fullName": full_name.strip(),
        "domain": domain,
        "realtime": True,
    }
    li = _normalize_linkedin(linkedin)
    if li:
        body["linkedinStandardProfileURL"] = li
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        cfg.get("trykitt_endpoint", TRYKITT_FIND_URL),
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "User-Agent": "email-finder/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {
            "status": "http_error",
            "http_status": e.code,
            "error": err_body[:500],
        }
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"status": "error", "error": str(e)}
    email = (payload.get("email") or "").strip()
    validity = (payload.get("validity") or "").strip()
    out: dict[str, Any] = {
        "status": "found" if email else "not_found",
        "email": email or None,
        "validity": validity or None,
        "validSMTP": payload.get("validSMTP"),
        "jobId": payload.get("jobId"),
        "domain": domain,
        "full_name": full_name,
    }
    return out



def _validity_note_text(validity: str, *, found: bool) -> str:
    if not found:
        return "trykitt: no email found"
    v = (validity or "").lower()
    if v == "valid":
        return "trykitt verify: valid"
    if v in ("valid-risky", "risky"):
        return "trykitt verify: catch_all"
    if v:
        return f"trykitt verify: {v}"
    return "trykitt verify: unknown"


def build_import_profile(
    *,
    full_name: str,
    company: str,
    domain: str,
    linkedin: str,
    find_result: dict[str, Any],
) -> dict[str, Any]:
    email = find_result.get("email")
    profile: dict[str, Any] = {
        "name": full_name,
        "company": company or domain,
        "company_domain": domain,
        "tags": ["trykitt_attempted"],
    }
    if linkedin:
        profile["linkedin"] = _normalize_linkedin(linkedin)
    if email:
        profile["email"] = email
        profile["tags"] = ["trykitt_attempted", "email_found"]
    profile["notes"] = _validity_note_text(str(find_result.get("validity") or ""), found=bool(email))
    return profile


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
    )
    imported = batch_import_results(
        om_dir, [profile], workspace=workspace, source_detail="email-finder/trykitt",
    )
    lead_id = None
    imp = imported.get("import") or {}
    if isinstance(imp.get("results"), list) and imp["results"]:
        lead_id = imp["results"][0].get("lead_id") or imp["results"][0].get("id")
    return {"saved": True, "import": imp, "lead_id": lead_id}


def tag_trykitt_attempted(
    om_dir: Path,
    *,
    full_name: str,
    company: str,
    domain: str,
    linkedin: str = "",
    workspace: str = "",
) -> dict[str, Any]:
    """Record a trykitt attempt on miss so batch re-runs do not repeat."""
    profile = build_import_profile(
        full_name=full_name,
        company=company,
        domain=domain,
        linkedin=linkedin,
        find_result={},
    )
    imported = batch_import_results(
        om_dir,
        [profile],
        workspace=workspace,
        source_detail="email-finder/trykitt-miss",
    )
    out: dict[str, Any] = {"tagged": True, "import": imported.get("import", {})}
    if not workspace:
        out["warning"] = (
            "tags require --workspace on import-profiles; "
            "pass --workspace so trykitt_attempted persists"
        )
    return out


def _should_tag_trykitt_attempt(result: dict[str, Any]) -> bool:
    if result.get("error") in ("TRYKITT_API_KEY not set", "valid --domain required"):
        return False
    return result.get("status") not in ("skipped", "no_key", "bad_input")


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
    result = trykitt_find(cfg, full_name=name, domain=domain, linkedin=linkedin)
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
        elif _should_tag_trykitt_attempt(result):
            result["tag_attempt"] = tag_trykitt_attempted(
                om_dir,
                full_name=name,
                company=company or domain,
                domain=domain,
                linkedin=linkedin,
                workspace=workspace,
            )
    print(json.dumps(result, indent=2))


def _load_people_json(path: str) -> list[dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8")
    people = json.loads(raw)
    if isinstance(people, dict):
        people = people.get("people") or people.get("rows") or []
    if not isinstance(people, list):
        raise ValueError("input must be a JSON array or {people: [...]}")
    return [r for r in people if isinstance(r, dict)]


def _row_fields(row: dict[str, Any]) -> tuple[str, str, str, str]:
    name = (row.get("full_name") or row.get("name") or "").strip()
    domain = (row.get("company_domain") or row.get("domain") or "").strip()
    company = (row.get("company_name") or row.get("company") or "").strip()
    linkedin = (row.get("linkedin_url") or row.get("linkedin") or "").strip()
    return name, domain, company, linkedin


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


def _process_one_lead(
    cfg: dict[str, Any],
    om_dir: Optional[Path],
    row: dict[str, Any],
    *,
    workspace: str,
) -> dict[str, Any]:
    name, domain, company, linkedin = _row_fields(row)
    if not name or not domain:
        return {"status": "skipped", "reason": "missing name or domain", "row": row}
    if om_dir:
        existing = check_existing_email(om_dir, name, company or domain, linkedin, workspace=workspace)
        if existing.get("email"):
            return {"status": "skipped", "reason": "has_email", "existing": existing}
    return trykitt_find(cfg, full_name=name, domain=domain, linkedin=linkedin)


def _collect_import_profiles(
    results: list[dict[str, Any]],
    people_meta: list[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for (name, domain, company, linkedin), result in zip(people_meta, results):
        if result.get("status") == "skipped":
            continue
        if not _should_tag_trykitt_attempt(result) and not result.get("email"):
            continue
        profiles.append(
            build_import_profile(
                full_name=name,
                company=company or domain,
                domain=domain,
                linkedin=linkedin,
                find_result=result,
            )
        )
    return profiles


def cmd_batch_find(
    path: str,
    workspace: str = "",
    delay: float = 8.0,
    *,
    no_save: bool = False,
    output_csv: str = "",
) -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    try:
        people = _load_people_json(path)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    max_n = int(cfg.get("max_people_per_run", 50))
    if len(people) > max_n:
        print(json.dumps({"error": f"max {max_n} people per run"}))
        sys.exit(1)
    results: list[dict[str, Any]] = []
    csv_rows: list[dict[str, str]] = []
    import_meta: list[tuple[str, str, str, str]] = []
    for i, row in enumerate(people):
        name, domain, company, linkedin = _row_fields(row)
        result = _process_one_lead(cfg, om_dir, row, workspace=workspace)
        results.append(result)
        import_meta.append((name, domain, company, linkedin))
        if output_csv and name and domain:
            csv_rows.append(_result_to_csv_row(name, company, domain, linkedin, result, row))
        if i + 1 < len(people) and delay > 0:
            time.sleep(delay)
    save_out: dict[str, Any] = {}
    if om_dir and not no_save:
        profiles = _collect_import_profiles(results, import_meta)
        if profiles:
            save_out = batch_import_results(
                om_dir, profiles, workspace=workspace, source_detail="email-finder/batch",
            )
    if output_csv:
        write_results_csv(output_csv, csv_rows)
    out = {"count": len(results), "results": results}
    if save_out:
        out["batch_save"] = save_out
    if output_csv:
        out["csv"] = output_csv
    print(json.dumps(out, indent=2))


def cmd_parallel_find(
    path: str,
    workspace: str = "",
    workers: int = 3,
    delay: float = 3.0,
    *,
    output_csv: str = "",
    no_save: bool = False,
) -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    try:
        people = _load_people_json(path)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    workers = max(1, min(workers, 10))
    results: list[Optional[dict[str, Any]]] = [None] * len(people)
    csv_rows: list[dict[str, str]] = []
    import_meta: list[tuple[str, str, str, str]] = []

    def _task(idx: int, row: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if delay > 0:
            time.sleep(delay * (idx % workers))
        return idx, _process_one_lead(cfg, om_dir, row, workspace=workspace)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_task, i, row) for i, row in enumerate(people)]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result

    ordered = [r if r is not None else {"status": "error", "error": "missing result"} for r in results]
    for row, result in zip(people, ordered):
        name, domain, company, linkedin = _row_fields(row)
        import_meta.append((name, domain, company, linkedin))
        if output_csv and name and domain:
            csv_rows.append(_result_to_csv_row(name, company, domain, linkedin, result, row))

    save_out: dict[str, Any] = {}
    if om_dir and not no_save:
        profiles = _collect_import_profiles(ordered, import_meta)
        if profiles:
            save_out = batch_import_results(
                om_dir, profiles, workspace=workspace, source_detail="email-finder/parallel",
            )
    if output_csv:
        write_results_csv(output_csv, csv_rows)
    out = {"count": len(ordered), "workers": workers, "results": ordered}
    if save_out:
        out["batch_save"] = save_out
    if output_csv:
        out["csv"] = output_csv
    print(json.dumps(out, indent=2))


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
        print(json.dumps({"error": "outreachmagic not found — install outreachmagic first"}))
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
        profiles,
        workspace=ws,
        source_detail="email-finder/import-to-om",
    )
    print(json.dumps({"status": "ok", **result}, indent=2))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_tag(tag: str) -> str:
    t = tag.strip()
    return t if t.startswith("v") else f"v{t}"


def _current_skill_version() -> str:
    skill_md = _find_skill_dir() / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    m = re.search(r"^version:\s*([^\s]+)\s*$", text, flags=re.M)
    return m.group(1).strip() if m else "unknown"


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
        if _sha256_hex(content) != expected:
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
            print("Usage: email_finder.py find --name X --domain Y [--linkedin URL] [--company C] [--save] [--workspace W]")
            sys.exit(1)
        cmd_find(name, domain, linkedin, workspace, save, company)
    elif cmd == "batch-find":
        if len(sys.argv) < 3:
            print("Usage: email_finder.py batch-find [--delay 8] [--workspace W] [--no-save] [--output-csv PATH] input.json")
            sys.exit(1)
        delay = 8.0
        workspace = ""
        path = ""
        no_save = False
        output_csv = ""
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--delay" and i + 1 < len(args):
                delay = float(args[i + 1])
                i += 2
            elif args[i].startswith("--delay="):
                delay = float(args[i].split("=", 1)[1])
                i += 1
            elif args[i] == "--workspace" and i + 1 < len(args):
                workspace = args[i + 1]
                i += 2
            elif args[i].startswith("--workspace="):
                workspace = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--no-save":
                no_save = True
                i += 1
            elif args[i] == "--output-csv" and i + 1 < len(args):
                output_csv = args[i + 1]
                i += 2
            elif args[i].startswith("--output-csv="):
                output_csv = args[i].split("=", 1)[1]
                i += 1
            else:
                path = args[i]
                i += 1
        if not path:
            print("Usage: email_finder.py batch-find input.json")
            sys.exit(1)
        cmd_batch_find(path, workspace, delay, no_save=no_save, output_csv=output_csv)
    elif cmd == "parallel-find":
        if len(sys.argv) < 3:
            print("Usage: email_finder.py parallel-find [--workers 3] [--delay 3] [--workspace W] [--output-csv PATH] input.json")
            sys.exit(1)
        delay = 3.0
        workers = 3
        workspace = ""
        path = ""
        no_save = False
        output_csv = ""
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--workers" and i + 1 < len(args):
                workers = int(args[i + 1])
                i += 2
            elif args[i].startswith("--workers="):
                workers = int(args[i].split("=", 1)[1])
                i += 1
            elif args[i] == "--delay" and i + 1 < len(args):
                delay = float(args[i + 1])
                i += 2
            elif args[i].startswith("--delay="):
                delay = float(args[i].split("=", 1)[1])
                i += 1
            elif args[i] == "--workspace" and i + 1 < len(args):
                workspace = args[i + 1]
                i += 2
            elif args[i].startswith("--workspace="):
                workspace = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--no-save":
                no_save = True
                i += 1
            elif args[i] == "--output-csv" and i + 1 < len(args):
                output_csv = args[i + 1]
                i += 2
            elif args[i].startswith("--output-csv="):
                output_csv = args[i].split("=", 1)[1]
                i += 1
            else:
                path = args[i]
                i += 1
        if not path:
            print("Usage: email_finder.py parallel-find input.json")
            sys.exit(1)
        cmd_parallel_find(path, workspace, workers, delay, output_csv=output_csv, no_save=no_save)
    elif cmd == "prepare-import":
        csv_path = ""
        workspace = ""
        output_path = ""
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
            print("Usage: email_finder.py prepare-import --csv PATH [--workspace W] [--output PATH]")
            sys.exit(1)
        cmd_prepare_import(csv_path, workspace, output_path)
    elif cmd == "import-to-om":
        file_path = ""
        workspace = ""
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


if __name__ == "__main__":
    main()
