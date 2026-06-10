#!/usr/bin/env python3
"""Layer 2: run script-mode catalog cases against installed skill CLIs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from validate import validate_expect  # noqa: E402

SKILL_BINARIES = {
    "outreachmagic": "pipeline.py",
    "lead-enrich": "enrich.py",
    "email-finder": "email_finder.py",
}


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def load_catalog(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_cases(
    catalog: dict[str, Any],
    *,
    skills: set[str] | None,
    tags: set[str] | None,
    ids: set[str] | None,
    exclude: set[str] | None,
    mode: str,
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for skill_name, cases in catalog.get("skills", {}).items():
        if skills and skill_name not in skills:
            continue
        for case in cases:
            if case.get("mode", "agent") != mode:
                continue
            case_id = case.get("id", "")
            case_tags = set(case.get("tags") or [])
            if ids and case_id not in ids:
                continue
            if tags and not case_tags.intersection(tags):
                continue
            if exclude and case_id in exclude:
                continue
            out.append((skill_name, case))
    return out


def _fixture_root(catalog_path: Path) -> Path:
    return catalog_path.parent / "fixtures"


def _resolve_case_path(raw: str, *, catalog_path: Path) -> str:
    token = (raw or "").strip()
    if token.startswith("@fixture:"):
        rel = token[len("@fixture:") :]
        return str((_fixture_root(catalog_path) / rel).resolve())
    if token.startswith("@"):
        return str(Path(token[1:]).expanduser().resolve())
    return token


def _run_case_setup(setup: str | None, *, catalog_path: Path) -> str | None:
    if not setup:
        return None
    if setup == "copy_fixture_db":
        rel = "dedup"
    elif setup.startswith("copy_fixture_db:"):
        rel = setup.split(":", 1)[1].strip("/")
    else:
        return f"unknown setup hook: {setup}"
    src = _fixture_root(catalog_path) / rel / "data-root"
    dest = _fixture_root(catalog_path) / rel / "data-root-copy"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return None


def _resolve_case_env(
    env: dict[str, str] | None,
    *,
    catalog_path: Path,
    skills_root: Path,
) -> dict[str, str]:
    if not env:
        return {}
    out: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(value, str) and value.startswith("@fixture:"):
            rel = value[len("@fixture:") :]
            out[key] = str((_fixture_root(catalog_path) / rel).resolve())
        elif isinstance(value, str) and value.startswith("@skills:"):
            rel = value[len("@skills:") :]
            out[key] = str((skills_root / rel).resolve())
        else:
            out[key] = str(value)
    return out


def _companion_outreachmagic_home(skills_root: Path) -> str | None:
    om = (skills_root / "outreachmagic").resolve()
    if (om / "scripts" / "pipeline.py").is_file():
        return str(om)
    return None


def run_skill_command(
    skills_root: Path,
    skill: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    script_name = SKILL_BINARIES.get(skill)
    if not script_name:
        return 1, f"unknown skill: {skill}"
    script = skills_root / skill / "scripts" / script_name
    if not script.is_file():
        return 1, f"missing script: {script}"
    proc_env = os.environ.copy()
    if skill in ("lead-enrich", "email-finder"):
        om_home = _companion_outreachmagic_home(skills_root)
        if om_home and not proc_env.get("OUTREACHMAGIC_HOME"):
            proc_env["OUTREACHMAGIC_HOME"] = om_home
    if env:
        proc_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(script), *command],
        capture_output=True,
        text=True,
        timeout=300,
        env=proc_env,
    )
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, combined.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--skills-root", required=True)
    parser.add_argument("--skills", default="")
    parser.add_argument("--tags", default="")
    parser.add_argument("--ids", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--environment", default="hermes-script")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    skills = {s.strip() for s in args.skills.split(",") if s.strip()} or None
    tags = {t.strip() for t in args.tags.split(",") if t.strip()} or None
    ids = {i.strip() for i in args.ids.split(",") if i.strip()} or None
    exclude = {e.strip() for e in args.exclude.split(",") if e.strip()} or None

    catalog_path = _expand(args.catalog)
    catalog = load_catalog(catalog_path)
    skills_root = _expand(args.skills_root)
    cases = iter_cases(
        catalog,
        skills=skills,
        tags=tags,
        ids=ids,
        exclude=exclude,
        mode="script",
    )

    results: list[dict[str, Any]] = []
    passed = failed = 0
    for skill_name, case in cases:
        case_id = case["id"]
        setup_err = _run_case_setup(case.get("setup"), catalog_path=catalog_path)
        if setup_err:
            results.append(
                {
                    "id": case_id,
                    "skill": case.get("skill") or skill_name,
                    "status": "fail",
                    "prompt": "",
                    "actual": "",
                    "reason": setup_err,
                }
            )
            failed += 1
            print(f"TEST {case_id}: FAIL — {setup_err}")
            continue
        command = [
            _resolve_case_path(part, catalog_path=catalog_path)
            if isinstance(part, str) and (part.startswith("@") or part.startswith("@fixture:"))
            else part
            for part in (case.get("command") or [])
        ]
        target_skill = case.get("skill") or skill_name
        case_env = _resolve_case_env(case.get("env"), catalog_path=catalog_path, skills_root=skills_root)
        external = case.get("external")
        if external:
            parts = external.split()
            ext_cmd = " ".join(
                _resolve_case_path(part, catalog_path=catalog_path)
                if part.startswith("@fixture:") or part.startswith("@")
                else part
                for part in parts
            )
            proc_env = os.environ.copy()
            proc_env.update(case_env)
            proc = subprocess.run(
                ext_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
                env=proc_env,
            )
            rc, output = proc.returncode, ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        else:
            rc, output = run_skill_command(skills_root, target_skill, command, env=case_env)
        if rc != 0 and not output:
            output = f"exit code {rc}"
        ok, reason = validate_expect(output, case.get("expect") or {})
        if rc != 0 and ok:
            ok, reason = False, f"command exit {rc}"
        status = "pass" if ok else "fail"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append(
            {
                "id": case_id,
                "skill": target_skill,
                "status": status,
                "prompt": " ".join(command),
                "actual": output[:100000],
                "reason": reason,
            }
        )
        print(f"TEST {case_id}: {'PASS' if ok else 'FAIL'}" + (f" — {reason}" if reason else ""))

    payload = {
        "environment": args.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
    out_path = _expand(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: {passed} / FAIL: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
