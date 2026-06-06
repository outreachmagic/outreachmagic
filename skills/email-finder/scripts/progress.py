"""Terminal progress and summary output for email-finder batch runs."""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

_USE_COLOR = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(text: str) -> str:
    return _c("32", text)


def _api_calls_line(stats: dict[str, Any]) -> str:
    calls = stats.get("api_calls") or {}
    if not isinstance(calls, dict):
        return "API calls: 0"
    total = sum(int(v) for v in calls.values())
    parts = ", ".join(f"{k}: {int(v)}" for k, v in sorted(calls.items()) if int(v))
    return f"API calls: {total} total ({parts})" if parts else f"API calls: {total}"


def _init_stats(stats: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    base = stats or {}
    base.setdefault("found", 0)
    base.setdefault("not_found", 0)
    base.setdefault("errors", 0)
    base.setdefault("rate_limited", 0)
    base.setdefault("timeout", 0)
    base.setdefault("api_calls", {})
    base.setdefault("verify", {"valid": 0, "catch_all": 0, "invalid": 0, "unknown": 0})
    base.setdefault("waterfall", {})
    return base


def _provider_api_lines(stats: dict[str, Any]) -> list[str]:
    calls = stats.get("api_calls") or {}
    if not isinstance(calls, dict):
        return []
    lines: list[str] = []
    for pname in sorted(calls.keys()):
        n = int(calls.get(pname, 0))
        lines.append(f"{pname} API calls:  {n}")
    return lines


def record_api_calls(stats: dict[str, Any], result: dict[str, Any]) -> None:
    _init_stats(stats)
    calls = stats["api_calls"]
    attempts = result.get("provider_attempts")
    if isinstance(attempts, list) and attempts:
        for att in attempts:
            if not isinstance(att, dict) or not att.get("attempted", True):
                continue
            p = str(att.get("provider") or "")
            if p:
                calls[p] = int(calls.get(p, 0)) + 1
    else:
        p = str(result.get("provider") or "")
        if p and result.get("status") not in ("skipped",):
            calls[p] = int(calls.get(p, 0)) + 1


def record_verify_status(stats: dict[str, Any], validity: str, provider: str) -> None:
    from providers import validity_to_verify_status

    _init_stats(stats)
    v = stats["verify"]
    status = validity_to_verify_status(validity, provider=provider)
    key = status if status in v else "unknown"
    v[key] = int(v.get(key, 0)) + 1


def print_progress(
    done: int,
    total: int,
    stats: dict[str, Any],
    start_time: float,
    *,
    provider: str = "",
    file=sys.stderr,
) -> None:
    stats = _init_stats(stats)
    elapsed = max(time.time() - start_time, 0.001)
    rate = done / elapsed
    eta = (total - done) / rate if rate > 0 else 0
    pct = (done / total * 100) if total else 0
    hit_rate = (stats.get("found", 0) / done * 100) if done else 0
    bar_len = 20
    filled = int(bar_len * stats.get("found", 0) / max(done, 1))
    bar = "█" * filled + "░" * (bar_len - filled)
    elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
    eta_str = f"{int(eta // 60)}m {int(eta % 60)}s" if eta > 0 else "—"

    print(file=file)
    print("═" * 60, file=file)
    print(f" PROGRESS: {done}/{total} leads ({pct:.1f}%)  elapsed: {elapsed_str}", file=file)
    print("─" * 60, file=file)
    print(f" Found:      {stats.get('found', 0):>5}  ({hit_rate:.1f}% hit rate)  {bar}", file=file)
    print(f" Not found:  {stats.get('not_found', 0):>5}", file=file)
    print(f" Errors:     {stats.get('errors', 0):>5}", file=file)
    if stats.get("rate_limited"):
        print(f" Rate limit: {stats.get('rate_limited', 0):>5}", file=file)
    if stats.get("timeout"):
        print(f" Timeouts:   {stats.get('timeout', 0):>5}", file=file)
    print("─" * 60, file=file)
    print(f" {_api_calls_line(stats)}", file=file)
    print(f" Rate:       {rate:.2f}/s  ETA: {eta_str}", file=file)
    if provider:
        print(f" Provider:   {provider}", file=file)
    print("═" * 60, file=file)
    print(file=file)


def _import_status_lines(
    import_status: dict[str, Any],
    *,
    output_base: str,
    verify: dict[str, Any],
) -> list[str]:
    reason = str(import_status.get("reason") or "not_attempted")
    lines: list[str] = []
    if reason == "success":
        imported = int(import_status.get("imported_count") or 0)
        verified = int(import_status.get("verified_count") or 0)
        valid_n = int(verify.get("valid", 0))
        catch_n = int(verify.get("catch_all", 0))
        detail = f"{imported} leads"
        if valid_n or catch_n:
            detail = f"{imported} leads ({valid_n} valid, {catch_n} catch_all)"
        lines.append(f"Imported to OM:  {detail}")
        if verified:
            lines.append(f"Verified:        {verified} record(s)")
        created = int(import_status.get("import_created") or 0)
        if created:
            lines.append(f"⚠ New leads created: {created} (expected 0)")
        source = str(import_status.get("source") or "")
        if source == "from_checkpoint":
            lines.append("Source:          checkpoint CSV/JSON")
    elif reason == "no_save":
        lines.append("⚠ No import (--no-save); CSV/JSON saved to disk")
    elif reason == "skip_om":
        lines.append("⚠ No import (--skip-om); CSV/JSON saved to disk")
    elif reason == "no_om":
        lines.append("⚠ No import (OutreachMagic not connected)")
    elif reason == "no_workspace":
        lines.append("⚠ No import (--workspace required)")
    elif reason == "no_profiles":
        lines.append("⚠ No import performed (0 profiles to save)")
    elif reason == "failed":
        err = str(import_status.get("error") or "unknown error")[:48]
        lines.append(f"⚠ Import failed: {err}")
    elif reason == "not_attempted":
        lines.append("⚠ No import performed")
    else:
        lines.append(f"⚠ No import ({reason})")
    hint = str(import_status.get("recovery_hint") or "").strip()
    if hint and reason in ("no_profiles", "no_workspace", "failed", "not_attempted"):
        lines.append(f"Recovery:        {hint[:48]}")
    elif hint and output_base and reason in ("no_profiles", "failed"):
        lines.append(f"Recovery:        import-to-om --file {output_base}.csv")
    return lines


def print_final_summary(
    stats: dict[str, Any],
    elapsed: float,
    output_base: str,
    *,
    provider: str = "",
    imported_count: int = 0,
    verified_count: int = 0,
    import_created: int = 0,
    import_status: Optional[dict[str, Any]] = None,
    file=sys.stderr,
) -> None:
    stats = _init_stats(stats)
    total = stats.get("found", 0) + stats.get("not_found", 0) + stats.get("errors", 0)
    denom = max(stats.get("found", 0) + stats.get("not_found", 0), 1)
    hit_rate = stats.get("found", 0) / denom * 100
    elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
    speed = total / max(elapsed, 0.001)
    verify = stats.get("verify") or {}
    found_n = stats.get("found", 0)

    print(file=file)
    print("╔" + "═" * 62 + "╗", file=file)
    print(f"║{'EMAIL FINDER — COMPLETE':^62}║", file=file)
    print("╠" + "═" * 62 + "╣", file=file)
    print(f"║  Total processed:  {total:<44}║", file=file)
    print(f"║{'':62}║", file=file)
    print(f"║  FIND RESULTS{'':49}║", file=file)
    print(f"║    Found:          {stats.get('found', 0):<5} ({hit_rate:.1f}%){'':>30}║", file=file)
    print(f"║    Not found:      {stats.get('not_found', 0):<44}║", file=file)
    print(f"║    Errors:         {stats.get('errors', 0):<44}║", file=file)
    if stats.get("rate_limited"):
        print(f"║    Rate limited:   {stats.get('rate_limited', 0):<44}║", file=file)
    if stats.get("timeout"):
        print(f"║    Timeouts:       {stats.get('timeout', 0):<44}║", file=file)
    provider_lines = _provider_api_lines(stats)
    if provider_lines:
        print(f"║{'':62}║", file=file)
        print(f"║  PROVIDER{'':53}║", file=file)
        for line in provider_lines:
            print(f"║    {line:<58}║", file=file)
    if provider:
        print(f"║    mode:            {provider:<44}║", file=file)
    if found_n:
        print(f"║{'':62}║", file=file)
        print(f"║  VERIFICATION (of found){'':37}║", file=file)
        for key in ("valid", "catch_all", "invalid", "unknown"):
            n = int(verify.get(key, 0))
            pct = n / found_n * 100 if found_n else 0
            print(f"║    {key:<12} {n:>5}  ({pct:.1f}%){'':>28}║", file=file)
    wf = stats.get("waterfall") or {}
    if wf and len(wf) > 1:
        print(f"║{'':62}║", file=file)
        print(f"║  WATERFALL{'':52}║", file=file)
        for line in _waterfall_lines(wf):
            print(f"║    {line:<58}║", file=file)
    print(f"║  Time elapsed:     {elapsed_str:<47}║", file=file)
    print(f"║  Average speed:    {speed:.2f} leads/s{'':>31}║", file=file)
    skipped_email = int(stats.get("skipped_email", 0))
    skipped_tagged = int(stats.get("skipped_tagged", 0))
    skipped_resume = int(stats.get("skipped_resume", 0))
    skipped_mid = int(stats.get("skipped_mid_batch", 0))
    if skipped_email or skipped_tagged or skipped_resume or skipped_mid:
        print(f"║{'':62}║", file=file)
        print(f"║  SKIPPED{'':54}║", file=file)
        if skipped_email:
            print(f"║    Already has email:  {skipped_email:<38}║", file=file)
        if skipped_tagged:
            print(f"║    Already attempted:   {skipped_tagged:<37}║", file=file)
        if skipped_resume:
            print(f"║    Resume (checkpoint): {skipped_resume:<37}║", file=file)
        if skipped_mid:
            print(f"║    Mid-batch (fresh OM): {skipped_mid:<36}║", file=file)
    if output_base:
        print(f"║{'':62}║", file=file)
        print(f"║  OUTPUT{'':55}║", file=file)
        print(f"║    CSV:             {output_base}.csv{'':>30}║", file=file)
        print(f"║    JSON:            {output_base}.json{'':>29}║", file=file)
    status = import_status or {}
    if not status and imported_count:
        status = {
            "reason": "success",
            "imported_count": imported_count,
            "verified_count": verified_count,
            "import_created": import_created,
        }
    if status:
        print(f"║{'':62}║", file=file)
        print(f"║  IMPORT{'':55}║", file=file)
        for line in _import_status_lines(status, output_base=output_base, verify=verify):
            print(f"║    {line[:58]:<58}║", file=file)
    print("╚" + "═" * 62 + "╝", file=file)
    print(file=file)


def _waterfall_lines(wf: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for p, data in sorted(wf.items()):
        if not isinstance(data, dict):
            continue
        calls = int(data.get("calls", 0))
        found = int(data.get("found", 0))
        nf = int(data.get("not_found", 0))
        err = int(data.get("errors", 0))
        lines.append(f"{p}: {calls} calls, {found} found, {nf} miss, {err} err")
    return lines


def print_om_setup_box(file=sys.stderr) -> None:
    print(file=file)
    print("╔" + "═" * 62 + "╗", file=file)
    print(f"║{'OUTREACHMAGIC — NOT CONNECTED':^62}║", file=file)
    print("╠" + "═" * 62 + "╣", file=file)
    print("║  This tool needs OutreachMagic to check if a lead already has  ║", file=file)
    print("║  an email — this prevents double-paying for enrichment.        ║", file=file)
    print("║                                                              ║", file=file)
    print("║  Tell your AI assistant:                                     ║", file=file)
    print("║  ┌────────────────────────────────────────────────────────┐  ║", file=file)
    print("║  │ Install OutreachMagic and set it up, then re-run       │  ║", file=file)
    print("║  │ email_finder.py batch-find. Install from:              │  ║", file=file)
    print("║  │ https://github.com/outreachmagic/outreachmagic           │  ║", file=file)
    print("║  │ 1. Clone or run install.sh --platform hermes             │  ║", file=file)
    print("║  │ 2. Create account at https://app.outreachmagic.io        │  ║", file=file)
    print("║  │ 3. Run pipeline.py login to connect                      │  ║", file=file)
    print("║  │ 4. Re-run batch-find with --workspace and --yes          │  ║", file=file)
    print("║  └────────────────────────────────────────────────────────┘  ║", file=file)
    print("║  Or run with --skip-om (CSV/JSON only, no dedup).             ║", file=file)
    print("╚" + "═" * 62 + "╝", file=file)
    print(file=file)


def print_dry_run_box(
    *,
    to_process: int,
    skipped_email: int,
    skipped_tagged: int,
    provider: str,
    workers: int,
    health_lines: Optional[list[str]] = None,
    resume_done: int = 0,
    missing_om_match: int = 0,
    file=sys.stderr,
) -> None:
    print(file=file)
    print("╔" + "═" * 62 + "╗", file=file)
    print(f"║{'EMAIL FINDER — DRY RUN':^62}║", file=file)
    print("╠" + "═" * 62 + "╣", file=file)
    if resume_done:
        print(f"║  Resuming: already done {resume_done}, new {to_process}{'':>28}║", file=file)
    print(f"║  New lookups:       {to_process:<44}║", file=file)
    print(f"║  Skipped (email):    {skipped_email:<44}║", file=file)
    print(f"║  Skipped (tagged):   {skipped_tagged:<44}║", file=file)
    print(f"║  Provider:          {provider:<44}║", file=file)
    print(f"║  Workers:           {workers:<44}║", file=file)
    if missing_om_match:
        print(
            f"║  ⚠ No lead_id/linkedin: {missing_om_match} rows (may create dupes){'':>8}║",
            file=file,
        )
    if health_lines:
        print(f"║{'':62}║", file=file)
        print(f"║  Health:{'':54}║", file=file)
        for line in health_lines:
            print(f"║    {line[:58]:<58}║", file=file)
    print(f"║  Run without --dry-run to proceed.{'':>28}║", file=file)
    print("╚" + "═" * 62 + "╝", file=file)
    print(file=file)


def print_resume_banner(done: int, new_count: int, total: int, file=sys.stderr) -> None:
    print(file=file)
    print(f"Resuming from previous run:", file=file)
    print(f"  Already done:  {done} leads (loaded from CSV/JSON)", file=file)
    print(f"  New:           {new_count} leads", file=file)
    print(f"  Total:         {total}", file=file)
    print(file=file)


def print_mv_summary(stats: dict[str, Any], *, title: str, file=sys.stderr) -> None:
    print(file=file)
    print("╔" + "═" * 62 + "╗", file=file)
    print(f"║{title:^62}║", file=file)
    print("╠" + "═" * 62 + "╣", file=file)
    for key, val in stats.items():
        print(f"║  {str(key):<20} {str(val):<40}║", file=file)
    print("╚" + "═" * 62 + "╝", file=file)
    print(file=file)
