#!/usr/bin/env python3
"""Regression: a first-ever `pipeline.py pull` (no --full flag) must still be
treated as a full pull so the end-of-pull last_sync bump fires -- otherwise
freshly-backfilled data (companies, leads) looks like locally-pending changes
on the next `sync --dry-run` (crm-entity-map-fresh-pull-bug.md-adjacent report)."""

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
import pipeline_cli  # noqa: E402


def _patch_common(monkeypatch, *, last_max_id, sync_result=(0, 0)):
    calls = {}

    def fake_sync_from_relay_org(agent_key, **kwargs):
        calls["kwargs"] = kwargs
        return sync_result

    monkeypatch.setattr(om, "_require_agent_key", lambda: "om_agent_test")
    monkeypatch.setattr(om, "_warn_duplicate_installs", lambda: None)
    monkeypatch.setattr(om, "get_last_max_id", lambda: last_max_id)
    monkeypatch.setattr(om, "sync_from_relay_org", fake_sync_from_relay_org)
    monkeypatch.setattr(om, "migrate_db", lambda: None)
    monkeypatch.setattr(om, "sync_workspace_routing_mode_from_config", lambda: None)
    return calls


def test_first_pull_without_full_flag_is_treated_as_full(monkeypatch):
    om.init_db()
    calls = _patch_common(monkeypatch, last_max_id=0)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "pull", "--cron"])

    with pytest.raises(SystemExit):
        pipeline_cli.main()

    assert calls["kwargs"]["full"] is True
    assert calls["kwargs"]["after_id"] is None


def test_incremental_pull_without_full_flag_stays_incremental(monkeypatch):
    """A pull with an existing cursor (not the fresh-install case) must not
    be silently upgraded to full -- that would re-set last_sync on every
    routine pull and could mask locally-pending edits made since the last
    sync (the exact risk the source doc flagged)."""
    om.init_db()
    calls = _patch_common(monkeypatch, last_max_id=12345)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "pull", "--cron"])

    with pytest.raises(SystemExit):
        pipeline_cli.main()

    assert calls["kwargs"]["full"] is False
    assert calls["kwargs"]["after_id"] == 12345


def test_explicit_full_flag_still_works(monkeypatch):
    om.init_db()
    calls = _patch_common(monkeypatch, last_max_id=12345)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "pull", "--full", "--cron"])

    with pytest.raises(SystemExit):
        pipeline_cli.main()

    assert calls["kwargs"]["full"] is True
    assert calls["kwargs"]["after_id"] is None
