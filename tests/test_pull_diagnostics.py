#!/usr/bin/env python3
"""Regression tests for pull diagnostics and cursor behavior."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402


def test_sync_stats_incremental_duplicate_only_advances_cursor(monkeypatch):
    pages = [
        {"events": [{"relay_id": 10}, {"relay_id": 11}], "max_id": 11},
        {"events": [], "max_id": 11},
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": []}
        return pages.pop(0)

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(om, "relay_already_ingested", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_kwargs: None)

    stats = {}
    imported, skipped = om.sync_from_relay_org(
        "om_agent_test",
        after_id=5,
        full=False,
        quiet=True,
        stats=stats,
    )

    assert imported == 0
    assert skipped == 2
    assert stats["skipped_duplicates"] == 2
    assert stats["cursor_advanced"] is True
    assert stats["pull_after_id_end"] == 11
    assert stats["verdict"] == "relay has events but deduped"


def test_sync_stats_cursor_stall_guard(monkeypatch):
    first_page = {"events": [{"relay_id": i} for i in range(1, 1001)], "max_id": 5}

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": []}
        return first_page

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(om, "relay_already_ingested", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_kwargs: None)

    stats = {}
    imported, skipped = om.sync_from_relay_org(
        "om_agent_test",
        after_id=5,
        full=False,
        quiet=True,
        stats=stats,
    )

    assert imported == 0
    assert skipped == 1000
    assert stats["cursor_stalled"] is True
    assert stats["skipped_filtered"] == 1000
    assert stats["verdict"] == "cursor stalled"


def test_sync_pull_progress_when_not_quiet(capsys, monkeypatch):
    calls = {"n": 0}

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"events": [{"relay_id": 1}], "max_snapshot_id": 1}
            return {"events": []}
        return {"events": [{"relay_id": 2}], "max_id": 2}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(om, "relay_already_ingested", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_kwargs: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)

    out = capsys.readouterr().out
    assert "Contacting relay to pull new events..." in out
    assert "Pulled 1 relay events (page 1, 1 total seen)..." in out
    assert "Pulling snapshot profiles... page 1 (1 profiles, 1 total)..." in out


def test_sync_advances_cursor_past_empty_relay_page(monkeypatch):
    pages = [
        {"events": [], "max_id": 100},
        {"events": [{"relay_id": 101}], "max_id": 101},
        {"events": [], "max_id": 101},
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": []}
        return pages.pop(0)

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: 1)
    monkeypatch.setattr(om, "relay_already_ingested", lambda *_a, **_k: False)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "get_last_snapshot_after_id", lambda: 0)
    monkeypatch.setattr(om, "set_last_snapshot_after_id", lambda *_a, **_k: None)

    stats = {}
    imported, _skipped = om.sync_from_relay_org(
        "om_agent_test",
        after_id=50,
        full=False,
        quiet=True,
        stats=stats,
    )

    assert imported == 1
    assert stats["pull_after_id_end"] == 101
    assert stats["cursor_advanced"] is True


def test_pull_diagnostics_verdict_priority():
    assert om._pull_diagnostics_verdict({"cursor_stalled": True}) == "cursor stalled"
    assert om._pull_diagnostics_verdict({"relay_events_seen": 0}) == "relay empty"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 0, "skipped_duplicates": 5}
    ) == "relay has events but deduped"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 1, "cursor_advanced": True}
    ) == "cursor advanced"
