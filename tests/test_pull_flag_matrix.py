#!/usr/bin/env python3
"""Pull flag matrix — every optional phase/path should complete with stats populated."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402


def _prefetch_all(keys, conn=None):
    return set(keys)


def _patch_pull(monkeypatch):
    def fake_pull(*_args, **kwargs):
        if kwargs.get("snapshots_only"):
            kind = kwargs.get("snapshot_kind", "core")
            return {
                "events": [{"relay_id": 2_000_000_001}],
                "max_snapshot_id": 1,
                "has_more_snapshots": False,
            }
        return {"events": [], "max_id": 99}

    monkeypatch.setattr(om, "pull_events_org", fake_pull)
    monkeypatch.setattr(om, "ingest_relay_event", lambda *_a, **_k: None)
    monkeypatch.setattr(om, "prefetch_relay_ingested", _prefetch_all)
    monkeypatch.setattr(om, "prefetch_ws_idempotency_keys", lambda *_a, **_k: set())
    monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "maybe_sync_agent_secrets_from_cloud", lambda **_k: None)
    monkeypatch.setattr(om, "_snapshot_pending_count", lambda *_a, **_k: None)
    monkeypatch.setattr(om, "print_quarantine_guidance", lambda: None)


@pytest.mark.parametrize(
    "kwargs,expect_phases",
    [
        ({"skip_snapshots": True, "skip_routing_sync": True}, ["events"]),
        ({"skip_snapshots": True, "full": True, "skip_routing_sync": True}, ["events"]),
        ({"pull_kinds": frozenset({"events"}), "skip_routing_sync": True}, ["events"]),
        ({"skip_snapshots": False, "skip_routing_sync": True}, ["events", "snapshots"]),
    ],
)
def test_sync_from_relay_org_flag_matrix(monkeypatch, kwargs, expect_phases):
    _patch_pull(monkeypatch)
    stats = {}
    om.sync_from_relay_org(
        "om_agent_test",
        after_id=5,
        quiet=True,
        stats=stats,
        **kwargs,
    )
    assert stats["pull_phases"] == expect_phases
    assert "pending_events" in stats
    assert "pending_snapshots" in stats
    assert "verdict" in stats


def test_parse_pull_kinds_events_matches_skip_snapshots(monkeypatch):
    """CLI --kind events should match skip_snapshots snapshot filtering."""
    _patch_pull(monkeypatch)

    kinds_stats = {}
    om.sync_from_relay_org(
        "om_agent_test",
        quiet=True,
        stats=kinds_stats,
        skip_routing_sync=True,
        pull_kinds=om.parse_pull_kinds("events"),
    )

    skip_stats = {}
    om.sync_from_relay_org(
        "om_agent_test",
        quiet=True,
        stats=skip_stats,
        skip_routing_sync=True,
        skip_snapshots=True,
    )

    assert kinds_stats["pull_phases"] == skip_stats["pull_phases"] == ["events"]
    assert kinds_stats["snapshot_pages"] == skip_stats["snapshot_pages"] == 0
