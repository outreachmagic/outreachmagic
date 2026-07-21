"""Early-abandon: stop paying a provider for a domain it clearly cannot resolve.

Provider coverage is bimodal per domain, not uniform. Measured over 342 real
leads: voya.com 14/14, cortland.com 13/13, transwestern.com 17/18 -- versus
lincolnapts.com 0/29 and ventronmanagement.com 0/15. The 0/29 spent 29 calls
to learn the same fact 29 times; 58 of 342 calls in that run went to domains
that never returned anything.
"""

import itertools
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import batch_runner  # noqa: E402
import progress  # noqa: E402


def _people(domain, n, start=0):
    return [{"name": f"Person {i}", "domain": domain, "company": "Co"}
            for i in range(start, start + n)]


_RUN_SEQ = itertools.count()


def _run(people, found_domains=(), abandon_after=3, workers=3):
    """Drive run_batch with a stubbed provider; return the calls it made."""
    calls = []

    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        calls.append(domain)
        if domain in found_domains:
            return {"status": "found", "email": f"x@{domain}", "validity": "valid",
                    "provider": "trykitt", "provider_attempts": []}
        return {"status": "not_found", "email": None, "provider": "trykitt",
                "provider_attempts": []}

    base = str(Path(tempfile.mkdtemp()) / f"run{next(_RUN_SEQ)}")
    opts = batch_runner.BatchOptions(
        workspace="w", workers=workers, no_save=True, skip_om=True, yes=True,
        max_leads=500, delay=0, progress_every=10_000, abandon_after=abandon_after,
        output_base=base,
    )
    with mock.patch.object(batch_runner, "run_find_with_fallback", side_effect=fake_find), \
         mock.patch.object(batch_runner, "load_people_json", return_value=people), \
         mock.patch.object(batch_runner, "prompt_batch_provider_plan", return_value=["trykitt"]), \
         mock.patch.object(batch_runner, "bulk_dedup_map", return_value=({}, False)), \
         mock.patch.object(batch_runner, "_mid_batch_credit_stop", return_value=False):
        batch_runner.run_batch(
            "in.json", {}, None, opts, skill_dir=SCRIPTS,
            normalize_linkedin_fn=lambda s: s, key_status_fn=lambda *a, **k: {},
        )
    return calls


def test_a_dead_domain_stops_costing_credits():
    """29 leads on a domain the provider cannot resolve must not cost 29 calls."""
    calls = _run(_people("lincolnapts.com", 29), abandon_after=3, workers=1)
    assert len(calls) < 29, f"spent {len(calls)} calls on a dead domain"
    assert len(calls) <= 6


def test_a_productive_domain_is_never_abandoned():
    """A miss on a domain that resolves others is about the person, not the
    domain -- one hit resets the counter."""
    people = _people("voya.com", 20)
    calls = _run(people, found_domains={"voya.com"}, abandon_after=3, workers=1)
    assert len(calls) == 20


def test_intermittent_misses_do_not_trigger_abandonment():
    """Alternating hit/miss must never reach N consecutive misses."""
    people = _people("mixed.com", 12)
    seen = {"n": 0}

    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        seen["n"] += 1
        hit = seen["n"] % 2 == 0
        return {"status": "found" if hit else "not_found",
                "email": f"x@{domain}" if hit else None,
                "provider": "trykitt", "provider_attempts": []}

    base = str(Path(tempfile.mkdtemp()) / f"run{next(_RUN_SEQ)}")
    opts = batch_runner.BatchOptions(
        workspace="w", workers=1, no_save=True, skip_om=True, yes=True,
        max_leads=500, delay=0, progress_every=10_000, abandon_after=3,
        output_base=base,
    )
    with mock.patch.object(batch_runner, "run_find_with_fallback", side_effect=fake_find), \
         mock.patch.object(batch_runner, "load_people_json", return_value=people), \
         mock.patch.object(batch_runner, "prompt_batch_provider_plan", return_value=["trykitt"]), \
         mock.patch.object(batch_runner, "bulk_dedup_map", return_value=({}, False)), \
         mock.patch.object(batch_runner, "_mid_batch_credit_stop", return_value=False):
        batch_runner.run_batch("in.json", {}, None, opts, skill_dir=SCRIPTS,
                               normalize_linkedin_fn=lambda s: s,
                               key_status_fn=lambda *a, **k: {})
    assert seen["n"] == 12


def test_one_dead_domain_does_not_stop_the_others():
    people = _people("dead.com", 10) + _people("good.com", 10, start=100)
    calls = _run(people, found_domains={"good.com"}, abandon_after=3, workers=1)
    assert calls.count("good.com") == 10
    assert calls.count("dead.com") <= 6


def test_abandon_can_be_switched_off():
    calls = _run(_people("dead.com", 12), abandon_after=0, workers=1)
    assert len(calls) == 12


# ── Readout ──────────────────────────────────────────────────────────────────

def test_progress_readout_shows_recent_outcomes_and_domain_coverage(capsys):
    progress.print_progress(
        10, 100, {"found": 4, "not_found": 6, "errors": 0}, __import__("time").time() - 60,
        provider="trykitt", file=sys.stdout,
        recent=[{"name": "Ada Lovelace", "domain": "acme.com",
                 "email": "ada@acme.com", "status": "found"},
                {"name": "Bob Stone", "domain": "dead.com",
                 "email": None, "status": "not_found"}],
        domain_stats={"acme.com": {"tried": 4, "found": 4},
                      "dead.com": {"tried": 5, "found": 0}},
        skipped_no_coverage=7,
    )
    out = capsys.readouterr().out
    assert "ada@acme.com" in out          # what actually came back
    assert "Bob Stone" in out
    assert "4/4" in out and "0/5" in out  # coverage is visible per domain
    assert "no coverage" in out
    assert "skipped (no coverage): 7" in out


def test_progress_readout_unchanged_when_no_extra_data(capsys):
    """The new blocks are additive -- callers that pass nothing see the old
    output."""
    progress.print_progress(
        5, 50, {"found": 2, "not_found": 3, "errors": 0}, __import__("time").time() - 10,
        provider="trykitt", file=sys.stdout,
    )
    out = capsys.readouterr().out
    assert "PROGRESS: 5/50" in out
    assert "no coverage" not in out


# ── Spacing leads that share a domain ────────────────────────────────────────

def _domains(queue):
    return [batch_runner.row_fields(r)[1] for _i, r in queue]


def test_same_domain_leads_are_spread_apart():
    """Candidate order groups by company, so a domain with 6 leads arrives as
    6 consecutive rows -- at 3 workers that is 3 simultaneous probes against
    one mail server, which reads as a dead domain when it is really just
    rate limiting."""
    queue = [(i, {"name": f"P{i}", "domain": "ventronmanagement.com"}) for i in range(6)]
    queue += [(i + 100, {"name": f"Q{i}", "domain": "voya.com"}) for i in range(6)]
    queue += [(i + 200, {"name": f"R{i}", "domain": "cortland.com"}) for i in range(6)]

    doms = _domains(batch_runner.spread_by_domain(queue))
    # No two neighbours share a domain.
    assert all(a != b for a, b in zip(doms, doms[1:])), doms


def test_spreading_does_not_cluster_at_the_tail():
    """A plain round-robin spaces the head well and clusters the tail: once the
    small domains run out, the big one runs consecutively -- exactly the burst
    this exists to prevent.

    With one domain far larger than the rest, perfect alternation is
    arithmetically impossible (12 leads against 4 others must repeat at least
    7 times). The guarantee is that it hits that floor rather than dumping the
    remainder in one block.
    """
    queue = [(i, {"name": f"P{i}", "domain": "big.com"}) for i in range(12)]
    queue += [(i + 100, {"name": f"Q{i}", "domain": "small1.com"}) for i in range(2)]
    queue += [(i + 200, {"name": f"R{i}", "domain": "small2.com"}) for i in range(2)]

    doms = _domains(batch_runner.spread_by_domain(queue))
    adjacent = sum(1 for a, b in zip(doms, doms[1:]) if a == b)
    floor = max(0, 12 - 1 - 4)          # biggest bucket, minus the separators available
    assert adjacent == floor, f"{adjacent} adjacent pairs, optimum is {floor}: {doms}"
    # ...and the crowding is at the end, after every other domain is spent.
    assert doms[:8] == ["big.com", "small1.com", "big.com", "small2.com",
                        "big.com", "small1.com", "big.com", "small2.com"], doms[:8]


def test_spreading_loses_nothing_and_duplicates_nothing():
    queue = [(i, {"name": f"P{i}", "domain": f"d{i % 7}.com"}) for i in range(40)]
    out = batch_runner.spread_by_domain(queue)
    assert sorted(i for i, _ in out) == sorted(i for i, _ in queue)
    assert len(out) == len(queue)


def test_order_within_one_domain_is_preserved():
    queue = [(i, {"name": f"P{i}", "domain": "acme.com"}) for i in range(4)]
    queue += [(i + 50, {"name": f"Q{i}", "domain": "beta.com"}) for i in range(4)]
    out = batch_runner.spread_by_domain(queue)
    acme = [i for i, r in out if batch_runner.row_fields(r)[1] == "acme.com"]
    assert acme == sorted(acme)


def test_a_single_domain_batch_is_unchanged():
    """Nothing to interleave with -- must not reorder or drop anything."""
    queue = [(i, {"name": f"P{i}", "domain": "solo.com"}) for i in range(5)]
    assert batch_runner.spread_by_domain(queue) == queue


def test_readout_shows_the_validity_verdict(capsys):
    """A catch_all is a guess the server declined to confirm; showing it as a
    plain OK is how bounce rates climb."""
    progress.print_progress(
        4, 40, {"found": 3, "not_found": 1, "errors": 0}, __import__("time").time() - 30,
        provider="trykitt", file=sys.stdout,
        recent=[
            {"name": "Ada L", "domain": "acme.com", "email": "a@acme.com", "validity": "valid"},
            {"name": "Bob S", "domain": "beta.com", "email": "b@beta.com", "validity": "catch_all"},
            {"name": "Cy R", "domain": "gam.com", "email": "c@gam.com", "validity": "valid-risky"},
            {"name": "Dee T", "domain": "dead.com", "email": None, "status": "not_found"},
        ],
    )
    out = capsys.readouterr().out
    assert "[valid]" in out
    assert "[catch_all]" in out and "CTCH" in out
    assert "[valid-risky]" in out and "RISK" in out
    assert "not_found" in out
