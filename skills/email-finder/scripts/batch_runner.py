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
    print_preflight_summary,
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
_PROVIDER_ENV_KEYS = {"trykitt": "TRYKITT_API_KEY", "icypeas": "ICYPEAS_API_KEY"}

RETRYABLE_CHECKPOINT_STATUSES = frozenset({
    "error", "http_error", "auth_error", "rate_limited", "credits_exhausted", "bad_input",
})


def checkpoint_row_is_complete(row: dict[str, Any]) -> bool:
    """True when a checkpoint row should be skipped on resume."""
    st = str(row.get("status") or "").strip().lower()
    if st in RETRYABLE_CHECKPOINT_STATUSES:
        return False
    if st == "skipped":
        return True
    if row.get("email"):
        return True
    if st == "not_found":
        return True
    if row.get("error"):
        return False
    if st:
        return st not in RETRYABLE_CHECKPOINT_STATUSES
    return bool(row.get("timestamp"))


def count_checkpoint_errors(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if not checkpoint_row_is_complete(row))


def _reload_cfg_provider_keys(cfg: dict[str, Any], provider_names: list[str]) -> None:
    for pname in provider_names:
        env_key = _PROVIDER_ENV_KEYS.get(pname)
        if not env_key:
            continue
        val = os.environ.get(env_key, "").strip()
        if val:
            cfg[f"{pname}_api_key"] = val


def _api_providers_from_cfg(cfg: dict[str, Any], provider_names: list[str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for pname in provider_names:
        key = cfg.get(f"{pname}_api_key") or cfg.get(
            "trykitt_api_key" if pname == "trykitt" else "icypeas_api_key"
        )
        rows.append((pname, str(key or "").strip()))
    return rows


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
    retry_errors: bool = False


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
            if not should_tag_provider_attempt(attempt if isinstance(attempt, dict) else {}):
                continue
            p = str((attempt or {}).get("provider") or "")
            tag = f"{p}_attempted"
            if tag not in attempted_tags and p in ("trykitt", "icypeas"):
                attempted_tags.append(tag)
    else:
        p = str(find_result.get("provider") or ("trykitt" if email else ""))
        if email and p in ("trykitt", "icypeas"):
            attempted_tags.append(f"{p}_attempted")
        elif p in ("trykitt", "icypeas") and should_tag_provider_attempt(
            {"provider": p, "status": find_result.get("status")}
        ):
            attempted_tags.append(f"{p}_attempted")
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
        profile["tags"] = attempted_tags
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


def _providers_with_keys(cfg: dict[str, Any], names: list[str]) -> list[str]:
    out: list[str] = []
    for pname in names:
        key = cfg.get(f"{pname}_api_key") or cfg.get(
            "trykitt_api_key" if pname == "trykitt" else "icypeas_api_key"
        )
        if str(key or "").strip():
            out.append(pname)
    return out


def prompt_batch_provider_plan(
    cfg: dict[str, Any],
    *,
    cli_provider: Optional[str],
    yes: bool,
    dry_run: bool,
) -> list[str]:
    if cli_provider:
        return resolve_provider_names(cfg, cli_provider)
    enabled = resolve_provider_names(cfg, None)
    keyed = _providers_with_keys(cfg, enabled)
    if len(keyed) <= 1:
        return keyed or enabled
    if yes or dry_run or not sys.stdin.isatty():
        return enabled
    print("\nHow do you want to find emails?\n", file=sys.stderr)
    print("  1. (Recommended) TryKitt first, Icypeas as fallback if not found", file=sys.stderr)
    print("  2. TryKitt only", file=sys.stderr)
    print("  3. Icypeas only", file=sys.stderr)
    choice = input("\nEnter choice [1]: ").strip() or "1"
    if choice == "2":
        return ["trykitt"]
    if choice == "3":
        return ["icypeas"]
    if "trykitt" in keyed and "icypeas" in keyed:
        return ["trykitt", "icypeas"]
    return keyed


def prompt_batch_confirm(
    *,
    to_process: int,
    skipped_total: int,
    provider_label: str,
    yes: bool,
    dry_run: bool,
) -> bool:
    if dry_run or yes or not to_process:
        return True
    if not sys.stdin.isatty():
        return False
    print("\nReady to run:", file=sys.stderr)
    print(f"  Providers:     {provider_label}", file=sys.stderr)
    print(
        f"  Leads to try:  {to_process} ({skipped_total} skipped — already have email or previously attempted)",
        file=sys.stderr,
    )
    print(f"  Max credits:   ~{to_process} (1 per email found; misses cost 0)\n", file=sys.stderr)
    answer = input("Proceed? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def should_import_result(result: dict[str, Any]) -> bool:
    """True when a batch row should be saved to OM (found, not_found, or has email)."""
    if result.get("batch_status") == "skipped":
        return False
    if result.get("email"):
        return True
    status = str(result.get("status") or "").lower()
    if status in ("found", "not_found"):
        return True
    attempts = result.get("provider_attempts") if isinstance(result.get("provider_attempts"), list) else []
    if any(should_tag_provider_attempt(a) for a in attempts if isinstance(a, dict)):
        return True
    return False


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
        if not should_import_result(result):
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


def resolve_profiles_for_import(
    rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    normalize_linkedin_fn: Callable[[str], str],
    *,
    lookup_by_index: Optional[dict[int, dict[str, Any]]] = None,
    checkpoint_rows: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """Build OM import profiles from in-memory results, falling back to checkpoint rows."""
    profiles = collect_import_profiles(
        rows, results, normalize_linkedin_fn, lookup_by_index=lookup_by_index,
    )
    if profiles:
        return profiles, "from_results"
    if checkpoint_rows:
        checkpoint_profiles = profiles_from_checkpoint_rows(checkpoint_rows, normalize_linkedin_fn)
        if checkpoint_profiles:
            return checkpoint_profiles, "from_checkpoint"
    return [], "empty"


def _is_import_profile_row(row: dict[str, Any]) -> bool:
    if row.get("company_domain"):
        return True
    return isinstance(row.get("tags"), list)


def _parse_lead_id(row: dict[str, Any]) -> Optional[int]:
    raw = row.get("lead_id") or row.get("id")
    if raw is not None and str(raw).strip().isdigit():
        return int(str(raw).strip())
    return None


def _checkpoint_row_to_find_result(row: dict[str, Any]) -> dict[str, Any]:
    email = str(row.get("email") or row.get("found_email") or "").strip()
    status = str(row.get("status") or row.get("batch_status") or "").strip().lower()
    provider = str(row.get("provider") or "trykitt").strip() or "trykitt"
    if not status:
        status = "found" if email else "not_found"
    out: dict[str, Any] = {
        "validity": str(row.get("validity") or ""),
        "provider": provider,
        "status": status,
    }
    if email:
        out["email"] = email
    return out


def profiles_from_checkpoint_rows(
    rows: list[dict[str, Any]],
    normalize_linkedin_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Build OM import profiles from batch-find checkpoint CSV/JSON rows."""
    profiles: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or row.get("batch_status") or "").strip().lower()
        if status == "skipped":
            continue
        name = str(row.get("name") or "").strip()
        domain = str(row.get("domain") or row.get("company_domain") or "").strip().lower().lstrip("@")
        if not name or not domain:
            continue
        find_result = _checkpoint_row_to_find_result(row)
        if not find_result.get("email") and status not in ("found", "not_found"):
            continue
        company = str(row.get("company") or domain).strip()
        linkedin = str(row.get("linkedin") or row.get("linkedin_url") or "").strip()
        ext = str(row.get("external_id") or row.get("sales_nav_id") or "").strip() or None
        profiles.append(
            build_import_profile(
                full_name=name,
                company=company,
                domain=domain,
                linkedin=linkedin,
                find_result=find_result,
                normalize_linkedin_fn=normalize_linkedin_fn,
                lead_id=_parse_lead_id(row),
                external_id=ext,
            )
        )
    return profiles


def load_profiles_for_om_import(
    path: str,
    *,
    normalize_linkedin_fn: Callable[[str], str],
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Load profiles from batch checkpoint (.csv/.json) or import JSON payload."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    if file_path.suffix.lower() == ".csv":
        with file_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return profiles_from_checkpoint_rows(rows, normalize_linkedin_fn), None

    data = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        profiles = data.get("profiles")
        if isinstance(profiles, list):
            ws = str(data.get("workspace") or "").strip() or None
            return profiles, ws
        raise ValueError("JSON object must contain a profiles array")

    if isinstance(data, list):
        if data and isinstance(data[0], dict) and _is_import_profile_row(data[0]):
            return data, None
        return profiles_from_checkpoint_rows(data, normalize_linkedin_fn), None

    raise ValueError("JSON must be an array of rows or {profiles: [...]}")


def bulk_dedup_map(
    om_dir: Path,
    people: list[dict[str, Any]],
    *,
    workspace: str,
    skill_dir: Path,
    provider_names: list[str],
    indices: Optional[list[int]] = None,
) -> tuple[dict[int, dict[str, Any]], bool]:
    """Return (index → lookup row, lookup_failed). indices maps each people[i] to original row index."""
    items: list[dict[str, Any]] = []
    for i, row in enumerate(people):
        name, domain, _co, linkedin, lead_id = row_fields(row)
        orig_idx = indices[i] if indices is not None else i
        item: dict[str, Any] = {"index": orig_idx}
        if lead_id is not None:
            item["lead_id"] = lead_id
        elif linkedin:
            item["linkedin"] = linkedin
        elif name:
            item["name"] = name
        items.append(item)
    if not items:
        return {}, False
    try:
        payload = cc.run_batch_lead_lookup(om_dir, items, workspace=workspace, skill_dir=skill_dir)
    except RuntimeError as e:
        print(
            f"\n⚠️  OM lead lookup failed: {e}\n"
            "   Dedup disabled — API credits may be wasted on already-resolved leads.\n",
            file=sys.stderr,
        )
        return {}, True
    out: dict[int, dict[str, Any]] = {}
    for entry in payload.get("results") or []:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if idx is None:
            continue
        out[int(idx)] = entry
    return out, False


def skip_resolved_before_api(
    om_dir: Path,
    chunk: list[tuple[int, dict[str, Any]]],
    lookup_by_index: dict[int, dict[str, Any]],
    *,
    workspace: str,
    skill_dir: Path,
    provider_names: list[str],
) -> tuple[list[tuple[int, dict[str, Any]]], dict[int, dict[str, Any]], list[tuple[int, dict[str, Any], str]]]:
    """Fresh OM lookup immediately before API calls; return (api_chunk, lookup, skipped)."""
    if not chunk:
        return chunk, lookup_by_index, []
    rows = [row for _idx, row in chunk]
    indices = [idx for idx, _row in chunk]
    fresh, _failed = bulk_dedup_map(
        om_dir, rows, workspace=workspace, skill_dir=skill_dir,
        provider_names=provider_names, indices=indices,
    )
    lookup_by_index = {**lookup_by_index, **fresh}
    api_chunk: list[tuple[int, dict[str, Any]]] = []
    skipped: list[tuple[int, dict[str, Any], str]] = []
    for idx, row in chunk:
        reason = skip_reason_from_lookup(lookup_by_index.get(idx), provider_names)
        if reason:
            skipped.append((idx, row, reason))
        else:
            api_chunk.append((idx, row))
    return api_chunk, lookup_by_index, skipped


def _record_om_skip(stats: dict[str, Any], reason: str, *, fresh: bool = False) -> None:
    if reason == "has_email":
        stats["skipped_email"] = int(stats.get("skipped_email", 0)) + 1
    else:
        stats["skipped_tagged"] = int(stats.get("skipped_tagged", 0)) + 1
    stats["skipped"] = int(stats.get("skipped", 0)) + 1
    if fresh:
        stats["skipped_fresh_om"] = int(stats.get("skipped_fresh_om", 0)) + 1


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
    for p in provider_names:
        if f"{p}_attempted" in tags:
            return f"{p}_attempted"
    return None


def _resolve_output_base(path: str, input_path: str, *, om_dir: Optional[Path] = None) -> str:
    if path:
        p = Path(path).expanduser()
        if p.suffix in (".csv", ".json"):
            return str(p.with_suffix(""))
        return str(p)
    stem = Path(input_path).expanduser().stem
    export_dir = cc.get_working_export_dir(om_dir) if om_dir else Path.cwd() / "outreachmagic" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return str(export_dir / f"{stem}-email-results")


class IncrementalWriter:
    def __init__(self, output_base: str, *, retry_errors: bool = False) -> None:
        self.output_base = output_base
        self.retry_errors = retry_errors
        self.csv_path = f"{output_base}.csv"
        self.json_path = f"{output_base}.json"
        self.buffer: list[dict[str, Any]] = []
        self.done_keys: set[str] = set()
        self.error_keys: set[str] = set()
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        by_key: dict[str, dict[str, Any]] = {}

        def _merge_row(row: dict[str, Any]) -> None:
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
                    print(
                        f"⚠️  CSV header mismatch in {self.csv_path}:",
                        f"expected '{BATCH_CSV_COLUMNS[0]}' but got "
                        f"{first_line.strip()[:60]!r}. The file may have been",
                        "modified by an external editor. Reading rows anyway.",
                        file=sys.stderr,
                    )
                    fh.seek(0)
                    reader = csv.DictReader(fh, fieldnames=BATCH_CSV_COLUMNS)
                    next(reader, None)  # skip the corrupted header row
                    for row in reader:
                        _merge_row(row)
                else:
                    fh.seek(0)
                    reader = csv.DictReader(fh)
                    if reader.fieldnames and BATCH_CSV_COLUMNS[0] in reader.fieldnames:
                        for row in reader:
                            _merge_row(row)

        self.buffer = list(by_key.values())
        self.done_keys = set()
        self.error_keys = set()
        for key, row in by_key.items():
            if checkpoint_row_is_complete(row):
                self.done_keys.add(key)
            else:
                self.error_keys.add(key)
                if self.retry_errors:
                    continue
                self.done_keys.add(key)

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
            complete = checkpoint_row_is_complete(row_dict)
            if resume_key in self.done_keys and complete:
                return
            if complete:
                self.done_keys.add(resume_key)
                self.error_keys.discard(resume_key)
            else:
                self.error_keys.add(resume_key)
                self.done_keys.discard(resume_key)
            existing = next((i for i, r in enumerate(self.buffer) if r.get("resume_key") == resume_key), None)
            if existing is not None:
                self.buffer[existing] = row_dict
            else:
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

    def _rewrite_csv(self) -> None:
        with open(self.csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(BATCH_CSV_COLUMNS)
            for row_dict in self.buffer:
                writer.writerow([row_dict.get(c, "") for c in BATCH_CSV_COLUMNS])
            fh.flush()
            os.fsync(fh.fileno())

    def finalize(self) -> None:
        by_key: dict[str, dict[str, Any]] = {}
        for row_dict in self.buffer:
            key = str(row_dict.get("resume_key") or "").strip()
            if not key:
                continue
            prev = by_key.get(key)
            if prev is None or checkpoint_row_is_complete(row_dict):
                by_key[key] = row_dict
        self.buffer = list(by_key.values())
        if self.buffer:
            self._rewrite_csv()
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

    provider_names = prompt_batch_provider_plan(
        cfg, cli_provider=opts.provider, yes=opts.yes, dry_run=opts.dry_run,
    )
    if not provider_names:
        return {"error": "no provider configured (check API keys and --provider)"}
    if opts.provider:
        missing = [p for p in provider_names if p not in _providers_with_keys(cfg, [p])]
        if missing:
            label = missing[0]
            return {
                "error": (
                    f"{label} API key not configured. "
                    "Add it at app.outreachmagic.io → Settings, then run: "
                    "pipeline.py sync-secrets --check"
                ),
            }
    keyed = _providers_with_keys(cfg, provider_names)
    if keyed:
        provider_names = [p for p in provider_names if p in keyed]
    if not provider_names:
        return {"error": "no provider API keys configured (run pipeline.py sync-secrets --check)"}

    provider_label = "+".join(provider_names)
    output_base = _resolve_output_base(
        opts.output_base or (str(Path(opts.output_csv).with_suffix("")) if opts.output_csv else ""),
        input_path,
        om_dir=om_dir,
    )
    writer: Optional[IncrementalWriter] = (
        IncrementalWriter(output_base, retry_errors=opts.retry_errors) if output_base else None
    )

    lookup_by_index: dict[int, dict[str, Any]] = {}
    dedup_lookup_failed = False
    if om_dir and not opts.skip_om:
        lookup_by_index, dedup_lookup_failed = bulk_dedup_map(
            om_dir, people, workspace=opts.workspace, skill_dir=skill_dir, provider_names=provider_names,
        )

    to_process: list[tuple[int, dict[str, Any]]] = []
    pre_skipped: dict[int, dict[str, Any]] = {}
    skipped_email = skipped_tagged = skipped_resume = 0
    skipped_names: list[str] = []
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
            if reason == "has_email":
                skipped_email += 1
            else:
                skipped_tagged += 1
            pre_skipped[i] = {
                "batch_status": "skipped",
                "status": "skipped",
                "skip_reason": reason,
                "name": name,
            }
            if name and reason == "has_email":
                skipped_names.append(name)
            continue
        if writer and lead_resume_key(row, index=i) in writer.done_keys:
            resume_reason = skip_reason_from_lookup(lookup_by_index.get(i), provider_names)
            if resume_reason == "has_email":
                skipped_email += 1
                pre_skipped[i] = {
                    "batch_status": "skipped",
                    "status": "skipped",
                    "skip_reason": resume_reason,
                }
            else:
                skipped_resume += 1
                pre_skipped[i] = {
                    "batch_status": "skipped",
                    "status": "skipped",
                    "skip_reason": "resume_done",
                }
            continue
        to_process.append((i, row))

    resume_done = len(writer.done_keys) if writer else 0
    checkpoint_errors = len(writer.error_keys) if writer else 0
    if writer and checkpoint_errors and not opts.retry_errors and not opts.dry_run:
        print(
            f"\n⚠️  Checkpoint has {checkpoint_errors} row(s) with errors from a prior run. "
            f"They will be skipped. Re-run with --retry-errors to re-attempt, "
            f"or delete {writer.csv_path} to start fresh.\n",
            file=sys.stderr,
        )
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
            credits_max=len(to_process),
        )
        return {
            "dry_run": True,
            "to_process": len(to_process),
            "skipped_email": skipped_email,
            "skipped_tagged": skipped_tagged,
            "skipped_resume": skipped_resume,
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

    skipped_total = skipped_email + skipped_tagged + skipped_resume
    print_preflight_summary(
        total=len(people),
        to_process=len(to_process),
        skipped_total=skipped_total,
        skipped_names=skipped_names,
    )
    if to_process and not prompt_batch_confirm(
        to_process=len(to_process),
        skipped_total=skipped_total,
        provider_label=provider_label,
        yes=opts.yes,
        dry_run=False,
    ):
        print(
            f"\nAbout to run {len(to_process)} API lookups ({provider_label}). Pass --yes to confirm.\n",
            file=sys.stderr,
        )
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
        "skipped": skipped_email + skipped_tagged + skipped_resume,
        "skipped_email": skipped_email,
        "skipped_tagged": skipped_tagged,
        "skipped_resume": skipped_resume,
        "skipped_fresh_om": 0,
        "dedup_lookup_failed": dedup_lookup_failed,
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
    auth_resync_lock = threading.Lock()
    auth_resync_attempted = False

    def _maybe_resync_on_auth(result: dict[str, Any]) -> bool:
        nonlocal auth_resync_attempted, api_providers
        if str(result.get("status") or "") != "auth_error":
            return False
        with auth_resync_lock:
            if auth_resync_attempted:
                return False
            auth_resync_attempted = True
        print(
            "\n⚠️  Auth error — refreshing API keys from portal (sync-secrets)...\n",
            file=sys.stderr,
        )
        if not cc.maybe_sync_secrets_from_portal(skill_dir=skill_dir, quiet=False):
            return False
        _reload_cfg_provider_keys(cfg, provider_names)
        api_providers = _api_providers_from_cfg(cfg, provider_names)
        return True

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
        if om_dir and not opts.skip_om:
            chunk, lookup_by_index, fresh_skips = skip_resolved_before_api(
                om_dir,
                chunk,
                lookup_by_index,
                workspace=opts.workspace,
                skill_dir=skill_dir,
                provider_names=provider_names,
            )
            for idx, _row, reason in fresh_skips:
                results[idx] = {
                    "batch_status": "skipped",
                    "status": "skipped",
                    "skip_reason": reason,
                    "provider_attempts": [],
                }
                _record_om_skip(stats, reason, fresh=True)
        if not chunk:
            continue
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            futures = {pool.submit(_work, item): item for item in chunk}
            for fut in as_completed(futures):
                item = futures[fut]
                idx, result = fut.result()
                if _maybe_resync_on_auth(result):
                    idx, result = _work(item)
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
    import_status: dict[str, Any] = {"reason": "not_attempted"}
    verified = 0
    import_created = 0
    csv_hint = f"{output_base}.csv" if output_base else "<checkpoint.csv>"
    json_hint = f"{output_base}.json" if output_base else "<checkpoint.json>"

    if opts.no_save:
        import_status = {"reason": "no_save", "recovery_hint": ""}
    elif opts.skip_om:
        import_status = {"reason": "skip_om", "recovery_hint": ""}
    elif not om_dir:
        import_status = {"reason": "no_om", "recovery_hint": ""}
    else:
        checkpoint_rows = writer.buffer if writer else None
        profiles, profile_source = resolve_profiles_for_import(
            people,
            results,
            normalize_linkedin_fn,
            lookup_by_index=lookup_by_index,
            checkpoint_rows=checkpoint_rows,
        )
        if not profiles:
            import_status = {
                "reason": "no_profiles",
                "recovery_hint": (
                    f"python3 scripts/email_finder.py import-to-om --file {csv_hint}"
                    f" --workspace {opts.workspace or 'WORKSPACE'}"
                ),
            }
            print(
                f"\n⚠️  0 profiles to import — skipping OM save "
                f"(results on disk: {csv_hint})\n",
                file=sys.stderr,
            )
        elif not opts.workspace:
            save_out = {"error": "workspace required for OM save (--workspace)"}
            import_status = {
                "reason": "no_workspace",
                "error": save_out["error"],
                "recovery_hint": (
                    f"python3 scripts/email_finder.py import-to-om --file {csv_hint}"
                    f" --workspace WORKSPACE"
                ),
            }
            print(
                "\n❌ Outreach Magic save skipped: --workspace is required.\n"
                "   Email results are in CSV/JSON on disk.\n",
                file=sys.stderr,
            )
        else:
            import_status = {"reason": "pending", "source": profile_source}
            print(
                f"\n  Importing {len(profiles)} profile(s) to workspace {opts.workspace}"
                f" ({profile_source})...",
                flush=True,
            )
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
                print(
                    f"  Imported: {len(profiles)} profile(s), verified {verified} record(s)",
                    flush=True,
                )
                import_status = {
                    "reason": "success",
                    "source": profile_source,
                    "imported_count": len(profiles),
                    "verified_count": verified,
                    "import_created": import_created,
                }
            except Exception as e:
                save_out = {"error": str(e), "imported": 0}
                import_status = {
                    "reason": "failed",
                    "error": str(e),
                    "source": profile_source,
                    "recovery_hint": (
                        f"python3 scripts/email_finder.py import-to-om --file {csv_hint}"
                        f" --workspace {opts.workspace}"
                    ),
                }
                cc.print_import_failure_recovery(
                    e,
                    skill="email-finder/batch-find",
                    data_paths=[csv_hint, json_hint],
                    recovery_lines=[
                        "Re-sync to OM:",
                        f"python3 scripts/email_finder.py import-to-om --file {csv_hint}"
                        f" --workspace {opts.workspace}",
                        f"python3 scripts/email_finder.py import-to-om --file {json_hint}"
                        f" --workspace {opts.workspace}",
                        "Or re-run batch-find (resume skips completed API rows).",
                    ],
                )

    elapsed = time.time() - start
    auth_n = int(stats.get("auth_errors", 0))
    if auth_n:
        providers = sorted(
            {
                str(r.get("provider") or "")
                for r in results
                if isinstance(r, dict) and str(r.get("status") or "") == "auth_error"
            }
            - {""}
        )
        label = ", ".join(providers) if providers else "provider"
        resync_note = (
            "   sync-secrets ran automatically after the first auth error.\n"
            if auth_resync_attempted
            else "   Run: pipeline.py sync-secrets --check to verify portal key status.\n"
        )
        print(
            f"\n⚠  {label} auth error on {auth_n} lead(s) — API key may be invalid or expired.\n"
            f"{resync_note}",
            file=sys.stderr,
        )
    print_final_summary(
        stats,
        elapsed,
        output_base,
        provider=provider_label,
        import_status=import_status,
        skipped_names=skipped_names,
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
    elif st == "auth_error":
        stats["auth_errors"] = int(stats.get("auth_errors", 0)) + 1
    elif st in ("error", "http_error") or result.get("error"):
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
    credits = int(result.get("credits_used") or 0)
    if credits:
        stats["credits_used"] = int(stats.get("credits_used", 0)) + credits
    record_api_calls(stats, result)
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
