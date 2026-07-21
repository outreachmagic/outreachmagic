"""find-domains observability: progress, interruption, and retry economics.

All four came out of one incident. The command printed four key-fallback
warnings and then nothing for minutes, so a working run and a hung one looked
identical; the operator killed and restarted it three times, re-spending
~90-120 Serper queries to discover it had been fine each time.
"""

import signal
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domain_discovery as dd  # noqa: E402
import enrich  # noqa: E402
import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _company(conn, ws_id, name, person):
    cid = om.ensure_company(conn, name=name)
    lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead["id"])
    conn.commit()
    return cid, lead["id"]


def _seed(n=3):
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    for i in range(n):
        _company(conn, ws["id"], f"Company {i} LLC", f"Person {i}")
    conn.close()


def _result(domain="found.com"):
    return {"organic": [{"link": f"https://www.{domain}/",
                         "title": "A Company", "snippet": "x"}],
            "knowledgeGraph": {}}


# ── Bug 1: silent execution ──────────────────────────────────────────────────

def test_each_company_reports_progress_to_stderr(capsys):
    _seed(3)
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _result()):
        om.find_domains_for_workspace("storefront")
    err = capsys.readouterr().err
    for i in range(3):
        assert f"Company {i} LLC" in err, err
    assert "[    1/3]" in err
    assert "credits" in err          # periodic roll-up on the final company


def test_progress_goes_to_stderr_so_stdout_stays_parseable(capsys):
    """Callers parse the JSON result off stdout; progress must not corrupt it."""
    _seed(2)
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _result()):
        om.find_domains_for_workspace("storefront")
    captured = capsys.readouterr()
    assert "Company 0 LLC" in captured.err
    assert "Company 0 LLC" not in captured.out


# ── Bug 3: SIGTERM lost the summary ──────────────────────────────────────────

def test_interrupt_returns_the_partial_summary_instead_of_dying(capsys):
    """Work was always committed per company; what was missing was any report
    of what had been done or what it cost."""
    _seed(6)
    calls = {"n": 0}

    def fake(query, config):
        calls["n"] += 1
        if calls["n"] == 2:
            signal.raise_signal(signal.SIGTERM)
        return _result()

    with mock.patch.object(enrich, "serper_search", side_effect=fake):
        out = om.find_domains_for_workspace("storefront")

    assert out["status"] == "ok"
    assert "interrupted" in out["stopped_reason"]
    assert out["companies_remaining"] > 0
    assert out["serper_queries_spent"] >= 1
    # ...and the companies it did finish are persisted, not rolled back.
    assert len(out["results"]) >= 1


def test_the_signal_handler_is_restored_afterwards():
    """A library function must not leave the process's handlers rewired."""
    _seed(1)
    before = signal.getsignal(signal.SIGTERM)
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _result()):
        om.find_domains_for_workspace("storefront")
    assert signal.getsignal(signal.SIGTERM) is before


# ── Bug 4: --retry-unresolved re-spent on a killed run's work ────────────────

def test_retry_unresolved_does_not_re_search_what_a_killed_run_just_did():
    """The scenario that motivated this: a run dies, the operator restarts it
    with --retry-unresolved, and every company already paid for is searched
    again."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _company(conn, ws["id"], "Unfindable Co", "P One")
    conn.close()

    empty = {"organic": [], "knowledgeGraph": {}}
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: empty):
        om.find_domains_for_workspace("storefront")     # searched, resolved nothing

    with mock.patch.object(enrich, "serper_search",
                           side_effect=AssertionError("must not re-spend")) as fake:
        out = om.find_domains_for_workspace("storefront", retry_unresolved=True)
        fake.assert_not_called()
    assert out["cached"] == 1


def test_retry_unresolved_still_bypasses_the_30_day_cache():
    """The flag's actual purpose -- re-evaluate under new scoring -- must
    survive the fix."""
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    cid, lead_id = _company(conn, ws["id"], "Unfindable Co", "P One")
    conn.close()

    empty = {"organic": [], "knowledgeGraph": {}}
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: empty):
        om.find_domains_for_workspace("storefront")

    # Age the observation past the retry window but well inside FRESHNESS_DAYS.
    conn = om.get_conn()
    conn.execute(
        """UPDATE lead_provider_observations
           SET observed_at = datetime('now', ?)
           WHERE kind = 'domain_lookup'""",
        (f"-{dd.RETRY_FRESHNESS_HOURS + 2} hours",))
    conn.commit()
    conn.close()

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: empty) as fake:
        om.find_domains_for_workspace("storefront", retry_unresolved=True)
        assert fake.called, "a genuine retry must still bypass the 30-day cache"


def test_a_plain_rerun_is_still_fully_cached():
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    _company(conn, ws["id"], "Unfindable Co", "P One")
    conn.close()

    empty = {"organic": [], "knowledgeGraph": {}}
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: empty):
        om.find_domains_for_workspace("storefront")
    with mock.patch.object(enrich, "serper_search",
                           side_effect=AssertionError("must not re-query")) as fake:
        out = om.find_domains_for_workspace("storefront")
        fake.assert_not_called()
    assert out["cached"] == 1


# ── Bug 2 + 5: a working fallback key looked identical to a broken run ───────

def test_a_working_backup_key_announces_itself(capsys):
    """Only failures printed, so surviving on a backup key produced pages of
    "slot 0 failed, trying next" and no indication anything worked."""
    import urllib.error

    import api_key_pool as pool

    pool._ANNOUNCED_FALLBACKS.clear()
    attempts = []

    def fn(key):
        attempts.append(key)
        if key == "bad-key":
            raise urllib.error.HTTPError("u", 429, "rate limited", {}, None)
        return "ok"

    with mock.patch.object(pool, "api_key_pool", return_value=["bad-key", "good-key"]), \
         mock.patch.object(pool, "record_key_usage"), \
         mock.patch.object(pool, "_slot_is_retired", return_value=False):
        assert pool.call_with_key_pool("SERPER_API_KEY", fn, provider="serper") == "ok"

    err = capsys.readouterr().err
    assert "backup key (slot 1) is working" in err, err
    assert "proceeding normally" in err


def test_the_backup_announcement_does_not_repeat_every_call(capsys):
    """One line per provider+slot per process -- the operator needs to know the
    fallback took over, not to be told on every single query."""
    import api_key_pool as pool

    pool._ANNOUNCED_FALLBACKS.clear()
    with mock.patch.object(pool, "api_key_pool", return_value=["k0", "k1"]), \
         mock.patch.object(pool, "record_key_usage"), \
         mock.patch.object(pool, "_slot_is_retired", side_effect=lambda p, s: s == 0):
        for _ in range(5):
            pool.call_with_key_pool("SERPER_API_KEY", lambda k: "ok", provider="serper")
    assert capsys.readouterr().err.count("backup key (slot 1) is working") == 1
