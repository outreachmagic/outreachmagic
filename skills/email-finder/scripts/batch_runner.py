"""Batch email finding with incremental saves, dedup, and OM import."""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import companion_common as cc
from health import format_health_lines, run_health_check
from normalize import lead_resume_key, load_people_json, row_fields, sanitize_input_path, validate_domain
from progress import (
    print_dry_run_box,
    print_final_summary,
    print_progress,
    print_resume_banner,
    record_api_calls,
    record_verify_status,
)
from providers import (
    CreditsExhaustedError,
    provider_note_text,
    resolve_provider_names,
    run_find_with_fallback,
    validity_to_verify_status,
)

BATCH_CSV_COLUMNS = (
    "resume_key", "lead_id", "name", "domain", "email", "validity",
    "error", "provider", "api_calls", "status", "timestamp",
)
CREDIT_RECHECK_EVERY = 100


@dataclass
class BatchOptions:
    workspace: str = ""
    delay: float = 8.0
    workers: int = 1
    no_save: bool = False
    skip_om: bool = False
    provider: Optional[str] = None
    output_base: str = ""
    output_csv: str = ""
    max_leads: int = 500
    dry_run: bool = False
    yes: bool = False
    progress_every: int = 25
    json_checkpoint: int = 50


def build_import_profile(
    *,
    full_name: str,
    company: str,
    domain: str,
    linkedin: str,
    find_result: dict[str, Any],
    normalize_linkedin_fn: Callable[[str], str],
    lead_id: Optional[int] = None,
    external_id: Optional[str] = None,
) -> dict[str, Any]:
    email = find_result.get("email")
    attempts = find_result.get("provider_attempts") if isinstance(find_result.get("provider_attempts"), list) else []
    attempted_tags: list[str] = []
    if attempts:
        for attempt in attempts:
            p = str((attempt or {}).get("provider") or "")
            if p == "trykitt" and "trykitt_attempted" not in attempted_tags:
                attempted_tags.append("trykitt_attempted")
            if p == "icypeas" and "icypeas_attempted" not in attempted_tags:
                attempted_tags.append("icypeas_attempted")
    else:
        p = str(find_result.get("provider") or "trykitt")
        attempted_tags.append("icypeas_attempted" if p == "icypeas" else "trykitt_attempted")
    profile: dict[str, Any] = {
        "name": full_name,
        "company": company or domain,
        "company_domain": domain,
        "tags": attempted_tags,
    }
    if lead_id is not None:
        profile["id"] = lead_id
    if external_id:
        profile["external_id"] = external_id
    if linkedin:
        profile["linkedin"] = normalize_linkedin_fn(linkedin)
    if email:
        profile["email"] = email
        profile["tags"] = [*attempted_tags, "email_found"]
    provider = str(find_result.get("provider") or "trykitt")
    validity = str(find_result.get("validity") or "")
    profile["notes"] = provider_note_text(provider, validity, found=bool(email))
    if email:
        profile["_verify_provider"] = provider
        profile["_verify_validity"] = validity
    return profile


def should_tag_provider_attempt(result: dict[str, Any]) -> bool:
    if result.get("attempted") is False:
        return False
    return result.get("status") in ("found", "not_found")


def collect_import_profiles(
    rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    normalize_linkedin_fn: Callable[[str], str],
    *,
    lookup_by_index: Optional[dict[int, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for i, (row, result) in enumerate(zip(rows, results)):
        if result.get("batch_status") == "skipped":
            continue
        name, domain, company, linkedin, lead_id = row_fields(row)
        lookup = (lookup_by_index or {}).get(i) if lookup_by_index else None
        if lead_id is None and lookup and lookup.get("lead_id"):
            lead_id = int(lookup["lead_id"])
        if not linkedin and lookup:
            linkedin = str(lookup.get("linkedin_url") or lookup.get("linkedin") or "")
        ext = str(row.get("external_id") or row.get("sales_nav_id") or "").strip() or None
        attempts = result.get("provider_attempts") if isinstance(result.get("provider_attempts"), list) else []
        should_tag = any(should_tag_provider_attempt(a) for a in attempts if isinstance(a, dict))
        if not should_tag and not result.get("email"):
            continue
        profiles.append(
            build_import_profile(
                full_name=name,
                company=company or domain,
                domain=domain,
                linkedin=linkedin,
                find_result=result,
                normalize_linkedin_fn=normalize_linkedin_fn,
                lead_id=lead_id,
                external_id=ext,
            )
        )
    return profiles


def bulk_dedup_map(
    om_dir: Path,
    people: list[dict[str, Any]],
    *,
    workspace: str,
    skill_dir: Path,
    provider_names: list[str],
) -> dict[int, dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for i, row in enumerate(people):
        name, domain, _co, linkedin, lead_id = row_fields(row)
        item: dict[str, Any] = {"index": i}
        if lead_id is not None:
            item["lead_id"] = lead_id
        elif linkedin:
            item["linkedin"] = linkedin
        elif name:
            item["name"] = name
        items.append(item)
    if not items:
        return {}
    try:
        payload = cc.run_batch_lead_lookup(om_dir, items, workspace=workspace, skill_dir=skill_dir)
    except RuntimeError:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for entry in payload.get("results") or []:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if idx is None:
            continue
        out[int(idx)] = entry
    return out


def skip_reason_from_lookup(
    lookup: Optional[dict[str, Any]],
    provider_names: list[str],
) -> Optional[str]:
    if not lookup or lookup.get("status") != "found":
        return None
    email = (lookup.get("email") or "").strip()
    if email:
        return "has_email"
    tags = set(lookup.get("tags") or [])
    if "email_found" in tags:
        return "email_found_tag"
    for p in provider_names:
        if f"{p}_attempted" in tags:
            return f"{p}_attempted"
    return None


def _resolve_output_base(path: str, input_path: str) -> str:
    if path:
        p = Path(path).expanduser()
        if p.suffix in (".csv", ".json"):
            return str(p.with_suffix(""))
        return str(p)
    stem = Path(input_path).expanduser().stem
    return str(Path.cwd() / f"{stem}-email-results")


class IncrementalWriter:
    def __init__(self, output_base: str) -> None:
        self.output_base = output_base
        self.csv_path = f"{output_base}.csv"
        self.json_path = f"{output_base}.json"
        self.buffer: list[dict[str, Any]] = []
        self.done_keys: set[str] = set()
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        if os.path.exists(self.json_path):
            try:
                data = json.loads(Path(self.json_path).read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for row in data:
                        if isinstance(row, dict):
                            key = (row.get("resume_key") or "").strip()
                            if key:
                                self.done_keys.add(key)
                                self.buffer.append(row)
            except (json.JSONDecodeError, OSError):
                pass
        if self.done_keys or not os.path.exists(self.csv_path):
            return
        with open(self.csv_path, encoding="utf-8", newline="") as fh:
            first_line = fh.readline()
            if not first_line.strip().startswith(BATCH_CSV_COLUMNS[0]):
                recovered = self.csv_path.replace(".csv", "-recovered.csv")
                try:
                    os.rename(self.csv_path, recovered)
                except OSError:
                    pass
                return
            fh.seek(0)
            reader = csv.DictReader(fh)
            if not reader.fieldnames or BATCH_CSV_COLUMNS[0] not in (reader.fieldnames or []):
                return
            for row in reader:
                key = (row.get("resume_key") or "").strip()
                if key:
                    self.done_keys.add(key)
                    self.buffer.append(row)

    def _open_csv(self):
        new_file = not self.done_keys
        fh = open(self.csv_path, "a" if self.done_keys else "w", encoding="utf-8", newline="")
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(BATCH_CSV_COLUMNS)
            fh.flush()
            os.fsync(fh.fileno())
        return fh, writer

    def append(self, row_dict: dict[str, Any], resume_key: str) -> None:
        with self._lock:
            if resume_key in self.done_keys:
                return
            self.done_keys.add(resume_key)
            self.buffer.append(row_dict)
            fh, writer = self._open_csv()
            try:
                writer.writerow([row_dict.get(c, "") for c in BATCH_CSV_COLUMNS])
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fh.close()
            if len(self.buffer) % 50 == 0:
                self._write_json()

    def _write_json(self) -> None:
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(self.buffer, f, indent=2)
        try:
            fd = os.open(self.json_path, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass

    def finalize(self) -> None:
        self._write_json()


def build_verify_batch(
    import_result: dict[str, Any],
    clean_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build verify-email batch aligned with import-profiles row results."""
    verify_items: list[dict[str, Any]] = []
    imp_results = import_result.get("results") or []
    for imp_row, profile in zip(imp_results, clean_profiles):
        if not isinstance(imp_row, dict):
            continue
        email = profile.get("email")
        if not email:
            continue
        lead_id = imp_row.get("lead_id") or imp_row.get("id")
        if not lead_id:
            continue
        provider = str(profile.get("_verify_provider") or "trykitt")
        validity = str(profile.get("_verify_validity") or "")
        verify_items.append({
            "lead_id": int(lead_id),
            "email": email,
            "status": validity_to_verify_status(validity, provider=provider),
            "source": provider,
            "source_detail": "email-finder/batch",
        })
    return verify_items


def strip_profiles_for_import(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep _verify_* keys for post-import verify batch (not sent to API)."""
    return [dict(profile) for profile in profiles]


def run_batch(
    input_path: str,
    cfg: dict[str, Any],
    om_dir: Optional[Path],
    opts: BatchOptions,
    *,
    skill_dir: Path,
    normalize_linkedin_fn: Callable[[str], str],
    key_status_fn: Callable,
) -> dict[str, Any]:
    people = load_people_json(input_path)
    if len(people) > opts.max_leads:
        return {"error": f"max {opts.max_leads} people per run (use --max to raise)"}

    provider_names = resolve_provider_names(cfg, opts.provider)
    if not provider_names:
        return {"error": "no provider configured (check API keys and --provider)"}

    provider_label = "+".join(provider_names)
    output_base = _resolve_output_base(
        opts.output_base or (str(Path(opts.output_csv).with_suffix("")) if opts.output_csv else ""),
        input_path,
    )
    writer: Optional[IncrementalWriter] = (
        IncrementalWriter(output_base) if output_base else None
    )

    lookup_by_index: dict[int, dict[str, Any]] = {}
    if om_dir and not opts.skip_om:
        lookup_by_index = bulk_dedup_map(
            om_dir, people, workspace=opts.workspace, skill_dir=skill_dir, provider_names=provider_names,
        )

    to_process: list[tuple[int, dict[str, Any]]] = []
    skipped_email = skipped_tagged = 0
    for i, row in enumerate(people):
        name, domain, _c, _li, _lid = row_fields(row)
        if not name or not domain:
            continue
        if not validate_domain(domain):
            continue
        reason = skip_reason_from_lookup(lookup_by_index.get(i), provider_names)
        if reason:
            if reason == "has_email" or reason == "email_found_tag":
                skipped_email += 1
            else:
                skipped_tagged += 1
            continue
        if writer and lead_resume_key(row, index=i) in writer.done_keys:
            skipped_tagged += 1
            continue
        to_process.append((i, row))

    resume_done = len(writer.done_keys) if writer else 0
    if resume_done and not opts.dry_run:
        print_resume_banner(resume_done, len(to_process), len(people))

    api_providers = []
    for pname in provider_names:
        key = cfg.get(f"{pname}_api_key") or cfg.get("trykitt_api_key" if pname == "trykitt" else "icypeas_api_key")
        api_providers.append((pname, str(key or "").strip()))

    health_lines: list[str] = []
    if opts.dry_run:
        _ok, issues, ok_msgs = run_health_check(
            cfg,
            om_dir=om_dir,
            key_status_fn=key_status_fn,
            providers=api_providers,
            batch_size=len(to_process),
            skip_om=opts.skip_om,
        )
        health_lines = format_health_lines(
            issues, ok_msgs, skip_om=opts.skip_om, om_connected=om_dir is not None,
        )
        print_dry_run_box(
            to_process=len(to_process),
            skipped_email=skipped_email,
            skipped_tagged=skipped_tagged,
            provider=provider_label,
            workers=min(max(opts.workers, 1), 5),
            health_lines=health_lines,
            resume_done=resume_done,
        )
        return {
            "dry_run": True,
            "to_process": len(to_process),
            "skipped_email": skipped_email,
            "skipped_tagged": skipped_tagged,
        }

    ok, issues, ok_msgs = run_health_check(
        cfg,
        om_dir=om_dir,
        key_status_fn=key_status_fn,
        providers=api_providers,
        batch_size=len(to_process),
        skip_om=opts.skip_om,
    )
    for msg in ok_msgs:
        print(f"  ✅ {msg}")
    if issues:
        print("\n⚠️  Health check issues:")
        for issue in issues:
            print(f"   • {issue}")
        if not opts.yes:
            return {"error": "health check failed", "issues": issues}

    if to_process and not opts.yes:
        print(f"\nAbout to run {len(to_process)} API lookups ({provider_label}). Pass --yes to confirm.\n")
        return {"error": "confirmation required", "use": "--yes"}

    stats: dict[str, Any] = {
        "found": 0,
        "not_found": 0,
        "errors": 0,
        "api_calls": {p: 0 for p in provider_names},
        "verify": {"valid": 0, "catch_all": 0, "invalid": 0, "unknown": 0},
        "waterfall": {p: {"calls": 0, "found": 0, "not_found": 0, "errors": 0} for p in provider_names},
        "skipped": skipped_email + skipped_tagged,
    }
    results: list[dict[str, Any]] = [{"batch_status": "pending"} for _ in people]
    start = time.time()
    workers = max(1, min(opts.workers, 5))
    done_count = 0
    total_work = len(to_process)

    credits_stop = False

    def _work(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        idx, row = item
        if opts.delay > 0 and workers == 1:
            time.sleep(opts.delay)
        name, domain, _company, linkedin, _lead_id = row_fields(row)
        try:
            result = run_find_with_fallback(
                cfg,
                full_name=name,
                domain=domain,
                linkedin=linkedin,
                provider_names=provider_names,
            )
        except CreditsExhaustedError as e:
            result = {"status": "error", "error": str(e), "provider_attempts": []}
        result["batch_status"] = "processed"
        return idx, result

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_work, item): item for item in to_process}
            for fut in as_completed(futures):
                if credits_stop:
                    fut.cancel()
                    continue
                idx, result = fut.result()
                results[idx] = result
                done_count += 1
                _record_result(idx, people[idx], result, writer, stats)
                if done_count % CREDIT_RECHECK_EVERY == 0:
                    credits_stop = _mid_batch_credit_stop(cfg, api_providers, provider_names)
                if done_count % opts.progress_every == 0:
                    print_progress(done_count, total_work, stats, start, provider=provider_label)
    else:
        for item in to_process:
            if credits_stop:
                break
            idx, result = _work(item)
            results[idx] = result
            done_count += 1
            _record_result(idx, people[idx], result, writer, stats)
            if done_count % CREDIT_RECHECK_EVERY == 0:
                credits_stop = _mid_batch_credit_stop(cfg, api_providers, provider_names)
            if done_count % opts.progress_every == 0:
                print_progress(done_count, total_work, stats, start, provider=provider_label)

    if writer:
        writer.finalize()

    save_out: dict[str, Any] = {}
    verified = 0
    import_created = 0
    if om_dir and not opts.no_save and not opts.skip_om:
        profiles = collect_import_profiles(
            people, results, normalize_linkedin_fn, lookup_by_index=lookup_by_index,
        )
        if profiles:
            try:
                import_profiles = strip_profiles_for_import(profiles)
                imported = cc.run_import_profiles(
                    om_dir,
                    [{k: v for k, v in p.items() if not str(k).startswith("_verify")} for p in import_profiles],
                    workspace=opts.workspace,
                    source_detail="email-finder/batch",
                    skill_dir=skill_dir,
                )
                import_created = int(imported.get("created") or 0)
                if import_created:
                    print(
                        f"\n⚠️  Warning: import-profiles created {import_created} new lead(s) "
                        f"(expected 0 — pass lead_id/linkedin in input).\n",
                        file=sys.stderr,
                    )
                save_out = {"imported": len(import_profiles), "import": imported, "created": import_created}
                verify_items = build_verify_batch(imported, import_profiles)
                if verify_items:
                    vout = cc.run_verify_email_batch(om_dir, verify_items, skill_dir=skill_dir)
                    verified = int(vout.get("recorded") or 0)
                    save_out["verify"] = vout
            except RuntimeError as e:
                save_out = {"error": str(e)}

    elapsed = time.time() - start
    print_final_summary(
        stats,
        elapsed,
        output_base,
        provider=provider_label,
        imported_count=int(save_out.get("imported") or 0),
        verified_count=verified,
        import_created=import_created,
    )

    return {
        "count": len(people),
        "processed": done_count,
        "stats": stats,
        "output_base": output_base,
        "batch_save": save_out,
        "results": results,
    }


def _mid_batch_credit_stop(
    cfg: dict[str, Any],
    api_providers: list[tuple[str, str]],
    provider_names: list[str],
) -> bool:
    """Return True if all configured find providers are out of credits."""
    from health import probe_trykitt

    exhausted = 0
    checked = 0
    for name, api_key in api_providers:
        if name not in provider_names or not api_key:
            continue
        checked += 1
        if name == "trykitt":
            remaining, _lookups, err = probe_trykitt(api_key)
            if err or remaining <= 0:
                exhausted += 1
        elif name == "icypeas":
            pass
    if checked and exhausted >= checked:
        print("\n⚠️  Credits exhausted — stopping batch (partial results saved).\n", file=sys.stderr)
        return True
    return False


def _record_result(
    idx: int,
    row: dict[str, Any],
    result: dict[str, Any],
    writer: Optional[IncrementalWriter],
    stats: dict[str, Any],
) -> None:
    if result.get("error") and result.get("status") == "error":
        stats["errors"] += 1
    elif result.get("email"):
        stats["found"] += 1
        record_verify_status(
            stats,
            str(result.get("validity") or ""),
            str(result.get("provider") or "trykitt"),
        )
    elif result.get("status") == "skipped":
        pass
    else:
        stats["not_found"] += 1
    record_api_calls(stats, result)
    attempts = result.get("provider_attempts") if isinstance(result.get("provider_attempts"), list) else []
    wf = stats.setdefault("waterfall", {})
    for att in attempts:
        if not isinstance(att, dict):
            continue
        p = str(att.get("provider") or "")
        if p not in wf:
            wf[p] = {"calls": 0, "found": 0, "not_found": 0, "errors": 0}
        wf[p]["calls"] = int(wf[p].get("calls", 0)) + 1
        st = str(att.get("status") or "")
        if st == "found" or (p == str(result.get("provider")) and result.get("email")):
            wf[p]["found"] = int(wf[p].get("found", 0)) + 1
        elif st in ("error", "http_error"):
            wf[p]["errors"] = int(wf[p].get("errors", 0)) + 1
        elif st == "not_found":
            wf[p]["not_found"] = int(wf[p].get("not_found", 0)) + 1

    if not writer:
        return
    name, domain, _c, _li, lead_id = row_fields(row)
    resume_key = lead_resume_key(row, index=idx)
    api_n = 1
    if isinstance(attempts, list):
        api_n = sum(1 for a in attempts if isinstance(a, dict) and a.get("attempted", True))
    elif result.get("provider"):
        api_n = 1
    row_dict = {
        "resume_key": resume_key,
        "lead_id": lead_id or "",
        "name": name,
        "domain": domain,
        "email": result.get("email") or "",
        "validity": result.get("validity") or "",
        "error": result.get("error") or "",
        "provider": result.get("provider") or "",
        "api_calls": api_n,
        "status": result.get("status") or "",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    writer.append(row_dict, resume_key)
