"""run_find_with_domain_fallback() (Stage D5): waterfall across candidate
domains, stopping at the first domain+provider combination that returns an
email -- the same "stop at first success" shape run_find_with_fallback()
already uses across providers, one level up across domains."""

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import waterfall  # noqa: E402


def test_stops_at_first_domain_that_succeeds():
    calls = []

    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        calls.append(domain)
        if domain == "coe.acme.com":
            return {"email": None, "status": "not_found", "provider_attempts": [{"provider": "trykitt", "status": "not_found"}]}
        if domain == "mail.acme.com":
            return {"email": "jane@mail.acme.com", "validity": "valid", "provider": "trykitt",
                    "provider_attempts": [{"provider": "trykitt", "status": "found"}]}
        return {"email": None, "status": "not_found", "provider_attempts": []}

    with mock.patch.object(waterfall, "run_find_with_fallback", side_effect=fake_find):
        result = waterfall.run_find_with_domain_fallback(
            {}, full_name="Jane", domains=["coe.acme.com", "mail.acme.com", "acme.com"],
        )

    assert result["email"] == "jane@mail.acme.com"
    assert result["winning_domain"] == "mail.acme.com"
    # Must not have tried the third domain once the second succeeded.
    assert calls == ["coe.acme.com", "mail.acme.com"]


def test_provider_attempts_combined_and_tagged_with_their_own_domain():
    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        if domain == "a.com":
            return {"email": None, "status": "not_found",
                    "provider_attempts": [{"provider": "trykitt", "status": "not_found"}]}
        return {"email": "j@b.com", "provider": "trykitt",
                "provider_attempts": [{"provider": "trykitt", "status": "found"}]}

    with mock.patch.object(waterfall, "run_find_with_fallback", side_effect=fake_find):
        result = waterfall.run_find_with_domain_fallback(
            {}, full_name="Jane", domains=["a.com", "b.com"],
        )

    attempts = result["provider_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["domain"] == "a.com"
    assert attempts[0]["status"] == "not_found"
    assert attempts[1]["domain"] == "b.com"
    assert attempts[1]["status"] == "found"


def test_no_domains_returns_skipped():
    result = waterfall.run_find_with_domain_fallback({}, full_name="Jane", domains=[])
    assert result["status"] == "skipped"
    assert result["provider_attempts"] == []


def test_all_domains_fail_returns_not_found_with_full_attempt_history():
    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        return {"email": None, "status": "not_found",
                "provider_attempts": [{"provider": "trykitt", "status": "not_found"}]}

    with mock.patch.object(waterfall, "run_find_with_fallback", side_effect=fake_find):
        result = waterfall.run_find_with_domain_fallback(
            {}, full_name="Jane", domains=["a.com", "b.com", "c.com"],
        )

    assert result["email"] is None
    assert "winning_domain" not in result
    assert len(result["provider_attempts"]) == 3
    assert [a["domain"] for a in result["provider_attempts"]] == ["a.com", "b.com", "c.com"]


def test_credits_exhausted_stops_trying_further_domains():
    calls = []

    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        calls.append(domain)
        return {"email": None, "status": "credits_exhausted", "provider_attempts": []}

    with mock.patch.object(waterfall, "run_find_with_fallback", side_effect=fake_find):
        waterfall.run_find_with_domain_fallback(
            {}, full_name="Jane", domains=["a.com", "b.com", "c.com"],
        )

    assert calls == ["a.com"]


def test_single_domain_behaves_like_direct_call():
    def fake_find(cfg, *, full_name, domain, linkedin="", provider_names=None):
        return {"email": "j@only.com", "provider": "trykitt", "provider_attempts": []}

    with mock.patch.object(waterfall, "run_find_with_fallback", side_effect=fake_find):
        result = waterfall.run_find_with_domain_fallback(
            {}, full_name="Jane", domains=["only.com"],
        )
    assert result["email"] == "j@only.com"
    assert result["winning_domain"] == "only.com"
