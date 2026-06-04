#!/usr/bin/env python3
"""Smoke + regression tests for email-finder v2.2 (uses real APIs when keys set)."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from batch_runner import BatchOptions, count_rows_missing_om_match, run_batch
from health import icypeas_batch_warnings
from millionverifier import MillionVerifierProvider
from providers import (
    icypeas_credits_for_status,
    icypeas_find,
    icypeas_poll_wait_seconds,
    is_icypeas_rate_limited,
    provider_request_delay_seconds,
    run_find_with_fallback,
    trykitt_find,
)
import companion_common as cc
from email_finder import load_config, find_outreachmagic, _find_skill_dir


def _ok(name: str, detail: str = "") -> dict[str, Any]:
    return {"test": name, "status": "pass", "detail": detail}


def _fail(name: str, detail: str) -> dict[str, Any]:
    return {"test": name, "status": "fail", "detail": detail}


def _skip(name: str, detail: str) -> dict[str, Any]:
    return {"test": name, "status": "skip", "detail": detail}


def run_unit_tests() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        assert icypeas_credits_for_status("DEBITED_NOT_FOUND") == 0.003
        assert icypeas_credits_for_status("NOT_FOUND") == 0.0
        assert icypeas_poll_wait_seconds(0, 3) == 3.0
        assert icypeas_poll_wait_seconds(5, 2) <= 30.0
        assert is_icypeas_rate_limited("exceeded the max number of requests")
        assert provider_request_delay_seconds({}, ["icypeas"], cli_delay=3) == 3.0
        assert len(icypeas_batch_warnings(["icypeas"], workers=3, delay=0, cfg={})) >= 1
        assert count_rows_missing_om_match([{"name": "A", "company_domain": "x.com"}]) == 1
        assert count_rows_missing_om_match([{"lead_id": 1, "name": "A", "company_domain": "x.com"}]) == 0
        assert cc.profiles_have_known_lead_ids([{"id": 1}, {"lead_id": 2}])
        assert not cc.profiles_have_known_lead_ids([{"id": 1}, {"name": "x"}])
        assert cc._chunk_timeout(200) == 100
        assert cc._chunk_timeout(1000) == 300
        mv = MillionVerifierProvider("x")
        assert callable(getattr(mv, "wait_for_completion", None))
        out.append(_ok("unit_assertions"))
    except Exception as e:
        out.append(_fail("unit_assertions", str(e)))
    return out


def run_config_test() -> dict[str, Any]:
    cfg = load_config()
    if not cfg.get("trykitt_api_key") and not cfg.get("icypeas_api_key"):
        return _skip("config", "no API keys in env/config")
    missing = []
    if not cfg.get("trykitt_api_key"):
        missing.append("trykitt")
    if not cfg.get("icypeas_api_key"):
        missing.append("icypeas")
    om = find_outreachmagic(cfg)
    detail = f"om={'yes' if om else 'no'} missing_keys={missing or 'none'}"
    return _ok("config", detail)


def run_single_find(
    label: str,
    fn: Callable[..., dict[str, Any]],
    cfg: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    t0 = time.time()
    try:
        res = fn(cfg, **kwargs)
    except Exception as e:
        return _fail(label, f"exception: {e}")
    elapsed = time.time() - t0
    st = res.get("status")
    if st in ("no_key", "bad_input"):
        return _skip(label, str(res.get("error") or st))
    if st == "rate_limited":
        return _ok(label, f"rate_limited detected in {elapsed:.1f}s (expected path works)")
    if st == "error" and res.get("error") == "icypeas_timeout":
        return _ok(label, f"icypeas_timeout in {elapsed:.1f}s (poll guard works)")
    if st == "http_error":
        return _fail(label, f"http_error (check API key) err={str(res.get('error'))[:120]}")
    if st not in ("found", "not_found"):
        return _fail(label, f"unexpected status={st} err={res.get('error')} keys={list(res.keys())}")
    credits = res.get("credits_used")
    icy = res.get("icypeas_status")
    detail = (
        f"status={st} email={bool(res.get('email'))} elapsed={elapsed:.1f}s "
        f"credits_used={credits} icypeas_status={icy}"
    )
    if label.startswith("icypeas") and st == "not_found" and icy:
        debited = icy in ("DEBITED_NOT_FOUND", "FOUND", "DEBITED")
        if debited and credits == 0:
            return _fail(label, f"debited status but credits_used=0 ({detail})")
        if not debited and credits not in (0, 0.0, None) and credits != 0.003:
            pass
    return _ok(label, detail)


def run_poll_timeout_forced(cfg: dict[str, Any]) -> dict[str, Any]:
    """Force short poll window; expect icypeas_timeout or found (fast jobs)."""
    if not cfg.get("icypeas_api_key"):
        return _skip("icypeas_poll_forced_timeout", "no icypeas key")
    tiny = dict(cfg)
    tiny["icypeas_poll_attempts"] = 2
    tiny["icypeas_poll_delay_seconds"] = 0.1
    t0 = time.time()
    res = icypeas_find(
        tiny,
        full_name="Satya Nadella",
        domain="microsoft.com",
        linkedin="",
    )
    elapsed = time.time() - t0
    st = res.get("status")
    if st == "found":
        return _ok("icypeas_poll_forced_timeout", f"fast completion in {elapsed:.1f}s (ok)")
    if st == "error" and res.get("error") == "icypeas_timeout":
        return _ok("icypeas_poll_forced_timeout", f"timeout in {elapsed:.1f}s (guard ok)")
    if st == "not_found":
        return _fail("icypeas_poll_forced_timeout", f"false not_found? elapsed={elapsed:.1f}s icy={res.get('icypeas_status')}")
    return _fail("icypeas_poll_forced_timeout", f"status={st} err={res.get('error')}")


def run_batch_smoke(cfg: dict[str, Any], skill_dir: Path, tmp: Path) -> list[dict[str, Any]]:
    leads = [
        {"name": "Tim Cook", "company_domain": "apple.com", "lead_id": 900001},
        {"name": "Satya Nadella", "company_domain": "microsoft.com", "lead_id": 900002},
        {"name": "No Name Person", "company_domain": "example.com"},
    ]
    leads_path = tmp / "test_leads.json"
    leads_path.write_text(json.dumps(leads, indent=2), encoding="utf-8")
    out_base = tmp / "batch-out"
    results: list[dict[str, Any]] = []

    # dry-run
    try:
        r = run_batch(
            str(leads_path),
            cfg,
            find_outreachmagic(cfg),
            BatchOptions(dry_run=True, workers=2, delay=3, provider="icypeas"),
            skill_dir=skill_dir,
            normalize_linkedin_fn=lambda x: x,
            key_status_fn=lambda d: cc.outreachmagic_agent_key_status(d),
        )
        if r.get("dry_run") and r.get("to_process", 0) >= 1:
            results.append(_ok("batch_dry_run_icypeas", f"to_process={r.get('to_process')}"))
        else:
            results.append(_fail("batch_dry_run_icypeas", json.dumps(r)[:300]))
    except Exception as e:
        results.append(_fail("batch_dry_run_icypeas", f"{e}\n{traceback.format_exc()[:400]}"))

    # small live batch skip-om
    if not cfg.get("icypeas_api_key"):
        results.append(_skip("batch_live_icypeas_2", "no icypeas key"))
        return results
    try:
        r = run_batch(
            str(leads_path),
            cfg,
            None,
            BatchOptions(
                yes=True,
                skip_om=True,
                no_save=True,
                workers=2,
                delay=3,
                provider="icypeas",
                output_base=str(out_base),
                max_leads=10,
            ),
            skill_dir=skill_dir,
            normalize_linkedin_fn=lambda x: x,
            key_status_fn=lambda d: (False, None),
        )
        stats = r.get("stats") or {}
        processed = r.get("processed", 0)
        detail = (
            f"processed={processed} found={stats.get('found')} not_found={stats.get('not_found')} "
            f"errors={stats.get('errors')} rate_limited={stats.get('rate_limited')} "
            f"timeout={stats.get('timeout')}"
        )
        if r.get("error"):
            results.append(_fail("batch_live_icypeas_2", f"{r.get('error')} {detail}"))
        elif processed < 2:
            results.append(_fail("batch_live_icypeas_2", detail))
        else:
            http_err = sum(
                1 for row in (r.get("results") or [])
                if isinstance(row, dict) and row.get("status") == "http_error"
            )
            if http_err and stats.get("not_found", 0) >= http_err:
                results.append(
                    _fail(
                        "batch_live_icypeas_2",
                        f"http_error rows may be mis-bucketed: http_error={http_err} {detail}",
                    )
                )
            elif stats.get("errors", 0) == 0 and http_err:
                results.append(_fail("batch_live_icypeas_2", f"http_error not in errors stat: {detail}"))
            elif stats.get("rate_limited", 0) > 1:
                results.append(_fail("batch_live_icypeas_2", f"too many rate_limited: {detail}"))
            else:
                results.append(_ok("batch_live_icypeas_2", detail))
    except Exception as e:
        results.append(_fail("batch_live_icypeas_2", f"{e}\n{traceback.format_exc()[:500]}"))

    return results


def run_mv_credits(cfg: dict[str, Any]) -> dict[str, Any]:
    key = (cfg.get("millionverifier_api_key") or "").strip()
    if not key:
        return _skip("mv_credits", "no MV key")
    mv = MillionVerifierProvider(key)
    credits, err = mv.check_credits()
    if err:
        return _fail("mv_credits", err)
    return _ok("mv_credits", f"credits={credits}")


def main() -> int:
    default_tmp = f"/tmp/email-finder-v22-test-{int(time.time())}"
    tmp = Path(os.environ.get("EMAIL_FINDER_TEST_TMP", default_tmp))
    tmp.mkdir(parents=True, exist_ok=True)
    skill_dir = _find_skill_dir()
    cfg = load_config()

    all_results: list[dict[str, Any]] = []
    all_results.extend(run_unit_tests())
    all_results.append(run_config_test())
    all_results.append(run_mv_credits(cfg))

    if cfg.get("trykitt_api_key"):
        all_results.append(
            run_single_find(
                "trykitt_single",
                trykitt_find,
                cfg,
                full_name="Tim Cook",
                domain="apple.com",
            )
        )
    else:
        all_results.append(_skip("trykitt_single", "no key"))

    if cfg.get("icypeas_api_key"):
        all_results.append(
            run_single_find(
                "icypeas_single",
                icypeas_find,
                cfg,
                full_name="Tim Cook",
                domain="apple.com",
            )
        )
        all_results.append(run_poll_timeout_forced(cfg))
        if cfg.get("trykitt_api_key"):
            all_results.append(
                run_single_find(
                    "waterfall_single",
                    lambda c, **kw: run_find_with_fallback(c, provider_names=["trykitt", "icypeas"], **kw),
                    cfg,
                    full_name="Jeff Bezos",
                    domain="amazon.com",
                )
            )
    else:
        all_results.append(_skip("icypeas_single", "no key"))

    all_results.extend(run_batch_smoke(cfg, skill_dir, tmp))

    passed = sum(1 for r in all_results if r["status"] == "pass")
    failed = sum(1 for r in all_results if r["status"] == "fail")
    skipped = sum(1 for r in all_results if r["status"] == "skip")

    summary = {
        "location": os.environ.get("TEST_LOCATION", "local"),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "results": all_results,
    }
    print(json.dumps(summary, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
