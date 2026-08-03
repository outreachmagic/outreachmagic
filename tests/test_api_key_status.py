"""Runtime API key status: what the dashboard's API-keys panel renders.

The panel showed Serper as "not used yet" after 1,023 calls in one day. It was
rendering the local status file faithfully; the file was the thing that was
wrong. Two write-path defects put it there, and both are covered here.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_key_pool as akp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_session():
    akp.clear_session_state()
    yield
    akp.clear_session_state()


def _raises(exc):
    def fn(_key):
        raise exc
    return fn


def test_an_unexpected_exception_still_records_the_slot(monkeypatch):
    """The bug that produced "never used" for a key that had just crashed.

    call_with_key_pool only caught HTTPError and ValueError. Anything else --
    a KeyError from a missing config field, a socket timeout, a bug in the
    adapter -- propagated out of the loop without recording, so the panel could
    not distinguish "idle" from "called and blew up".
    """
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "abcdef123456")

    with pytest.raises(KeyError):
        akp.call_with_key_pool(
            "FAKE_PROVIDER_KEY", _raises(KeyError("serper_endpoint")),
            provider="fakeprov")

    slot = akp.load_key_status()["fakeprov"]["0"]
    assert slot["status"] == "failed"
    assert "KeyError" in slot["last_error"]
    # And the original exception is unchanged -- this records, it does not
    # swallow or convert into a failover.
    assert slot["last_used"]


def test_the_report_no_longer_calls_a_crashed_key_never_used(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "abcdef123456")
    with pytest.raises(RuntimeError):
        akp.call_with_key_pool(
            "SERPER_API_KEY", _raises(RuntimeError("boom")), provider="serper")

    report = akp.build_api_keys_report()
    serper = next(p for p in report["providers"] if p["provider"] == "serper")
    assert serper["keys"][0]["status"] == "failed"
    assert "never_used" not in json.dumps(serper)


def test_last_ok_survives_a_later_failure():
    """"Failing now, last worked Tuesday" and "failing now, never worked" are
    different problems; the panel cannot tell them apart from status alone."""
    akp.record_key_usage(provider="fakeprov", slot=0, success=True)
    first_ok = akp.load_key_status()["fakeprov"]["0"]["last_ok"]
    assert first_ok

    akp.record_key_usage(provider="fakeprov", slot=0, success=False, error="HTTP 402")
    slot = akp.load_key_status()["fakeprov"]["0"]
    assert slot["status"] == "failed"
    assert slot["last_ok"] == first_ok, "last_ok must survive a failure"
    assert slot["last_error"] == "HTTP 402"


def test_a_never_successful_key_reports_last_ok_none():
    akp.record_key_usage(provider="fakeprov", slot=0, success=False, error="HTTP 401")
    assert akp.load_key_status()["fakeprov"]["0"]["last_ok"] is None


def test_recording_one_provider_does_not_erase_another():
    """The file holds every provider, and each write is a read-modify-write of
    the whole thing. A torn write used to leave invalid JSON, which
    load_key_status() swallows into `{}` -- and the next write then persisted
    that empty dict as the new truth, erasing every provider at once."""
    akp.record_key_usage(provider="trykitt", slot=0, success=True)
    akp.record_key_usage(provider="serper", slot=0, success=False, error="HTTP 400")
    akp.record_key_usage(provider="serper", slot=1, success=False, error="HTTP 400")

    data = akp.load_key_status()
    assert data["trykitt"]["0"]["status"] == "ok"
    assert set(data["serper"]) == {"0", "1"}


def test_status_file_is_written_atomically():
    """os.replace, so a crash mid-write leaves the previous file intact rather
    than a truncated one that reads back as {}."""
    akp.record_key_usage(provider="trykitt", slot=0, success=True)
    path = akp.status_file_path()
    # No temp files left behind, and what's on disk is valid JSON.
    assert json.loads(path.read_text())["trykitt"]["0"]["status"] == "ok"
    assert not list(path.parent.glob(f"{path.name}.*.tmp"))


def test_record_key_usage_never_raises(monkeypatch):
    """Bookkeeping must not be able to break a real provider call."""
    monkeypatch.setattr(akp, "status_file_path",
                        lambda: Path("/nonexistent-root-xyz/status.json"))
    akp.record_key_usage(provider="fakeprov", slot=0, success=True)  # must not raise
