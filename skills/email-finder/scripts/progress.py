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


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def print_progress(
    done: int,
    total: int,
    stats: dict[str, Any],
    start_time: float,
    *,
    provider: str = "",
    file=sys.stderr,
) -> None:
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
    credits = float(stats.get("credits_used", 0) or 0)

    print(file=file)
    print("═" * 60, file=file)
    print(f" PROGRESS: {done}/{total} leads ({pct:.1f}%)  elapsed: {elapsed_str}", file=file)
    print("─" * 60, file=file)
    print(f" Found:      {stats.get('found', 0):>5}  ({hit_rate:.1f}% hit rate)  {bar}", file=file)
    print(f" Not found:  {stats.get('not_found', 0):>5}", file=file)
    err_n = stats.get("errors", 0)
    print(f" Errors:     {err_n:>5}", file=file)
    print("─" * 60, file=file)
    print(f" Rate:       {rate:.2f}/s  ETA: {eta_str}", file=file)
    print(f" Credits:    {credits:.3f} used", file=file)
    if provider:
        print(f" Provider:   {provider}", file=file)
    print("═" * 60, file=file)
    print(file=file)


def print_final_summary(
    stats: dict[str, Any],
    elapsed: float,
    output_base: str,
    *,
    provider: str = "",
    imported_count: int = 0,
    verified_count: int = 0,
    file=sys.stderr,
) -> None:
    total = stats.get("found", 0) + stats.get("not_found", 0) + stats.get("errors", 0)
    denom = max(stats.get("found", 0) + stats.get("not_found", 0), 1)
    hit_rate = stats.get("found", 0) / denom * 100
    elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
    speed = total / max(elapsed, 0.001)
    credits = float(stats.get("credits_used", 0) or 0)

    print(file=file)
    print("╔" + "═" * 54 + "╗", file=file)
    print(f"║{'EMAIL FINDER — COMPLETE':^54}║", file=file)
    print("╠" + "═" * 54 + "╣", file=file)
    print(f"║  Total processed:  {total:<33}║", file=file)
    print(f"║  Found:            {stats.get('found', 0):<5} ({hit_rate:.1f}% hit rate){'':>22}║", file=file)
    print(f"║  Not found:        {stats.get('not_found', 0):<5}{'':>32}║", file=file)
    print(f"║  Errors:           {stats.get('errors', 0):<5}{'':>32}║", file=file)
    print(f"║{'':54}║", file=file)
    if provider:
        print(f"║  Provider:         {provider:<40}║", file=file)
    print(f"║  Credits used:     {credits:.3f}{'':>32}║", file=file)
    print(f"║  Time elapsed:     {elapsed_str:<40}║", file=file)
    print(f"║  Average speed:    {speed:.2f} leads/s{'':>24}║", file=file)
    if output_base:
        print(f"║{'':54}║", file=file)
        print(f"║  CSV saved:        {output_base}.csv{'':>31}║", file=file)
        print(f"║  JSON saved:       {output_base}.json{'':>31}║", file=file)
    if imported_count:
        print(f"║  Imported to OM:   {imported_count} leads{'':>24}║", file=file)
    if verified_count:
        print(f"║  Verified:         {verified_count} record(s){'':>24}║", file=file)
    print("╚" + "═" * 54 + "╝", file=file)
    print(file=file)


def print_om_setup_box(file=sys.stderr) -> None:
    print(file=file)
    print("╔" + "═" * 54 + "╗", file=file)
    print(f"║{'OUTREACHMAGIC — NOT CONNECTED':^54}║", file=file)
    print("╠" + "═" * 54 + "╣", file=file)
    print("║  OutreachMagic stores leads locally and prevents       ║", file=file)
    print("║  double-paying for email enrichment.                 ║", file=file)
    print("║                                                      ║", file=file)
    print("║  Setup:                                              ║", file=file)
    print("║    bash install.sh --platform hermes                 ║", file=file)
    print("║    python3 pipeline.py login                         ║", file=file)
    print("║                                                      ║", file=file)
    print("║  Or run with --skip-om (CSV/JSON only, no dedup).    ║", file=file)
    print("╚" + "═" * 54 + "╝", file=file)
    print(file=file)


def print_dry_run_box(
    *,
    to_process: int,
    skipped_email: int,
    skipped_tagged: int,
    estimated_credits: float,
    provider: str,
    workers: int,
    file=sys.stderr,
) -> None:
    print(file=file)
    print("╔" + "═" * 54 + "╗", file=file)
    print(f"║{'EMAIL FINDER — DRY RUN':^54}║", file=file)
    print("╠" + "═" * 54 + "╣", file=file)
    print(f"║  New lookups:       {to_process:<33}║", file=file)
    print(f"║  Skipped (email):    {skipped_email:<33}║", file=file)
    print(f"║  Skipped (tagged):   {skipped_tagged:<33}║", file=file)
    print(f"║  Est. credits:      ~{estimated_credits:.3f}{'':>28}║", file=file)
    print(f"║  Provider:          {provider:<33}║", file=file)
    print(f"║  Workers:           {workers:<33}║", file=file)
    print("║  Run without --dry-run to proceed.                   ║", file=file)
    print("╚" + "═" * 54 + "╝", file=file)
    print(file=file)
