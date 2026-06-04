"""Batch email finding with incremental saves, dedup, and OM import."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import companion_common as cc
from health import (
    count_usable_find_providers,
    format_health_lines,
    icypeas_batch_warnings,
    run_health_check,
)
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
    provider_request_delay_seconds,
    resolve_provider_names,
    run_find_with_fallback,
    validity_to_verify_status,
)

BATCH_CSV_COLUMNS = (
    "resume_key", "lead_id", "name", "domain", "email", "validity",
    "error", "provider", "api_calls", "status", "icypeas_status", "timestamp",
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
    profile["list_source"] = provider
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
        lookup_domain = (lookup or {}).get("company_domain")
        if lookup_domain:
            domain = str(lookup_domain).strip().lower().lstrip("@")
        if not company and lookup and lookup.get("company"):
            company = str(lookup.get("company") or "")
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


def count_rows_missing_om_match(people: list[dict[str, Any]]) -> int:
    """Rows without lead_id or linkedin may create duplicate OM leads on import."""
    missing = 0
    for row in people:
        _name, _domain, _company, linkedin, lead_id = row_fields(row)
        if lead_id is None and not (linkedin or "").strip():
            missing += 1
    return missing


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

    @staticmethod
    def _normalize_resume_row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        if "api_calls" not in out and out.get("credits_used") not in (None, ""):
            out["api_calls"] = out.get("credits_used")
        return out

    def _load_existing(self) -> None:
        by_key: dict[str, dict[str, Any]] = {}

        def _merge_row(row: dict[str, Any]) -> None:
            row = self._normalize_resume_row(row)
            key = (row.get("resume_key") or "").strip()
            if key:
                by_key[key] = row

        if os.path.exists(self.json_path):
            try:
                data = json.loads(Path(self.json_path).read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for row in data:
                        if isinstance(row, dict):
                            _merge_row(row)
            except (json.JSONDecodeError, OSError):
                pass

        if os.path.exists(self.csv_path):
            with open(self.csv_path, encoding="utf-8", newline="") as fh:
                first_line = fh.readline()
                if not first_line.strip().startswith(BATCH_CSV_COLUMNS[0]):
                    if not by_key:
                        recovered = self.csv_path.replace(".csv", "-recovered.csv")
                        try:
                            os.rename(self.csv_path, recovered)
                        except OSError:
                            pass
                else:
                    fh.seek(0)
                    reader = csv.DictReader(fh)
                    if reader.fieldnames and BATCH_CSV_COLUMNS[0] in reader.fieldnames:
                        for row in reader:
                            _merge_row(row)

        self.done_keys = set(by_key.keys())
        self.buffer = list(by_key.values())

    def _open_csv(self):
        need_header = (
            not os.path.exists(self.csv_path)
            or os.path.getsize(self.csv_path) == 0
        )
        fh = open(
            self.csv_path,
            "a" if os.path.exists(self.csv_path) and not need_header else "w",
            encoding="utf-8",
            newline="",
        )
        writer = csv.writer(fh)
        if need_header:
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
    pre_skipped: dict[int, dict[str, Any]] = {}
    skipped_email = skipped_tagged = 0
    for i, row in enumerate(people):
        name, domain, _c, _li, _lid = row_fields(row)
        if not name or not domain:
            pre_skipped[i] = {
                "batch_status": "skipped",
                "status": "skipped",
                "skip_reason": "missing_name_or_domain",
            }
            continue
        if not validate_domain(domain):
            pre_skipped[i] = {
                "batch_status": "skipped",
                "status": "skipped",
                "skip_reason": "invalid_domain",
            }
            continue
        reason = skip_reason_from_lookup(lookup_by_index.get(i), provider_names)
        if reason:
            if reason == "has_email" or reason == "email_found_tag":
                skipped_email += 1
            else:
                skipped_tagged += 1
            pre_skipped[i] = {
                "batch_status": "skipped",
                "status": "skipped",
                "skip_reason": reason,
            }
            continue
        if writer and lead_resume_key(row, index=i) in writer.done_keys:
            skipped_tagged += 1
            pre_skipped[i] = {
                "batch_status": "skipped",
                "status": "skipped",
                "skip_reason": "resume_done",
            }
            continue
        to_process.append((i, row))

    resume_done = len(writer.done_keys) if writer else 0
    if resume_done and not opts.dry_run:
        print_resume_banner(resume_done, len(to_process), len(people))

    api_providers = []
    for pname in provider_names:
        key = cfg.get(f"{pname}_api_key") or cfg.get("trykitt_api_key" if pname == "trykitt" else "icypeas_api_key")
        api_providers.append((pname, str(key or "").strip()))

    workers_planned = min(max(opts.workers, 1), 5)
    rate_warnings = icypeas_batch_warnings(
        provider_names, workers=workers_planned, delay=opts.delay, cfg=cfg,
    )

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
        issues = [*issues, *rate_warnings]
        health_lines = format_health_lines(
            issues, ok_msgs, skip_om=opts.skip_om, om_connected=om_dir is not None,
        )
        missing_match = count_rows_missing_om_match(people) if not opts.skip_om else 0
        print_dry_run_box(
            to_process=len(to_process),
            skipped_email=skipped_email,
            skipped_tagged=skipped_tagged,
            provider=provider_label,
            workers=workers_planned,
            health_lines=health_lines,
            resume_done=resume_done,
            missing_om_match=missing_match,
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
    issues = [*issues, *rate_warnings]
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
        "rate_limited": 0,
        "timeout": 0,
        "api_calls": {p: 0 for p in provider_names},
        "verify": {"valid": 0, "catch_all": 0, "invalid": 0, "unknown": 0},
        "waterfall": {p: {"calls": 0, "found": 0, "not_found": 0, "errors": 0} for p in provider_names},
        "skipped": skipped_email + skipped_tagged,
    }
    request_delay = provider_request_delay_seconds(cfg, provider_names, cli_delay=opts.delay)
    results: list[dict[str, Any]] = [
        pre_skipped.get(i, {"batch_status": "pending"}) for i in range(len(people))
    ]
    start = time.time()
    workers = max(1, min(opts.workers, 5))
    done_count = 0
    total_work = len(to_process)

    credits_stop = False
    pending_queue: list[tuple[int, dict[str, Any]]] = list(to_process)

    def _work(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        idx, row = item
        if request_delay > 0:
            time.sleep(request_delay)
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
            result = {
                "status": "credits_exhausted",
                "error": str(e),
                "provider_attempts": [],
            }
        result["batch_status"] = "processed"
        return idx, result

    def _mark_credit_stop_remaining(queue: list[tuple[int, dict[str, Any]]]) -> None:
        for idx, _row in queue:
            results[idx] = {
                "batch_status": "skipped",
                "status": "skipped",
                "reason": "credits_exhausted",
                "provider_attempts": [],
            }

    while pending_queue and not credits_stop:
        chunk = pending_queue[:workers]
        pending_queue = pending_queue[workers:]
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            futures = [pool.submit(_work, item) for item in chunk]
            for fut in as_completed(futures):
                idx, result = fut.result()
                results[idx] = result
                done_count += 1
                _record_result(idx, people[idx], result, writer, stats)
                if done_count % CREDIT_RECHECK_EVERY == 0:
                    credits_stop = _mid_batch_credit_stop(cfg, api_providers, provider_names, stats)
                if done_count % opts.progress_every == 0:
                    print_progress(done_count, total_work, stats, start, provider=provider_label)
        if credits_stop:
            _mark_credit_stop_remaining(pending_queue)
            pending_queue = []
            break

    if credits_stop:
        stats["stopped_reason"] = "credits_exhausted"

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
            if not opts.workspace:
                save_out = {"error": "workspace required for OM save (--workspace)"}
                print(
                    "\n❌ Outreach Magic save skipped: --workspace is required.\n"
                    "   Email results are in CSV/JSON on disk.\n",
                    file=sys.stderr,
                )
            else:
                try:
                    batch_source = (opts.provider or "").strip() if opts.provider else ""
                    imported = cc.save_email_find_profiles(
                        om_dir,
                        profiles,
                        workspace=opts.workspace,
                        source=batch_source,
                        source_detail="email-finder/batch",
                        skill_dir=skill_dir,
                    )
                    import_created = int(imported.get("created") or 0)
                    if import_created:
                        new_ids = [
                            str(r.get("lead_id") or r.get("id"))
                            for r in (imported.get("results") or [])
                            if isinstance(r, dict) and r.get("created")
                        ]
                        id_hint = f" ids: {', '.join(new_ids[:5])}" if new_ids else ""
                        if len(new_ids) > 5:
                            id_hint += f" (+{len(new_ids) - 5} more)"
                        print(
                            f"\n⚠️  Warning: OM import created {import_created} new lead(s) "
                            f"(expected 0 — pass lead_id on every row).{id_hint}\n",
                            file=sys.stderr,
                        )
                    save_out = {"imported": len(profiles), "import": imported, "created": import_created}
                    if imported.get("mode") == "apply_email_find_results":
                        verified = int(imported.get("recorded") or 0)
                    else:
                        verify_items = build_verify_batch(imported, profiles)
                        if verify_items:
                            vout = cc.run_verify_email_batch(om_dir, verify_items, skill_dir=skill_dir)
                            verified = int(vout.get("recorded") or 0)
                            save_out["verify"] = vout
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    save_out = {"error": str(e)}
                    json_hint = f"{output_base}.json" if output_base else "<checkpoint.json>"
                    csv_hint = f"{output_base}.csv" if output_base else "<checkpoint.csv>"
                    print(
                        "\n❌ Outreach Magic save failed (email finding completed; results on disk).\n"
                        f"   {e}\n"
                        f"   CSV: {csv_hint}\n"
                        f"   JSON: {json_hint}\n"
                        "   Re-sync to OM:\n"
                        f"     python3 scripts/email_finder.py import-to-om --file {json_hint}"
                        f" --workspace {opts.workspace}\n"
                        "   Or re-run batch-find (resume skips completed API rows).\n",
                        file=sys.stderr,
                    )

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
    stats: Optional[dict[str, Any]] = None,
) -> bool:
    """Return True when no configured find provider can continue."""
    live = bool(cfg.get("trykitt_live_health_probe", False))
    if not live and stats:
        trykitt_remaining = stats.get("trykitt_remaining_credits")
        if trykitt_remaining is not None and float(trykitt_remaining) <= 0:
            if "icypeas" in provider_names and any(
                p == "icypeas" and k for p, k in api_providers
            ):
                return False

    usable_n, _usable, _issues = count_usable_find_providers(cfg, api_providers, provider_names)
    if usable_n == 0:
        print("\n⚠️  No find providers available — stopping batch (partial results saved).\n", file=sys.stderr)
        return True
    return False


def _record_result(
    idx: int,
    row: dict[str, Any],
    result: dict[str, Any],
    writer: Optional[IncrementalWriter],
    stats: dict[str, Any],
) -> None:
    st = str(result.get("status") or "")
    if st == "credits_exhausted":
        stats["errors"] += 1
        stats["credits_exhausted"] = int(stats.get("credits_exhausted", 0)) + 1
    elif st == "rate_limited":
        stats["rate_limited"] = int(stats.get("rate_limited", 0)) + 1
        stats["errors"] += 1
    elif st == "error" and str(result.get("error") or "") == "icypeas_timeout":
        stats["timeout"] = int(stats.get("timeout", 0)) + 1
        stats["errors"] += 1
    elif st in ("error", "http_error", "auth_error") or result.get("error"):
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
    credits = result.get("credits") if isinstance(result.get("credits"), dict) else {}
    if not credits and isinstance(result.get("credits_used"), (int, float)):
        pass
    if str(result.get("provider") or "") == "trykitt" and isinstance(result.get("credits"), dict):
        rem = result["credits"].get("remainingCredits")
        if rem is not None:
            stats["trykitt_remaining_credits"] = float(rem)
    attempts = result.get("provider_attempts") if isinstance(result.get("provider_attempts"), list) else []
    wf = stats.setdefault("waterfall", {})
    winning_provider = str(result.get("provider") or "") if result.get("email") else ""
    for att in attempts:
        if not isinstance(att, dict):
            continue
        p = str(att.get("provider") or "")
        if p not in wf:
            wf[p] = {"calls": 0, "found": 0, "not_found": 0, "errors": 0}
        if not att.get("attempted", True):
            continue
        wf[p]["calls"] = int(wf[p].get("calls", 0)) + 1
        st = str(att.get("status") or "")
        if p == winning_provider and result.get("email"):
            wf[p]["found"] = int(wf[p].get("found", 0)) + 1
        elif st in ("error", "http_error", "rate_limited") or (
            st == "error" and "credit" in str(att.get("error") or "").lower()
        ):
            wf[p]["errors"] = int(wf[p].get("errors", 0)) + 1
        elif st == "not_found" or (st == "found" and p != winning_provider):
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
        "icypeas_status": result.get("icypeas_status") or "",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    writer.append(row_dict, resume_key)
