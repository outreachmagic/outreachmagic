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
            kind = kwargs.get("snapshot_kind", "workspace")
            calls["n"] += 1
            if kind == "core" and calls["n"] == 1:
                return {"events": [{"relay_id": 1_000_000_001}], "max_snapshot_id": 1}
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
    assert "Pulling from relay" in out
    assert "Relay: p1" in out
    assert "Pulling core" in out
    assert "Snapshot (core)" in out


def test_pull_uses_id_cursors_only(monkeypatch):
    calls = []

    def fake_pull(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("snapshots_only"):
            return {"events": []}
        return {"events": [], "max_id": 100}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)

    om.sync_from_relay_org("om_agent_test", after_id=99, full=False, quiet=True)

    event_pulls = [c for c in calls if not c.get("snapshots_only")]
    assert "since" not in event_pulls[0]
    assert event_pulls[0]["after_id"] == 99


def test_format_pull_summary_includes_lead_count():
    om.init_db()
    summary = om.format_pull_summary(2, 5, {"skipped_duplicates": 3, "skipped_filtered": 2})
    assert "Imported: 2 events" in summary
    assert "3 dupes" in summary


def test_pull_failure_message_routing_hint():
    msg = om._pull_failure_message(RuntimeError("Routing API 500: Internal Server Error"))
    assert "--skip-routing-sync" in msg


def test_estimate_relay_pages():
    assert om._estimate_relay_pages(1400) == 2
    assert om._estimate_relay_pages(1000) == 1
    assert om._estimate_relay_pages(None) is None


def test_sync_progress_with_pending_counts(capsys, monkeypatch):
    event_calls = {"n": 0}
    snap_calls = {"n": 0}

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            snap_calls["n"] += 1
            if snap_calls["n"] == 1:
                return {
                    "events": [{"relay_id": 1_000_000_001}],
                    "max_snapshot_id": 1,
                    "has_more_snapshots": False,
                    "pending_snapshot_count": 5336,
                }
            return {"events": []}
        event_calls["n"] += 1
        if event_calls["n"] == 1:
            return {
                "events": [{"relay_id": 2}],
                "max_id": 2,
                "pending_event_count": 1400,
            }
        return {"events": []}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    monkeypatch.setattr(om, "relay_already_ingested", lambda *_a, **_k: True)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)
    out = capsys.readouterr().out
    assert "~1400 pending" in out
    assert "p1/~2" in out
    assert "~5336 pending" in out


def test_pull_diagnostics_verdict_priority():
    assert om._pull_diagnostics_verdict({"cursor_stalled": True}) == "cursor stalled"
    assert om._pull_diagnostics_verdict({"relay_events_seen": 0}) == "relay empty"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 0, "skipped_duplicates": 5}
    ) == "relay has events but deduped"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 1, "cursor_advanced": True}
    ) == "cursor advanced"
