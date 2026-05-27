#!/usr/bin/env python3
"""Tests for db_health rules and archive lead resolution."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import db_health  # noqa: E402
import pipeline as om  # noqa: E402
import workspace_archive  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


def test_evaluate_rules_ok():
    status, rules = db_health.evaluate_health_rules(
        file_bytes=10_000_000,
        integrity_ok=True,
        row_counts={"leads": 100, "events": 50, "relay_ingested": 200, "cloud_pending": 0, "unmapped_campaign_queue": 0},
    )
    assert status == "ok"
    assert not any(r["id"] == "relay_bloat" for r in rules)


def test_evaluate_rules_size_warn():
    status, _ = db_health.evaluate_health_rules(
        file_bytes=600 * 1024 * 1024,
        integrity_ok=True,
        row_counts={"leads": 100, "events": 50, "relay_ingested": 100},
    )
    assert status == "warn"


def test_evaluate_rules_relay_bloat():
    status, rules = db_health.evaluate_health_rules(
        file_bytes=1000,
        integrity_ok=True,
        row_counts={"leads": 10, "events": 5, "relay_ingested": 60},
    )
    assert status == "warn"
    assert any(r["id"] == "relay_bloat" for r in rules)


def test_should_report_force():
    cfg = {}
    assert db_health.should_report_health("ok", lambda: cfg, force=True)


def test_archive_dry_run_empty_workspace():
    om.init_db()
    conn = om.get_conn()
    try:
        ids, meta = workspace_archive.resolve_archive_lead_ids(
            conn,
            DEFAULT_ORG_ID,
            "nonexistent-workspace-xyz",
            resolve_workspace_identity_fn=om.resolve_workspace_identity,
        )
        assert ids == set()
        assert meta["lead_count"] == 0
    finally:
        conn.close()


if __name__ == "__main__":
    test_evaluate_rules_ok()
    test_evaluate_rules_size_warn()
    test_evaluate_rules_relay_bloat()
    test_should_report_force()
    test_archive_dry_run_empty_workspace()
    print("ok")
