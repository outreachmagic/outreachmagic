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


def _prefetch_all(keys, conn=None):
    return set(keys)


def _prefetch_ws_empty(conn, org_id, keys):
    return set()


def _patch_pull_prefetch(monkeypatch, relay_keys=_prefetch_all):
    monkeypatch.setattr(om, "prefetch_relay_ingested", relay_keys)
    monkeypatch.setattr(om, "prefetch_ws_idempotency_keys", _prefetch_ws_empty)


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
    _patch_pull_prefetch(monkeypatch)
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
    _patch_pull_prefetch(monkeypatch, lambda keys, conn=None: set())
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
    assert skipped >= 999
    assert stats["cursor_stalled"] is True
    assert stats["skipped_filtered"] >= 999
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
    _patch_pull_prefetch(monkeypatch)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_kwargs: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)

    out = capsys.readouterr().out
    assert "Contacting relay to pull new events..." in out
    assert "Pulling from relay" in out
    assert "↓ Event" in out
    assert "↓ Lead" in out
    assert "p1 —" in out or "p1/" in out
    assert "[" in out


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


def test_relay_progress_format_helpers():
    pull = om._format_pull_progress(
        "Lead",
        page_n=2,
        total_pages=24,
        page_len=5000,
        seen=10000,
        total=117431,
    )
    assert "↓ Lead" in pull
    assert "p2/24" in pull
    assert "10,000/117,431" in pull
    assert "(8%)" in pull

    push = om._format_push_progress(
        "Event",
        page_n=2,
        total_pages=13,
        page_len=5000,
        seen=10000,
        total=62093,
        elapsed=7.9,
    )
    assert "↑ Event" in push
    assert "p2/13" in push
    assert "ok 7.9s" in push
    assert "(16%)" in push


def test_pull_snapshot_pending_requested_per_kind(monkeypatch):
    """Each snapshot kind should request include_pending on its first page."""
    pending_calls = []

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs.get("snapshot_kind")
            if kwargs.get("include_pending"):
                pending_calls.append(kind)
            if kind == "core":
                return {
                    "events": [{"relay_id": 1_000_000_001}],
                    "max_snapshot_id": 1,
                    "has_more_snapshots": False,
                    "pending_snapshot_count": 100,
                }
            if kind == "workspace":
                return {
                    "events": [{"relay_id": 1_000_000_002}],
                    "max_snapshot_id": 2,
                    "has_more_snapshots": False,
                    "pending_snapshot_count": 50,
                }
            return {"events": []}
        return {"events": [], "max_id": 0}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    _patch_pull_prefetch(monkeypatch)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)
    assert pending_calls == ["core", "workspace", "company"]


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
    _patch_pull_prefetch(monkeypatch)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)
    out = capsys.readouterr().out
    assert "~1,400 pending" in out or "~1400 pending" in out
    assert "p1/2" in out
    assert "~5,336 pending" in out or "~5336 pending" in out
    assert "%" in out


def test_event_pull_continues_after_bulk_limit_upgrade(monkeypatch):
    """Page 1 must not exit early when pull_limit bumps 1000→5000 after first response."""
    pages = [
        {
            "events": [{"relay_id": i} for i in range(1, 1001)],
            "max_id": 1000,
            "pending_event_count": 3000,
            "has_more_events": True,
        },
        {
            "events": [{"relay_id": i} for i in range(1001, 6001)],
            "max_id": 6000,
            "has_more_events": False,
        },
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": []}
        return pages.pop(0)

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    _patch_pull_prefetch(monkeypatch, lambda keys, conn=None: set())
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    stats = {}
    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=True, stats=stats)

    assert stats["event_pages"] == 2
    assert stats["pull_after_id_end"] == 6000
    assert stats["relay_events_seen"] == 6000


def test_pull_diagnostics_verdict_priority():
    assert om._pull_diagnostics_verdict({"cursor_stalled": True}) == "cursor stalled"
    assert om._pull_diagnostics_verdict({"relay_events_seen": 0}) == "relay empty"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 0, "skipped_duplicates": 5}
    ) == "relay has events but deduped"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 1, "cursor_advanced": True}
    ) == "cursor advanced"
