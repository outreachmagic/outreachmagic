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
    assert "(92% remaining)" in pull

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


def test_pull_snapshot_skips_include_pending(monkeypatch):
    """Snapshot bulk pages must not COUNT; one limit=1 probe per kind may include_pending."""
    snapshot_calls = []

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            snapshot_calls.append(kwargs)
            kind = kwargs.get("snapshot_kind")
            if kwargs.get("include_pending"):
                return {
                    "events": [],
                    "pending_snapshot_count": {"core": 100, "workspace": 50}.get(kind, 0),
                }
            if kind == "core":
                return {
                    "events": [{"relay_id": 1_000_000_001}],
                    "max_snapshot_id": 1,
                    "has_more_snapshots": False,
                }
            if kind == "workspace":
                return {
                    "events": [{"relay_id": 1_000_000_002}],
                    "max_snapshot_id": 2,
                    "has_more_snapshots": False,
                }
            return {"events": []}
        return {"events": [], "max_id": 0}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    _patch_pull_prefetch(monkeypatch)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=False)
    assert len(snapshot_calls) >= 3
    for call in snapshot_calls:
        if call.get("include_pending"):
            assert call.get("limit") == 1
        else:
            assert not call.get("include_pending")
            assert call.get("timeout") == om.RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT


def test_sync_progress_with_pending_counts(capsys, monkeypatch):
    event_calls = {"n": 0}
    snap_calls = {"n": 0}

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            snap_calls["n"] += 1
            if kwargs.get("include_pending"):
                return {
                    "events": [],
                    "pending_snapshot_count": 5336,
                }
            if snap_calls["n"] <= 2:
                return {
                    "events": [{"relay_id": 1_000_000_001}],
                    "max_snapshot_id": 1,
                    "has_more_snapshots": False,
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


def test_event_pull_continues_after_pending_count_on_page_one(monkeypatch):
    """Large pending backlog must not bump event pages above RELAY_PULL_EVENT_MAX."""
    limits = []
    pages = [
        {
            "events": [{"relay_id": i} for i in range(1, 1001)],
            "max_id": 1000,
            "pending_event_count": 3000,
            "has_more_events": True,
        },
        {
            "events": [{"relay_id": i} for i in range(1001, 2001)],
            "max_id": 2000,
            "has_more_events": True,
        },
        {
            "events": [{"relay_id": i} for i in range(2001, 3001)],
            "max_id": 3000,
            "has_more_events": False,
        },
    ]

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            return {"events": []}
        limits.append(kwargs.get("limit"))
        return pages.pop(0)

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    _patch_pull_prefetch(monkeypatch, lambda keys, conn=None: set())
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    stats = {}
    om.sync_from_relay_org("om_agent_test", after_id=0, full=False, quiet=True, stats=stats)

    assert limits == [om.RELAY_PULL_PAGE_SIZE] * 3
    assert stats["event_pages"] == 3
    assert stats["pull_after_id_end"] == 3000
    assert stats["relay_events_seen"] == 3000


def test_event_pull_continues_when_worker_caps_below_request(monkeypatch):
    """Pre-cap client requested 5k; worker returns 1k + pull_limit + has_more false → keep paging."""
    monkeypatch.setattr(om, "RELAY_PULL_EVENT_MAX", 5000)
    pages = [
        {
            "events": [{"relay_id": i} for i in range(1, 1001)],
            "max_id": 1000,
            "has_more_events": False,
            "pull_limit": 1000,
        },
        {"events": [{"relay_id": 1001}], "max_id": 1001, "has_more_events": False},
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
    assert stats["pull_after_id_end"] == 1001


def test_agent_event_log_reuses_pull_conn(monkeypatch):
    """Agent event_log during pull must not open a second SQLite connection (log_event)."""
    om.init_db()
    pull = om.get_conn()

    class _Routing:
        mode = om.WORKSPACE_ROUTING_SINGLE
        default_workspace_id = "ws_test"

    logged = []

    def spy_log_event(*_args, **kwargs):
        logged.append(kwargs)
        return 99

    monkeypatch.setattr(om, "log_event", spy_log_event)
    monkeypatch.setattr(om, "get_or_create_client_id", lambda: "local-client")
    monkeypatch.setattr(om, "find_lead_by_identifier", lambda *_a, **_k: 1)
    monkeypatch.setattr(om, "get_org_routing_config", lambda *_a, **_k: _Routing())

    event = {
        "platform": "agent",
        "client_id": "remote-client",
        "action": "event_log",
        "entity_key": "email:test@example.com",
        "timestamp": "2026-01-01T00:00:00Z",
        "workspace": "default",
        "payload": {"event_type": "email_sent", "direction": "outbound"},
    }
    om.ingest_agent_entry(
        event,
        pull_conn=pull,
        defer_mark=True,
        pending_marks=[],
        defer_activity_refresh=True,
    )
    pull.close()
    assert len(logged) == 1
    assert logged[0]["conn"] is pull
    assert logged[0]["commit"] is False
    assert logged[0]["refresh_activity"] is False


def test_snapshot_pull_passes_shared_pull_conn(monkeypatch):
    """Snapshot pages must reuse the bulk pull session (no per-page get_conn)."""
    captured = []

    def fake_ingest(events, **kwargs):
        captured.append(kwargs.get("pull_conn"))
        return {
            "imported": 0,
            "skipped": len(events),
            "skipped_duplicates": len(events),
            "skipped_filtered": 0,
            "skipped_errors": 0,
            "skipped_resolved": 0,
            "assigned_resolved": 0,
            "newest_relay_id_seen": 0,
        }

    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs["snapshot_kind"]
            if kind == "core":
                return {
                    "events": [{"relay_id": 2_000_000_001, "platform": "agent"}],
                    "max_snapshot_id": 1,
                    "has_more_snapshots": False,
                }
            return {"events": []}
        return {"events": []}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "_ingest_relay_page", fake_ingest)
    monkeypatch.setattr(om, "_snapshot_pending_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org(
        "om_agent_test",
        after_id=0,
        full=False,
        quiet=True,
        skip_routing_sync=True,
        pull_kinds=frozenset({"core"}),
    )
    assert len(captured) == 1
    assert captured[0] is not None


def test_parse_pull_kinds():
    assert om.parse_pull_kinds(None) is None
    assert om.parse_pull_kinds("events,company") == frozenset({"events", "company"})
    try:
        om.parse_pull_kinds("bogus")
        assert False
    except ValueError:
        pass


def test_sync_skips_event_pull_when_kind_company_only(monkeypatch):
    calls = []

    def fake_pull(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("snapshots_only"):
            return {"events": [], "pending_snapshot_count": 0}
        return {"events": [{"relay_id": 1}], "max_id": 1}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    _patch_pull_prefetch(monkeypatch)
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)

    om.sync_from_relay_org(
        "om_agent_test",
        after_id=0,
        full=False,
        quiet=True,
        skip_routing_sync=True,
        pull_kinds=frozenset({"company"}),
    )
    assert all(c.get("snapshots_only") for c in calls)
    assert not any(c.get("snapshots_only") is False for c in calls)


def test_probe_relay_backlog(monkeypatch):
    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs["snapshot_kind"]
            return {"pending_snapshot_count": 10 if kind == "core" else 0}
        return {"pending_event_count": 62000}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "get_last_max_id", lambda: 72263)
    monkeypatch.setattr(om, "get_snapshot_cursor", lambda _k: 100)

    report = om.probe_relay_backlog("om_agent_test")
    assert report["events"]["pending"] == 62000
    assert report["events"]["est_pages"] == 62
    assert report["snapshots"]["core"]["pending"] == 10


def test_snapshot_pull_limit_for_kind_caps_all_snapshots():
    assert om._snapshot_pull_limit_for_kind("company", 5000) == om.RELAY_PULL_SNAPSHOT_MAX
    assert om._snapshot_pull_limit_for_kind("core", 5000) == om.RELAY_PULL_SNAPSHOT_MAX


def test_pull_events_org_caps_company_snapshot_limit(monkeypatch):
    captured = []

    class FakeResp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(om.urllib.request, "urlopen", fake_urlopen)
    om.pull_events_org(
        "om_agent_test",
        limit=5000,
        snapshots_only=True,
        snapshot_kind="company",
    )
    assert "limit=1000" in captured[0]


def test_pull_events_org_caps_event_limit(monkeypatch):
    captured = []

    class FakeResp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(om.urllib.request, "urlopen", fake_urlopen)
    om.pull_events_org("om_agent_test", limit=5000)
    assert "limit=1000" in captured[0]
    om.pull_events_org("om_agent_test", limit=5000, snapshots_only=True, snapshot_kind="core")
    assert "limit=1000" in captured[1]


def test_pull_diagnostics_verdict_priority():
    assert om._pull_diagnostics_verdict({"cursor_stalled": True}) == "cursor stalled"
    assert om._pull_diagnostics_verdict({"relay_events_seen": 0}) == "relay empty"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 0, "skipped_duplicates": 5}
    ) == "relay has events but deduped"
    assert om._pull_diagnostics_verdict(
        {"relay_events_seen": 5, "imported": 1, "cursor_advanced": True}
    ) == "cursor advanced"
