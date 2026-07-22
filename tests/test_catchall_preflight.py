"""Pre-flight seeding of known catch-all domains from DB history.

_load_catchall_exhausted_domains() queries lead_provider_observations for
domains where all found results are risky/catch-all and found >= threshold.
Those domains are seeded into domain_stats before any worker fires so the very
first lead from a previously-exhausted domain is skipped without making an API
call.
"""

import itertools
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import batch_runner  # noqa: E402


_RUN_SEQ = itertools.count()


def _people(domain, n):
    return [{"name": f"Person {i}", "domain": domain, "company": "Co"} for i in range(n)]


def _run(people, preflight_map=None, skip_catchall_after=3, workers=2):
    """Drive run_batch with a stubbed provider and stubbed DB pre-flight."""
    calls = []

    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        calls.append(domain)
        return {"status": "not_found", "email": None, "provider": "trykitt",
                "provider_attempts": []}

    base = str(Path(tempfile.mkdtemp()) / f"run{next(_RUN_SEQ)}")
    opts = batch_runner.BatchOptions(
        workspace="w", workers=workers, no_save=True, skip_om=True, yes=True,
        max_leads=500, delay=0, progress_every=10_000, abandon_after=0,
        skip_catchall_after=skip_catchall_after, output_base=base,
    )
    preflight = preflight_map or {}
    with mock.patch.object(batch_runner, "run_find_with_fallback", side_effect=fake_find), \
         mock.patch.object(batch_runner, "load_people_json", return_value=people), \
         mock.patch.object(batch_runner, "prompt_batch_provider_plan", return_value=["trykitt"]), \
         mock.patch.object(batch_runner, "bulk_dedup_map", return_value=({}, False)), \
         mock.patch.object(batch_runner, "_mid_batch_credit_stop", return_value=False), \
         mock.patch.object(batch_runner, "_load_catchall_exhausted_domains", return_value=preflight), \
         mock.patch.object(batch_runner, "_acquire_batch_lock", return_value=object()):
        batch_runner.run_batch(
            "in.json", {}, None, opts, skill_dir=SCRIPTS,
            normalize_linkedin_fn=lambda s: s, key_status_fn=lambda *a, **k: {},
        )
    return calls


def test_no_preflight_without_flag():
    """With skip_catchall_after=0, _load_catchall_exhausted_domains is never called."""
    people = _people("bozzuto.com", 5)
    with mock.patch.object(batch_runner, "_load_catchall_exhausted_domains") as m:
        m.return_value = {}
        _run(people, skip_catchall_after=0)
        m.assert_not_called()


def test_preflight_domains_skipped_immediately():
    """Leads from a domain seeded as catch-all from DB are never sent to the provider."""
    people = _people("bozzuto.com", 6)
    calls = _run(people, preflight_map={"bozzuto.com": 3}, skip_catchall_after=3)
    assert calls == [], f"expected 0 API calls, got {len(calls)}"


def test_other_domains_unaffected():
    """Only the seeded domain is skipped; other domains still get API calls."""
    people = _people("bozzuto.com", 3) + _people("acme.com", 3)
    calls = _run(people, preflight_map={"bozzuto.com": 3}, skip_catchall_after=3)
    assert all(d == "acme.com" for d in calls), f"unexpected calls: {calls}"
    assert len(calls) == 3


def test_preflight_with_higher_risky_count():
    """Seeding with risky_ct > threshold still triggers the skip."""
    people = _people("bigcatchall.com", 5)
    calls = _run(people, preflight_map={"bigcatchall.com": 10}, skip_catchall_after=3)
    assert calls == []


def test_load_catchall_exhausted_domains_empty_when_db_unavailable():
    """Returns empty dict (never raises) when DB is not connected."""
    with mock.patch.object(batch_runner, "get_conn", side_effect=Exception("no db")):
        result = batch_runner._load_catchall_exhausted_domains(3)
    assert result == {}
