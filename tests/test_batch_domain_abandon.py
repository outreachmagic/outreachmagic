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


# ── Readout: a per-lead log, plus a rule showing the shape of the run ────────

def test_each_lead_prints_its_own_line_with_the_verdict(capsys):
    """A dashboard answers "how far along am I". It cannot answer "what did we
    get for this person", which is the question being asked while it runs."""
    for n, (name, dom, email, validity, status) in enumerate([
        ("Ada Lovelace", "acme.com", "ada@acme.com", "valid", "found"),
        ("Bob Stone", "beta.com", "bob@beta.com", "valid-risky", "found"),
        ("Cy Reed", "gam.com", "cy@gam.com", "invalid", "found"),
        ("Dee Turner", "dead.com", None, None, "not_found"),
    ], start=1):
        progress.print_result_line(
            n, 4, name=name, domain=dom, email=email, validity=validity,
            status=status, file=sys.stdout)
    out = capsys.readouterr().out
    assert "ada@acme.com" in out and "valid" in out
    assert "bob@beta.com" in out and "risky" in out
    assert "cy@gam.com" in out and "invalid" in out
    assert "Dee Turner" in out and "not found" in out
    assert len([l for l in out.splitlines() if l.strip()]) == 4


def test_a_skipped_lead_says_why(capsys):
    progress.print_result_line(
        7, 20, name="Eve Ash", domain="deadco.com", email=None,
        status="skipped", skip_reason="no_provider_coverage", file=sys.stdout)
    out = capsys.readouterr().out
    assert "no provider coverage" in out and "Eve Ash" in out and "skipped" in out


@pytest.mark.parametrize("validity, email, status, expected", [
    ("valid", "a@b.com", "found", "valid"),
    ("valid-risky", "a@b.com", "found", "risky"),
    ("catch_all", "a@b.com", "found", "catch-all"),
    ("invalid", "a@b.com", "found", "invalid"),
    (None, None, "not_found", "not_found"),
    (None, None, "skipped", "skipped"),
])
def test_verdict_bucketing(validity, email, status, expected):
    assert progress.verdict_bucket(validity, email, status) == expected


def test_the_rule_leads_with_the_verdict_split(capsys):
    progress.print_progress(
        30, 100, {"found": 24, "not_found": 6, "errors": 0,
                  "verdicts": {"valid": 18, "risky": 5, "invalid": 1}},
        __import__("time").time() - 120, file=sys.stdout)
    out = capsys.readouterr().out
    assert "valid 18" in out and "risky 5" in out and "invalid 1" in out
    assert "30/100" in out


def test_domain_block_shows_the_verdict_split_not_just_a_count(capsys):
    """A bare "cousins.com 24/24" says nothing about whether those 24 are
    usable: 24 valid addresses and 24 catch-alls are the same number and a
    completely different outcome."""
    progress.print_progress(
        40, 100, {"found": 28, "not_found": 12, "errors": 0,
                  "verdicts": {"valid": 22, "risky": 6}},
        __import__("time").time() - 60, file=sys.stdout,
        domain_stats={
            "cousins.com": {"tried": 24, "found": 24, "valid": 22, "risky": 2, "invalid": 0},
            "catchall.com": {"tried": 4, "found": 4, "valid": 0, "risky": 4, "invalid": 0},
            "dead.com": {"tried": 5, "found": 0, "valid": 0, "risky": 0, "invalid": 0},
        },
    )
    out = capsys.readouterr().out
    assert "cousins.com" in out and "valid 22" in out
    assert "none confirmed" in out          # all-catch-all domain is called out
    assert "no coverage" in out
