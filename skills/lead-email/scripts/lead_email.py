#!/usr/bin/env python3
"""
Lead Email — trykitt.ai email finding for Hermes / Cursor / Claude Code.

Checks outreachmagic before spending trykitt credits. Saves via import-profiles
and verify-email (record-only).

Usage:
    lead_email.py config
    lead_email.py check "Jane Doe" "Acme Corp"
    lead_email.py find --name "Jane Doe" --domain acme.com [--linkedin URL] [--save] [--workspace W]
    lead_email.py batch-find input.json [--delay 8] [--workspace W]
    lead_email.py update [--check] [--tag v1.0.0]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import companion_common as cc

SKILL_NAME = "lead-email"
GITHUB_REPO = "outreachmagic/lead-email"
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
    "scripts/lead_email.py",
)


def _find_skill_dir() -> Path:
    return cc.skill_dir_from_script(__file__)


def ensure_env_loaded() -> None:
    cc.ensure_agent_env_loaded(_find_skill_dir())


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lead-email-updater",
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


find_outreachmagic = cc.find_outreachmagic


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
            "User-Agent": "lead-email/1.0",
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


def _verify_status_from_validity(validity: str) -> Optional[str]:
    v = (validity or "").lower()
    if v == "valid":
        return "valid"
    if v in ("valid-risky", "risky"):
        return "risky"
    if v:
        return "unknown"
    return None


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
    profile: dict[str, Any] = {
        "name": full_name,
        "company": company or domain,
        "email": email,
        "company_domain": domain,
        "tags": ["trykitt_attempted"],
    }
    if linkedin:
        profile["linkedin"] = _normalize_linkedin(linkedin)
    imported = cc.run_import_profiles(
        om_dir,
        [profile],
        workspace=workspace,
        source_detail="lead-email/trykitt",
        skill_dir=_find_skill_dir(),
    )
    lead_id = find_result.get("lead_id") or imported.get("lead_id")
    if not lead_id and isinstance(imported.get("results"), list) and imported["results"]:
        lead_id = imported["results"][0].get("lead_id")
    verify_status = _verify_status_from_validity(str(find_result.get("validity") or ""))
    verify_out: dict[str, Any] = {}
    if lead_id and verify_status:
        verify_out = cc.run_verify_email(
            om_dir,
            int(lead_id),
            verify_status,
            "trykitt",
            source_detail=find_result.get("validity"),
            skill_dir=_find_skill_dir(),
        )
    return {"saved": True, "import": imported, "verify": verify_out, "lead_id": lead_id}


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
    if om_dir and save and result.get("email"):
        result["save"] = save_find_result(
            om_dir,
            full_name=name,
            company=company or domain,
            domain=domain,
            linkedin=linkedin,
            find_result=result,
            workspace=workspace,
        )
    print(json.dumps(result, indent=2))


def cmd_batch_find(path: str, workspace: str = "", delay: float = 8.0) -> None:
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    raw = Path(path).read_text(encoding="utf-8")
    people = json.loads(raw)
    if isinstance(people, dict):
        people = people.get("people") or people.get("rows") or []
    if not isinstance(people, list):
        print(json.dumps({"error": "input must be a JSON array or {people: [...]}"}))
        sys.exit(1)
    max_n = int(cfg.get("max_people_per_run", 50))
    if len(people) > max_n:
        print(json.dumps({"error": f"max {max_n} people per run"}))
        sys.exit(1)
    results: list[dict[str, Any]] = []
    for i, row in enumerate(people):
        if not isinstance(row, dict):
            continue
        name = (row.get("full_name") or row.get("name") or "").strip()
        domain = (row.get("company_domain") or row.get("domain") or "").strip()
        company = (row.get("company_name") or row.get("company") or "").strip()
        linkedin = (row.get("linkedin_url") or row.get("linkedin") or "").strip()
        if not name or not domain:
            results.append({"status": "skipped", "reason": "missing name or domain", "row": row})
            continue
        if om_dir:
            existing = check_existing_email(om_dir, name, company or domain, linkedin, workspace=workspace)
            if existing.get("email"):
                results.append({"status": "skipped", "reason": "has_email", "existing": existing})
                continue
        find_result = trykitt_find(cfg, full_name=name, domain=domain, linkedin=linkedin)
        if om_dir and find_result.get("email"):
            find_result["save"] = save_find_result(
                om_dir,
                full_name=name,
                company=company or domain,
                domain=domain,
                linkedin=linkedin,
                find_result=find_result,
                workspace=workspace,
            )
        results.append(find_result)
        if i + 1 < len(people) and delay > 0:
            time.sleep(delay)
    print(json.dumps({"count": len(results), "results": results}, indent=2))


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
            print('Usage: lead_email.py check [--workspace W] "Name" "Company"')
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
            print("Usage: lead_email.py find --name X --domain Y [--linkedin URL] [--company C] [--save] [--workspace W]")
            sys.exit(1)
        cmd_find(name, domain, linkedin, workspace, save, company)
    elif cmd == "batch-find":
        if len(sys.argv) < 3:
            print("Usage: lead_email.py batch-find [--delay 8] [--workspace W] input.json")
            sys.exit(1)
        delay = 8.0
        workspace = ""
        path = ""
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
            else:
                path = args[i]
                i += 1
        if not path:
            print("Usage: lead_email.py batch-find input.json")
            sys.exit(1)
        cmd_batch_find(path, workspace, delay)
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
