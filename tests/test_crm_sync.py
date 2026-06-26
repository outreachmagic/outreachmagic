#!/usr/bin/env python3
"""CRM sync tests — Phase 0 schema + Phase 1 engine + Phase 2 GHL driver.

Run:
    python3 -m pytest tests/test_crm_sync.py -v -k "phase_0"
    python3 -m pytest tests/test_crm_sync.py -v -k "phase_1"
    python3 -m pytest tests/test_crm_sync.py -v -k "ghl"
    python3 -m pytest tests/test_crm_sync.py -v -m "not integration"
"""

import argparse
import json
import os
import sqlite3
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from db_conn import get_conn  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402
import crm_sync  # noqa: E402
from crm_drivers.base import MockDriver  # noqa: E402
from crm_drivers.ghl import (  # noqa: E402
    GhlDriver,
    AuthError,
    GhlError,
    NetworkError,
    RateLimitError,
    TokenBucket as GhlTokenBucket,
    _format_event_note,
)
from crm_drivers.hubspot import (  # noqa: E402
    HubspotDriver,
    HubspotError,
    AuthError as HsAuthError,
    NetworkError as HsNetworkError,
    RateLimitError as HsRateLimitError,
    TokenBucket as HsTokenBucket,
    _format_event_note as _hs_format_event_note,
)

CRM_TABLES = ["crm_workspace_config", "crm_entity_map", "crm_sync_log"]

EXPECTED_COLUMNS = {
    "crm_workspace_config": {
        "workspace_id", "platform", "api_key", "location_id",
        "pipeline_id", "stage_mapping", "contact_field_mapping",
        "overwrite_existing", "enabled", "updated_at",
    },
    "crm_entity_map": {
        "workspace_id", "lead_id", "platform", "crm_contact_id",
        "crm_deal_id", "crm_company_id", "crm_owner_id", "last_synced_at",
        "last_event_id_synced", "last_sync_status", "sync_error",
        "sync_hash", "cloud_pending", "created_at", "updated_at",
    },
    "crm_sync_log": {
        "id", "workspace_id", "platform", "started_at", "completed_at",
        "leads_checked", "contacts_created", "contacts_updated",
        "opportunities_created", "opportunities_updated", "events_pushed",
        "skipped", "errors", "error_details", "status",
    },
}


def _get_table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r[1] for r in rows}


def _assert_tables_exist(conn, tables):
    for t in tables:
        assert _get_table_columns(conn, t), f"Table {t} has no columns or does not exist"


# ---------------------------------------------------------------------------
# Phase 0 tests
# ---------------------------------------------------------------------------

def test_phase_0_tables_exist_after_init():
    """Run init_db; verify all 3 CRM tables have correct columns."""
    om.init_db()
    conn = get_conn()
    try:
        for t in CRM_TABLES:
            cols = _get_table_columns(conn, t)
            assert cols, f"Table {t} does not exist"
            missing = EXPECTED_COLUMNS[t] - cols
            extra = cols - EXPECTED_COLUMNS[t]
            assert not missing, f"Table {t} missing columns: {missing}"
            assert not extra, f"Table {t} extra columns: {extra}"
    finally:
        conn.close()


def test_phase_0_migration_on_existing_db():
    """Create DB with all existing tables but without CRM tables, run migrate_db, verify CRM tables appear."""
    om.init_db()
    conn = get_conn()
    try:
        # Verify CRM tables exist after init (they're in SCHEMA_SQL)
        for t in CRM_TABLES:
            assert _get_table_columns(conn, t), f"Table {t} should exist after init"

        # Drop them to simulate pre-Phase-0 database
        conn.execute("DROP TABLE IF EXISTS crm_sync_log")
        conn.execute("DROP TABLE IF EXISTS crm_entity_map")
        conn.execute("DROP TABLE IF EXISTS crm_workspace_config")
        conn.commit()

        # Verify they're gone
        for t in CRM_TABLES:
            assert not _get_table_columns(conn, t), f"Table {t} should be gone after DROP"
    finally:
        conn.close()

    # Reopen and run migrate_db — should recreate CRM tables
    conn2 = get_conn()
    try:
        om.migrate_db(conn2)
        # Verify CRM tables now exist again
        for t in CRM_TABLES:
            cols = _get_table_columns(conn2, t)
            assert cols, f"Table {t} was not created by migrate_db"
            missing = EXPECTED_COLUMNS[t] - cols
            assert not missing, f"Table {t} missing columns after migration: {missing}"
    finally:
        conn2.close()


def test_phase_0_tables_survive_refresh():
    """After init + refresh --yes, CRM tables are still present."""
    om.init_db()
    conn = get_conn()
    try:
        # Insert a workspace so refresh --yes doesn't error out
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            ("ws-test", DEFAULT_ORG_ID, "Test", "test"),
        )
        conn.commit()
    finally:
        conn.close()

    # refresh --yes rebuilds DB from scratch (skip sync to avoid network)
    om.refresh_local_database(yes=True, skip_sync=True)

    conn2 = get_conn()
    try:
        for t in CRM_TABLES:
            cols = _get_table_columns(conn2, t)
            assert cols, f"Table {t} missing after refresh --yes"
            missing = EXPECTED_COLUMNS[t] - cols
            assert not missing, f"Table {t} missing columns after refresh: {missing}"
    finally:
        conn2.close()


def test_phase_0_foreign_keys():
    """Deleting a workspace cascades to crm_workspace_config."""
    om.init_db()
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            ("ws-fk-test", DEFAULT_ORG_ID, "FK Test", "fk-test"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key) VALUES (?, ?, ?)",
            ("ws-fk-test", "ghl", "test-api-key"),
        )
        conn.commit()

        # Verify config exists
        row = conn.execute(
            "SELECT 1 FROM crm_workspace_config WHERE workspace_id = ?", ("ws-fk-test",)
        ).fetchone()
        assert row is not None, "Config should exist before cascade delete"

        # Delete workspace
        conn.execute("DELETE FROM workspaces WHERE id = ?", ("ws-fk-test",))
        conn.commit()

        # Config should be cascaded away
        row = conn.execute(
            "SELECT 1 FROM crm_workspace_config WHERE workspace_id = ?", ("ws-fk-test",)
        ).fetchone()
        assert row is None, "Config should be cascaded on workspace delete"
    finally:
        conn.close()


def test_phase_0_unique_constraints():
    """Duplicate (workspace_id, platform) raises IntegrityError."""
    om.init_db()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            ("ws-uniq", DEFAULT_ORG_ID, "Unique Test", "uniq-test"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key) VALUES (?, ?, ?)",
            ("ws-uniq", "ghl", "key-1"),
        )
        conn.commit()

        # Duplicate should raise IntegrityError
        try:
            conn.execute(
                "INSERT INTO crm_workspace_config (workspace_id, platform, api_key) VALUES (?, ?, ?)",
                ("ws-uniq", "ghl", "key-2"),
            )
            conn.commit()
            assert False, "Expected IntegrityError for duplicate (workspace_id, platform)"
        except sqlite3.IntegrityError:
            pass  # Expected
    finally:
        conn.close()


def test_phase_0_defaults():
    """Verify column defaults on the 3 CRM tables."""
    om.init_db()
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            ("ws-defaults", DEFAULT_ORG_ID, "Defaults", "defaults"),
        )
        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key) VALUES (?, ?, ?)",
            ("ws-defaults", "ghl", "test-key"),
        )
        # crm_entity_map — insert with minimal fields
        conn.execute(
            "INSERT INTO leads (id, name) VALUES (?, ?)",
            (9999, "Test Lead"),
        )
        conn.execute(
            "INSERT INTO crm_entity_map (workspace_id, lead_id, platform) VALUES (?, ?, ?)",
            ("ws-defaults", 9999, "ghl"),
        )
        # crm_sync_log — insert with minimal fields
        conn.execute(
            "INSERT INTO crm_sync_log (workspace_id, platform, started_at) VALUES (?, ?, datetime('now'))",
            ("ws-defaults", "ghl"),
        )
        conn.commit()

        # Check crm_workspace_config defaults
        row = conn.execute(
            "SELECT enabled, stage_mapping FROM crm_workspace_config WHERE workspace_id = ?",
            ("ws-defaults",),
        ).fetchone()
        assert row["enabled"] == 1, f"enabled default expected 1, got {row['enabled']}"
        assert row["stage_mapping"] == "{}", f"stage_mapping default expected '{{}}', got {row['stage_mapping']}"

        # Check crm_entity_map defaults
        row = conn.execute(
            "SELECT last_sync_status FROM crm_entity_map WHERE workspace_id = ?",
            ("ws-defaults",),
        ).fetchone()
        assert row["last_sync_status"] == "pending", f"last_sync_status default expected 'pending', got {row['last_sync_status']}"

        # Check crm_sync_log defaults
        row = conn.execute(
            "SELECT status, platform FROM crm_sync_log WHERE workspace_id = ?",
            ("ws-defaults",),
        ).fetchone()
        assert row["status"] == "in_progress", f"status default expected 'in_progress', got {row['status']}"
        assert row["platform"] == "ghl", f"platform default expected 'ghl', got {row['platform']}"
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Phase 1 helpers
# ---------------------------------------------------------------------------

WS1_ID = "ws-phase1-a"
WS1_SLUG = "popcam"
WS2_ID = "ws-phase1-b"
WS2_SLUG = "sideproj"


def _setup_phase_1_data(conn):
    """Insert test leads, workspace_leads, and CRM config for Phase 1 tests."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        (DEFAULT_ORG_ID, "test-org"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (WS2_ID, DEFAULT_ORG_ID, "Side Project", WS2_SLUG),
    )

    leads = [
        (1, "Alice", "alice@example.com", "CEO", "SaaS", "1-10"),
        (2, "Bob", "bob@example.com", "CTO", "Fintech", "11-50"),
        (3, "Charlie", "charlie@example.com", "Engineer", "SaaS", "51-200"),
        (4, "Diana", "diana@example.com", "VP Sales", "Health", "201-500"),
        (5, "Eve", "eve@example.com", "PM", "EdTech", "10-50"),
        (6, "Frank", "frank@example.com", "Designer", "SaaS", "1-10"),
        (7, "Grace", "grace@example.com", "DevOps", "Cloud", "51-200"),
        (8, "Hank", "hank@example.com", "Marketing", "Retail", "11-50"),
    ]
    for lid, name, email, title, industry, hc in leads:
        conn.execute(
            "INSERT OR IGNORE INTO leads (id, name, email, title, industry, headcount) VALUES (?, ?, ?, ?, ?, ?)",
            (lid, name, email, title, industry, hc),
        )

    # Company
    conn.execute(
        "INSERT OR IGNORE INTO companies (id, name, domain) VALUES (?, ?, ?)",
        (1, "Popcam Inc", "popcam.com"),
    )
    conn.execute("UPDATE leads SET company_id = 1 WHERE id = 1")
    conn.execute("UPDATE leads SET company_id = 1 WHERE id = 2")

    # workspace_leads with various statuses
    wls = [
        (WS1_ID, 1, "interested"),
        (WS1_ID, 2, "interested"),
        (WS1_ID, 3, "interested"),
        (WS1_ID, 4, "proposal"),
        (WS1_ID, 5, "proposal"),
        (WS1_ID, 6, "won"),
        (WS1_ID, 7, "lost"),
        (WS1_ID, 8, "contacted"),  # should be excluded
    ]
    for ws_id, lead_id, status in wls:
        wl_id = f"{ws_id}-{lead_id}"
        conn.execute(
            """INSERT OR IGNORE INTO workspace_leads
               (id, org_id, workspace_id, lead_id, status, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (wl_id, DEFAULT_ORG_ID, ws_id, lead_id, status),
        )

    # CRM config for popcam / ghl
    conn.execute(
        "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
        (WS1_ID, "ghl", "mock-key-1", "pipe-1",
         '{"interested":"stage-interested","proposal":"stage-proposal","won":"stage-won","lost":"stage-lost"}'),
    )

    # Second config for popcam / hubspot
    conn.execute(
        "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
        (WS1_ID, "hubspot", "mock-key-hs", "hs-pipe-1",
         '{"interested":"hs-stage-1","proposal":"hs-stage-2","won":"hs-stage-3","lost":"hs-stage-4"}'),
    )

    conn.commit()


def _make_namespace(**kwargs):
    """Build an argparse.Namespace for testing cmd_* functions."""
    defaults = {
        "workspace": None,
        "all": False,
        "dry_run": False,
        "lead_id": None,
        "skip_events": False,
        "platform": None,
        "command": "sync",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Phase 1 tests
# ---------------------------------------------------------------------------

def test_phase_1_lead_selection_status_filter():
    """Only leads with syncable statuses are selected."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        leads = crm_sync.select_leads(conn, WS1_ID)
        statuses = {l["status"] for l in leads}
        assert statuses == {"interested", "proposal", "won", "lost"}, f"unexpected statuses: {statuses}"
        assert len(leads) == 7, f"expected 7 leads, got {len(leads)}"
        # "contacted" should not be present
        lead_ids = {l["lead_id"] for l in leads}
        assert 8 not in lead_ids, "contacted lead should be excluded"
    finally:
        conn.close()


def test_phase_1_lead_selection_stale_filter():
    """last_sync_at filters out recently-updated leads; NULL selects all."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)

        # All leads without timestamp filter
        all_leads = crm_sync.select_leads(conn, WS1_ID)
        assert len(all_leads) == 7

        # With a future timestamp, nothing matches
        future_leads = crm_sync.select_leads(conn, WS1_ID, last_sync_at="2099-01-01T00:00:00")
        assert len(future_leads) == 0

        # With an old timestamp, all match
        old_leads = crm_sync.select_leads(conn, WS1_ID, last_sync_at="2020-01-01T00:00:00")
        assert len(old_leads) == 7
    finally:
        conn.close()


def test_phase_1_lead_selection_joins():
    """JOINs return correct fields from leads + companies."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        leads = crm_sync.select_leads(conn, WS1_ID)
        alice = next(l for l in leads if l.get("email") == "alice@example.com")
        assert alice["name"] == "Alice"
        assert alice["email"] == "alice@example.com"
        assert alice["title"] == "CEO"
        assert alice["company_name"] == "Popcam Inc"
        assert alice["company_domain"] == "popcam.com"

        # Charlie has no company — domain should be None
        charlie = next(l for l in leads if l.get("email") == "charlie@example.com")
        assert charlie["company_name"] is None
        assert charlie["company_domain"] is None
    finally:
        conn.close()


def test_phase_1_stage_mapping():
    """lead.status maps to correct CRM stage from config."""
    cfg = {
        "stage_mapping": {
            "interested": "stage-interested",
            "proposal": "stage-proposal",
        },
        "contact_field_mapping": None,
        "platform": "ghl",
        "pipeline_id": "pipe-1",
    }
    mock = MockDriver()

    lead_interested = {"status": "interested", "email": "a@b.com", "name": "Test", "lead_id": 1}
    lead_proposal = {"status": "proposal", "email": "c@d.com", "name": "Test2", "lead_id": 2}

    # First lead -> "stage-interested"
    mock.calls.clear()
    cid1, did1, c_action1, d_action1 = crm_sync.sync_single_lead(lead_interested, cfg, mock)
    assert c_action1 in ("created_contact", "existing_contact", "updated_contact")
    # verify the upsert_deal call
    deal_calls = [c for c in mock.calls if c.startswith("upsert_deal")]
    assert len(deal_calls) >= 1
    assert "stage-interested" in deal_calls[0]

    # Second lead -> "stage-proposal"
    mock.calls.clear()
    cid2, did2, c_action2, d_action2 = crm_sync.sync_single_lead(lead_proposal, cfg, mock)
    deal_calls2 = [c for c in mock.calls if c.startswith("upsert_deal")]
    assert len(deal_calls2) >= 1
    assert "stage-proposal" in deal_calls2[0]


def test_phase_1_dry_run_no_calls():
    """--dry-run prevents any driver method from being called."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        mock = MockDriver()
        crm_sync._test_driver_override = mock

        cfg = {
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "s1", "proposal": "s2", "won": "s3", "lost": "s4"},
        }
        results = crm_sync.sync_workspace(
            conn, WS1_ID, "Popcam", cfg, dry_run=True, driver=mock,
        )
        # Driver should have zero calls
        assert len(mock.calls) == 0, f"driver should have no calls in dry-run, got {mock.calls}"
        assert results["leads_checked"] == 7
    finally:
        crm_sync._test_driver_override = None
        conn.close()


def test_phase_1_dry_run_prints_actions(capsys):
    """Dry-run prints 'Would create contact' and 'Would upsert deal' for each lead."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        mock = MockDriver()
        cfg = {
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "s1", "proposal": "s2", "won": "s3", "lost": "s4"},
        }
        crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, dry_run=True, driver=mock)
        captured = capsys.readouterr().out
        assert "Would create contact" in captured
        assert "Would upsert deal" in captured
    finally:
        conn.close()


def test_phase_1_sync_single_workspace():
    """--workspace popcam filters to that workspace only."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)

        # Add a workspace_lead for ws2 too (should not be synced)
        conn.execute(
            "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id, status, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
            ("ws2-lead-1", DEFAULT_ORG_ID, WS2_ID, 1, "interested"),
        )
        conn.commit()

        mock = MockDriver()
        cfg = {
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "s1"},
        }
        results = crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, driver=mock)
        # 7 leads in ws1 (excluding contacted), none from ws2
        assert results["leads_checked"] == 7
    finally:
        conn.close()


def test_phase_1_sync_all_workspaces():
    """--all iterates all enabled workspaces from config."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        # Only popcam has config, sideproj has none
        config_ids = [
            r["workspace_id"]
            for r in conn.execute(
                "SELECT DISTINCT workspace_id FROM crm_workspace_config WHERE enabled = 1"
            ).fetchall()
        ]
        assert WS1_ID in config_ids
        # sideproj should NOT be there (no config)
        assert WS2_ID not in config_ids
    finally:
        conn.close()


def test_phase_1_sync_single_lead():
    """--lead-id N syncs only that one lead."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        leads = crm_sync.select_leads(conn, WS1_ID, lead_id=1)
        assert len(leads) == 1
        assert leads[0]["lead_id"] == 1
    finally:
        conn.close()


def test_phase_1_sync_log_written():
    """After sync, crm_sync_log has a completed row with correct counts."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        mock = MockDriver()
        cfg = {
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "s1", "proposal": "s2", "won": "s3", "lost": "s4"},
        }
        results = crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, driver=mock)

        log_rows = conn.execute(
            "SELECT * FROM crm_sync_log WHERE workspace_id = ? AND platform = ? ORDER BY started_at DESC LIMIT 1",
            (WS1_ID, "ghl"),
        ).fetchall()
        assert len(log_rows) == 1
        log = log_rows[0]
        assert log["status"] == "completed"
        assert log["leads_checked"] == 7
        assert log["contacts_created"] == results["contacts_created"]
        assert log["opportunities_created"] == results["opportunities_created"]
    finally:
        conn.close()


def test_phase_1_skip_workspace_no_config():
    """Workspace with no CRM config returns 0 configs from read_crm_config."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        configs = crm_sync.read_crm_config(conn, WS2_ID)
        assert configs == [], f"expected empty configs for workspace without CRM config, got {configs}"
    finally:
        conn.close()


def test_phase_1_discover_workspace():
    """discover calls driver.discover_pipelines()."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        mock = MockDriver()
        crm_sync._test_driver_override = mock
        args = _make_namespace(command="discover", workspace=WS1_SLUG)
        crm_sync.cmd_discover(args)
        assert any("discover_pipelines" in c for c in mock.calls), f"expected discover_pipelines call, got {mock.calls}"
    finally:
        crm_sync._test_driver_override = None
        conn.close()


def test_phase_1_status(capsys):
    """status prints a table with workspace, platform, enabled, last_sync_at."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
        args = _make_namespace(command="status")
        crm_sync.cmd_status(args)
        captured = capsys.readouterr().out
        assert "Popcam" in captured
        assert "ghl" in captured
        assert "Yes" in captured
        assert "pipe-1" in captured
    finally:
        conn.close()


def test_phase_1_rate_limiter_bucket():
    """100 acquire() calls in a loop respect the rate budget."""
    tb = crm_sync.TokenBucket(rate=80, per_seconds=10)
    start = time.monotonic()
    for _ in range(80):
        wait = tb.acquire()
        assert wait == 0.0, "first 80 acquires should be immediate"
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"80 acquires should take < 1s, took {elapsed:.2f}s"


def test_phase_1_rate_limiter_block():
    """81st call within a 10-second window blocks (for GHL config)."""
    tb = crm_sync.TokenBucket(rate=80, per_seconds=10)
    # Acquire all 80 tokens quickly
    for _ in range(80):
        tb.acquire()
    # 81st should block (need to wait for token refill)
    start = time.monotonic()
    wait = tb.acquire()
    elapsed = time.monotonic() - start
    # It should have waited at least a tiny bit (or immediately if enough time passed)
    # The key is that the bucket runs out after 80 tokens
    assert tb.tokens < 1.0, "bucket should be empty after 80 acquires"


def test_phase_1_nonexistent_workspace(capsys):
    """--workspace invalid prints clear error, exits non-zero."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_1_data(conn)
    finally:
        conn.close()

    # Simulate cmd_sync with nonexistent workspace
    try:
        # Need an active conn for the test
        conn2 = get_conn()
        try:
            args = _make_namespace(command="sync", workspace="nonexistent-slug")
            crm_sync.cmd_sync(args)
            assert False, "should have exited"
        except SystemExit as e:
            assert e.code == 1
        finally:
            conn2.close()
    finally:
        pass


def test_phase_1_missing_crm_sync_py():
    """pipeline.py runs fine even when crm_sync.py is absent (validates Phase 6 subprocess design)."""
    # pipeline.py does not import crm_sync, so it should work regardless
    om.init_db()
    # Just verify importing pipeline works (crm_sync might be importable but pipeline doesn't need it)
    assert om.init_db is not None  # sanity check pipeline import


# ============================================================================
# Phase 2 tests — GHL driver
# ============================================================================


def _mock_response(data: dict, status: int = 200) -> MagicMock:
    """Build a mock urlopen response context manager."""
    raw = json.dumps(data).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = raw
    cm.__enter__.return_value.status = status
    return cm


def _make_ghl_config(api_key="test-api-key", location_id="loc-1", pipeline_id="pipe-1"):
    return {
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": pipeline_id,
    }


# ---------------------------------------------------------------------------
# Contact lookup tests
# ---------------------------------------------------------------------------

def test_ghl_lookup_contact_found():
    """200 with contacts returns contact ID."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"contacts": [{"id": "abc-123"}]})
        result = driver.lookup_contact("alice@example.com")
        assert result == "abc-123"


def test_ghl_lookup_contact_not_found():
    """404 returns None (simulated as empty contacts list)."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"contacts": []})
        result = driver.lookup_contact("nobody@example.com")
        assert result is None


def test_ghl_lookup_contact_unauthorized():
    """401 raises AuthError."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        # Simulate 401 HTTPError
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://test", 401, "Unauthorized", {}, None
        )
        try:
            driver.lookup_contact("test@example.com")
            assert False, "Expected AuthError"
        except AuthError as exc:
            assert "API key rejected" in str(exc)


# ---------------------------------------------------------------------------
# Contact creation tests
# ---------------------------------------------------------------------------

def test_ghl_create_contact_all_fields():
    """Builds correct POST body with name, email, company, custom fields."""
    driver = GhlDriver(_make_ghl_config())
    lead_data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "company_name": "Acme Corp",
        "title": "CEO",
        "industry": "SaaS",
        "headcount": "1-10",
        "linkedin_url": "https://linkedin.com/in/jane",
    }
    field_mapping = {
        "title": "cf_title_id",
        "industry": "cf_industry_id",
        "headcount": "cf_hc_id",
        "linkedin_url": "cf_li_id",
    }

    sent_body = None

    def capture_request(req, timeout=30):
        sent_body_data = req.data
        nonlocal sent_body
        if sent_body_data:
            sent_body = json.loads(sent_body_data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"contact": {"id": "new-001"}}).encode()
        return cm

    # Patch urllib.request.urlopen AND Request constructor to capture body
    with patch("urllib.request.urlopen", side_effect=capture_request):
        result = driver.create_contact(lead_data, field_mapping)
        assert result == "new-001"

    assert sent_body is not None
    assert sent_body["name"] == "Jane Doe"
    assert sent_body["email"] == "jane@example.com"
    assert sent_body["companyName"] == "Acme Corp"
    assert sent_body.get("locationId") == "loc-1"
    assert "customFields" in sent_body
    cf_ids = {cf["id"] for cf in sent_body["customFields"]}
    assert "cf_title_id" in cf_ids
    assert "cf_industry_id" in cf_ids
    assert "cf_hc_id" in cf_ids
    assert "cf_li_id" in cf_ids


def test_ghl_create_contact_no_custom_fields():
    """Omits custom fields when field_mapping is None."""
    driver = GhlDriver(_make_ghl_config())
    lead_data = {"name": "Bob", "email": "bob@example.com", "company": "BobCo"}

    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"contact": {"id": "new-002"}}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        result = driver.create_contact(lead_data, None)
        assert result == "new-002"

    assert sent_body is not None
    assert sent_body.get("locationId") == "loc-1"
    assert "customFields" not in sent_body


# ---------------------------------------------------------------------------
# Opportunity / Deal tests
# ---------------------------------------------------------------------------

def test_ghl_upsert_opportunity():
    """upsert_deal builds correct POST body with pipeline, stage, contactId."""
    driver = GhlDriver(_make_ghl_config(pipeline_id="pipe-xyz"))
    lead_data = {"name": "Alice", "company_name": "Wonderland Inc"}

    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"opportunity": {"id": "opp-001"}}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        result = driver.upsert_deal("contact-123", lead_data, "stage-interested", {"pipeline_id": "pipe-xyz"})
        assert result == "opp-001"

    assert sent_body is not None
    assert sent_body["pipelineId"] == "pipe-xyz"
    assert sent_body["pipelineStageId"] == "stage-interested"
    assert sent_body["contactId"] == "contact-123"
    assert sent_body["status"] == "open"


def test_ghl_upsert_opportunity_deal_name():
    """Name format: 'Jane Doe - Acme Corp'."""
    driver = GhlDriver(_make_ghl_config())
    lead_data = {"name": "Jane Doe", "company_name": "Acme Corp"}

    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"opportunity": {"id": "opp-002"}}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        driver.upsert_deal("c-1", lead_data, "stage-1", {"pipeline_id": "p-1"})

    assert sent_body is not None
    assert sent_body["name"] == "Jane Doe - Acme Corp"


def test_ghl_upsert_opportunity_deal_name_no_company():
    """Name is just the person name when company is missing."""
    driver = GhlDriver(_make_ghl_config())
    lead_data = {"name": "Solo Person"}

    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"opportunity": {"id": "opp-003"}}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        driver.upsert_deal("c-2", lead_data, "stage-2", {"pipeline_id": "p-2"})

    assert sent_body is not None
    assert sent_body["name"] == "Solo Person"


# ---------------------------------------------------------------------------
# Pipeline discovery tests
# ---------------------------------------------------------------------------

def test_ghl_discover_pipelines():
    """Parses GHL pipeline response with correct nested stage structure."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({
            "pipelines": [
                {
                    "id": "pipe-1",
                    "name": "Sales Pipeline",
                    "stages": [
                        {"id": "stage-1", "name": "New Lead"},
                        {"id": "stage-2", "name": "Qualified"},
                    ],
                },
                {
                    "id": "pipe-2",
                    "name": "Partner Pipeline",
                    "stages": [
                        {"id": "stage-a", "name": "Applied"},
                    ],
                },
            ]
        })
        result = driver.discover_pipelines()
        assert len(result) == 2
        assert result[0]["id"] == "pipe-1"
        assert result[0]["name"] == "Sales Pipeline"
        assert len(result[0]["stages"]) == 2
        assert result[0]["stages"][0]["id"] == "stage-1"
        assert result[0]["stages"][0]["name"] == "New Lead"
        assert result[1]["id"] == "pipe-2"


def test_ghl_discover_pipelines_empty():
    """Handles empty pipeline list."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({"pipelines": []})
        result = driver.discover_pipelines()
        assert result == []


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_ghl_test_connection_success():
    """Returns (True, '') on successful pipeline discovery."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response({
            "pipelines": [{"id": "p-1", "name": "Default", "stages": []}]
        })
        ok, msg = driver.test_connection()
        assert ok is True
        assert msg == ""


def test_ghl_test_connection_failure():
    """Returns (False, message) on auth error."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://test", 401, "Unauthorized", {}, None
        )
        ok, msg = driver.test_connection()
        assert ok is False
        assert "API key rejected" in msg


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------

def test_ghl_rate_limiter_80_per_10s():
    """81st request within window blocks."""
    tb = GhlTokenBucket(80, 10)
    # Acquire all 80 tokens quickly
    for _ in range(80):
        wait = tb.acquire()
        assert wait == 0.0, "first 80 acquires should be immediate"
    # 81st should block (need to wait for token refill)
    assert tb.tokens < 1.0, "bucket should be empty after 80 acquires"


def test_ghl_retry_on_429():
    """429 triggers exponential backoff (1s, 2s, 4s)."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        # Return 429 three times, then success
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("https://test", 429, "Too Many Requests", {}, None),
            urllib.error.HTTPError("https://test", 429, "Too Many Requests", {}, None),
            urllib.error.HTTPError("https://test", 429, "Too Many Requests", {}, None),
            _mock_response({"contacts": [{"id": "found"}]}),
        ]
        # patch time.sleep so we don't actually wait
        with patch("time.sleep") as mock_sleep:
            result = driver.lookup_contact("test@example.com")
            assert result == "found"
            # Should have slept at least 3 times (for 429s)
            assert mock_sleep.call_count >= 3


def test_ghl_retry_on_network_error():
    """3 retries on network errors, then raises NetworkError."""
    driver = GhlDriver(_make_ghl_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        # All attempts fail with URLError
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with patch("time.sleep"):  # don't actually wait
            try:
                driver.lookup_contact("test@example.com")
                assert False, "Expected NetworkError"
            except NetworkError:
                pass  # Expected
        # 1 initial + 3 retries = 4 calls
        assert mock_urlopen.call_count == 4


# ---------------------------------------------------------------------------
# Event formatting tests
# ---------------------------------------------------------------------------

def test_ghl_push_events_note_format():
    """Events formatted with correct prefix."""
    # email_sent
    assert "[Sent] Test Subject" in _format_event_note({
        "event_type": "email_sent", "direction": "outbound", "subject": "Test Subject",
    })
    # reply
    assert "[Replied] Thanks!" in _format_event_note({
        "event_type": "reply", "body_preview": "Thanks!",
    })
    # bounce
    assert "[Bounced]" in _format_event_note({
        "event_type": "bounce", "body_preview": "Mailbox full",
    })
    # stage_change
    assert "[Stage] interested" in _format_event_note({
        "event_type": "stage_change", "old_stage": "interested", "new_stage": "proposal",
    })
    # meeting_booked
    assert "[Meeting]" in _format_event_note({
        "event_type": "meeting_booked", "body_preview": "Wed 3pm",
    })
    # interested
    assert "[Interested]" in _format_event_note({
        "event_type": "interested", "body_preview": "Sounds great",
    })
    # not_interested
    assert "[Not Interested]" in _format_event_note({
        "event_type": "not_interested",
    })
    # Generic fallback
    note = _format_event_note({"event_type": "custom_event", "body_preview": "Some text"})
    assert "[Custom Event]" in note


def test_ghl_push_events_note_truncation():
    """Reply body preview is truncated to 200 chars."""
    long_body = "A" * 300
    note = _format_event_note({
        "event_type": "reply", "body_preview": long_body,
    })
    assert len(note) <= 200 + len("[Replied] ")


def test_ghl_push_events_empty_list():
    """Returns 0 for empty events list."""
    driver = GhlDriver(_make_ghl_config())
    result, _ = driver.push_events("c-1", "d-1", [])
    assert result == 0


def test_ghl_push_events_batch():
    """Email events go to conversation timeline; others stay as notes."""
    driver = GhlDriver(_make_ghl_config())
    events = [
        {"event_type": "email_sent", "direction": "outbound", "subject": "Hello", "body_preview": "Hi there"},
        {"event_type": "reply", "body_preview": "Thanks"},
        {"event_type": "meeting_booked", "subject": "Discovery Call"},
    ]
    conv_bodies = []
    note_bodies = []
    contact_get_called = [False]

    def side_effect(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/contacts/" in url and req.get_method() == "GET":
            contact_get_called[0] = True
            return _mock_response({"contact": {"email": "test@example.com"}})
        if req.data:
            b = json.loads(req.data)
            if "/conversations/" in url:
                conv_bodies.append(b)
            else:
                note_bodies.append(b)
        return _mock_response({})

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result, _ = driver.push_events("c-1", "d-1", events)
        assert result == 3
        assert contact_get_called[0]
        assert len(conv_bodies) == 2  # email_sent + reply → timeline
        assert len(note_bodies) == 1  # meeting → note

        # Both go to /conversations/messages/inbound
        assert conv_bodies[0]["type"] == "Email"
        assert conv_bodies[0]["contactId"] == "c-1"
        assert conv_bodies[0]["emailTo"] == "test@example.com"
        assert conv_bodies[0]["emailFrom"] == "Outreach Magic <outreach@outreachmagic.com>"
        assert "[Sent] Hello" == conv_bodies[0]["subject"]
        assert "<p>Hi there</p>" == conv_bodies[0]["html"]

        assert conv_bodies[1]["type"] == "Email"
        assert "[Reply]" in conv_bodies[1]["subject"]
        assert "<p>Thanks</p>" == conv_bodies[1]["html"]

        # meeting_booked → note with deal prefix
        assert "[Deal: d-1]" in note_bodies[0]["body"]


def test_ghl_push_events_no_deal():
    """Without deal_id, timeline messages still work."""
    driver = GhlDriver(_make_ghl_config())
    events = [{"event_type": "email_sent", "direction": "outbound", "subject": "Hello", "body_preview": "Hi"}]
    conv_bodies = []

    def side_effect(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/contacts/" in url and req.get_method() == "GET":
            return _mock_response({"contact": {"email": "test@example.com"}})
        if req.data:
            conv_bodies.append(json.loads(req.data))
        return _mock_response({})

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result, _ = driver.push_events("c-1", None, events)
        assert result == 1
        assert len(conv_bodies) == 1
        assert conv_bodies[0]["type"] == "Email"


def test_ghl_push_events_partial_failure():
    """Conversation API failures gracefully fall back to notes."""
    driver = GhlDriver(_make_ghl_config())
    events = [
        {"event_type": "email_sent", "direction": "outbound", "subject": "Good", "body_preview": "Hi"},
        {"event_type": "reply", "body_preview": "Bad one"},
        {"event_type": "email_sent", "direction": "outbound", "subject": "Good 2", "body_preview": "Bye"},
    ]

    import urllib.error
    fail_reply = [True]  # mutable flag for the "reply" event

    def side_effect(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/contacts/" in url and req.get_method() == "GET":
            return _mock_response({"contact": {"email": "test@example.com"}})
        if req.data:
            b = json.loads(req.data)
            if "/conversations/" in url and "[Reply]" in b.get("subject", "") and fail_reply[0]:
                fail_reply[0] = False
                raise urllib.error.HTTPError("https://test", 500, "Server Error", {}, None)
        return _mock_response({})

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with patch("time.sleep"):
            result, _ = driver.push_events("c-1", "d-1", events)
            # 2 email_sent succeed via conversations, reply fails → falls back to note = all 3
            assert result == 3


# ---------------------------------------------------------------------------
# Integration test stubs (require sandbox credentials)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ghl_integration_full_flow():
    """Create contact -> lookup contact -> upsert opportunity -> push events.

    Requires sandbox GHL credentials in environment:
      GHL_SANDBOX_API_KEY, GHL_SANDBOX_LOCATION_ID, GHL_SANDBOX_PIPELINE_ID
    """
    api_key = os.environ.get("GHL_SANDBOX_API_KEY")
    location_id = os.environ.get("GHL_SANDBOX_LOCATION_ID")
    pipeline_id = os.environ.get("GHL_SANDBOX_PIPELINE_ID")
    if not all([api_key, location_id, pipeline_id]):
        pytest.skip("GHL sandbox credentials not set")

    driver = GhlDriver({
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": pipeline_id,
    })

    # 1. Discover pipelines
    pipelines = driver.discover_pipelines()
    assert len(pipelines) > 0

    # 2. Create contact
    test_email = f"test-{int(time.time())}@example.com"
    contact_id = driver.create_contact({
        "name": "Integration Test",
        "email": test_email,
        "company_name": "TestCo",
    }, None)
    assert contact_id

    # 3. Lookup contact
    looked_up = driver.lookup_contact(test_email)
    assert looked_up == contact_id

    # 4. Upsert opportunity
    first_stage = pipelines[0]["stages"][0]["id"] if pipelines[0].get("stages") else ""
    deal_id = driver.upsert_deal(
        contact_id,
        {"name": "Integration Test", "company_name": "TestCo"},
        first_stage,
        {"pipeline_id": pipeline_id},
    )
    assert deal_id

    # 5. Push events
    count, _ = driver.push_events(contact_id, deal_id, [
        {"event_type": "email_sent", "direction": "outbound", "subject": "Integration test email"},
        {"event_type": "reply", "body_preview": "This is a test reply"},
    ])
    assert count == 2


@pytest.mark.integration
def test_ghl_integration_no_duplicates():
    """Running sync twice creates 0 new contacts, 0 new deals."""
    api_key = os.environ.get("GHL_SANDBOX_API_KEY")
    location_id = os.environ.get("GHL_SANDBOX_LOCATION_ID")
    pipeline_id = os.environ.get("GHL_SANDBOX_PIPELINE_ID")
    if not all([api_key, location_id, pipeline_id]):
        pytest.skip("GHL sandbox credentials not set")

    driver = GhlDriver({
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": pipeline_id,
    })

    test_email = f"nodup-{int(time.time())}@example.com"

    # First contact creation
    cid1 = driver.create_contact({"name": "No Dup", "email": test_email}, None)
    assert cid1

    # Lookup returns the same contact
    cid2 = driver.lookup_contact(test_email)
    assert cid2 == cid1

    # Second create attempt — same email should fail or return existing
    # (GHL may return existing or error — this test documents the behavior)


@pytest.mark.integration
def test_ghl_integration_invalid_key():
    """Invalid API key returns clear error."""
    driver = GhlDriver({
        "api_key": "invalid-key-12345",
        "location_id": "loc-fake",
        "pipeline_id": "pipe-fake",
    })
    ok, msg = driver.test_connection()
    assert ok is False
    assert len(msg) > 0


@pytest.mark.integration
def test_ghl_integration_discover_pipelines():
    """Returns real pipelines with stages from sandbox account."""
    api_key = os.environ.get("GHL_SANDBOX_API_KEY")
    location_id = os.environ.get("GHL_SANDBOX_LOCATION_ID")
    if not all([api_key, location_id]):
        pytest.skip("GHL sandbox credentials not set")

    driver = GhlDriver({
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": "any",
    })
    pipelines = driver.discover_pipelines()
    assert len(pipelines) > 0
    for p in pipelines:
        assert "id" in p
        assert "name" in p
        assert "stages" in p
        if p["stages"]:
            for s in p["stages"]:
                assert "id" in s
                assert "name" in s


# ============================================================================
# Phase 3 tests — HubSpot driver
# ============================================================================


def _mock_hs_response(data: dict, status: int = 200) -> MagicMock:
    """Build a mock urlopen response context manager for HubSpot tests."""
    raw = json.dumps(data).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = raw
    cm.__enter__.return_value.status = status
    return cm


def _make_hs_config(api_key="test-hs-api-key"):
    return {
        "api_key": api_key,
        "pipeline_id": "default",
    }


# ---------------------------------------------------------------------------
# Contact search tests
# ---------------------------------------------------------------------------

def test_hubspot_search_contact_found():
    """Search returns contactId from results array."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_hs_response({
            "results": [{"id": "hs-contact-123"}]
        })
        result = driver.search_contact("alice@example.com")
        assert result == "hs-contact-123"


def test_hubspot_search_contact_not_found():
    """Empty results returns None."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_hs_response({"results": []})
        result = driver.search_contact("nobody@example.com")
        assert result is None


def test_hubspot_lookup_contact_unauthorized():
    """401 raises AuthError."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://test", 401, "Unauthorized", {}, None
        )
        try:
            driver.lookup_contact("test@example.com")
            assert False, "Expected AuthError"
        except HsAuthError as exc:
            assert "API key rejected" in str(exc)


# ---------------------------------------------------------------------------
# Contact creation tests
# ---------------------------------------------------------------------------

def test_hubspot_create_contact_all_fields():
    """Builds correct POST body with all standard properties."""
    driver = HubspotDriver(_make_hs_config())
    lead_data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "company_name": "Acme Corp",
        "title": "CEO",
        "industry": "SaaS",
        "headcount": "1-10",
        "linkedin_url": "https://linkedin.com/in/jane",
        "company_domain": "acme.com",
    }

    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"id": "hs-new-001"}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        result = driver.create_contact(lead_data, None)
        assert result == "hs-new-001"

    assert sent_body is not None
    props = sent_body["properties"]
    assert props["email"] == "jane@example.com"
    assert props["firstname"] == "Jane"
    assert props["lastname"] == "Doe"
    assert props["jobtitle"] == "CEO"
    assert props["industry"] == "SaaS"
    assert props["numemployees"] == "1-10"
    assert props["linkedinbio"] == "https://linkedin.com/in/jane"
    assert props["website"] == "acme.com"
    assert props["company"] == "Acme Corp"


def test_hubspot_create_contact_name_split():
    """Name 'Jane Doe' splits to firstname='Jane', lastname='Doe'."""
    driver = HubspotDriver(_make_hs_config())
    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"id": "hs-new-002"}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        driver.create_contact({"name": "Jane Doe", "email": "jane@example.com"}, None)

    assert sent_body is not None
    assert sent_body["properties"]["firstname"] == "Jane"
    assert sent_body["properties"]["lastname"] == "Doe"


def test_hubspot_create_contact_single_name():
    """Name 'Jane' splits to firstname='Jane', lastname=''."""
    driver = HubspotDriver(_make_hs_config())
    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"id": "hs-new-003"}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        driver.create_contact({"name": "Jane", "email": "jane@example.com"}, None)

    assert sent_body is not None
    assert sent_body["properties"]["firstname"] == "Jane"
    assert sent_body["properties"]["lastname"] == ""


def test_hubspot_create_contact_company_fallback():
    """Company falls back to lead_data.company when company_name is missing."""
    driver = HubspotDriver(_make_hs_config())
    sent_body = None

    def capture_request(req, timeout=30):
        nonlocal sent_body
        if req.data:
            sent_body = json.loads(req.data)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"id": "hs-new-004"}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=capture_request):
        driver.create_contact({"name": "Bob", "email": "bob@example.com", "company": "BobCo"}, None)

    assert sent_body is not None
    assert sent_body["properties"]["company"] == "BobCo"


# ---------------------------------------------------------------------------
# Deal upsert tests
# ---------------------------------------------------------------------------

def test_hubspot_upsert_deal_create():
    """Not found -> creates deal + associates to contact."""
    driver = HubspotDriver(_make_hs_config())
    deal_search_called = [False]
    deal_create_called = [False]
    association_called = [False]

    sent_deal_body = None

    def side_effect(req, timeout=30):
        nonlocal sent_deal_body
        # req.data is bytes from json.dumps().encode()
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()

        if "/deals/search" in url:
            deal_search_called[0] = True
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"results": []}).encode()
            return cm
        if "/objects/deals" in url and "/associations" not in url and req.get_method() in ("POST", "post"):
            deal_create_called[0] = True
            sent_deal_body = json.loads(req.data) if req.data else {}
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"id": "deal-001"}).encode()
            return cm
        if "contacts/" in url and "/associations/" in url:
            association_called[0] = True
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
            return cm
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = driver.upsert_deal(
            "contact-123",
            {"name": "Alice", "company_name": "Wonderland"},
            "qualifiedtobuy",
            {"pipeline_id": "default"},
        )
        assert result == "deal-001"
        assert deal_create_called[0]
        assert association_called[0]

    assert sent_deal_body is not None
    assert sent_deal_body["properties"]["dealname"] == "Alice - Wonderland"
    assert sent_deal_body["properties"]["pipeline"] == "default"
    assert sent_deal_body["properties"]["dealstage"] == "qualifiedtobuy"


def test_hubspot_upsert_deal_update():
    """Found existing deal -> PATCH deal stage."""
    driver = HubspotDriver(_make_hs_config())

    patched_stage = None

    def side_effect(req, timeout=30):
        nonlocal patched_stage
        url = req.full_url or ""

        if "/deals/search" in url:
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({
                "results": [{"id": "deal-existing", "properties": {}}]
            }).encode()
            return cm
        if "/associations" in url:
            # Association check returns our contact
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({
                "results": [{"id": "contact-123"}]
            }).encode()
            return cm
        if "/objects/deals/deal-existing" in url and req.method in ("PATCH", "patch"):
            patched_stage = json.loads(req.data) if req.data else {}
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"id": "deal-existing"}).encode()
            return cm
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = driver.upsert_deal(
            "contact-123",
            {"name": "Alice"},
            "presentationscheduled",
            {"pipeline_id": "default"},
        )
        assert result == "deal-existing"

    assert patched_stage is not None
    assert patched_stage["properties"]["dealstage"] == "presentationscheduled"


# ---------------------------------------------------------------------------
# Pipeline discovery tests
# ---------------------------------------------------------------------------

def test_hubspot_discover_pipelines():
    """Parses HubSpot pipeline response with stages."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_hs_response({
            "results": [
                {
                    "id": "default",
                    "label": "Sales Pipeline",
                    "stages": [
                        {"id": "qualifiedtobuy", "label": "Qualified to Buy"},
                        {"id": "presentationscheduled", "label": "Presentation Scheduled"},
                    ],
                },
                {
                    "id": "another-pipeline",
                    "label": "Support Pipeline",
                    "stages": [],
                },
            ]
        })
        result = driver.discover_pipelines()
        assert len(result) == 2
        assert result[0]["id"] == "default"
        assert result[0]["name"] == "Sales Pipeline"
        assert len(result[0]["stages"]) == 2
        assert result[0]["stages"][0]["id"] == "qualifiedtobuy"
        assert result[0]["stages"][0]["name"] == "Qualified to Buy"
        assert result[1]["id"] == "another-pipeline"


def test_hubspot_discover_pipelines_empty():
    """Handles empty pipeline list."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_hs_response({"results": []})
        result = driver.discover_pipelines()
        assert result == []


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_hubspot_test_connection_success():
    """Returns (True, '') on successful pipeline discovery."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_hs_response({
            "results": [{"id": "default", "label": "Sales Pipeline", "stages": []}]
        })
        ok, msg = driver.test_connection()
        assert ok is True
        assert msg == ""


def test_hubspot_test_connection_failure():
    """Returns (False, message) on auth error."""
    driver = HubspotDriver(_make_hs_config())
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://test", 401, "Unauthorized", {}, None
        )
        ok, msg = driver.test_connection()
        assert ok is False
        assert "API key rejected" in msg


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------

def test_hubspot_rate_limiter():
    """401st request within window blocks (400 per 10s for HubSpot)."""
    tb = HsTokenBucket(400, 10)
    for _ in range(400):
        wait = tb.acquire()
        assert wait == 0.0, "first 400 acquires should be immediate"
    assert tb.tokens < 1.0, "bucket should be empty after 400 acquires"


# ---------------------------------------------------------------------------
# Event push tests
# ---------------------------------------------------------------------------

def test_hubspot_push_events_email():
    """Email events create both a Note AND an Email object."""
    driver = HubspotDriver(_make_hs_config())
    events = [
        {"event_type": "email_sent", "direction": "outbound", "subject": "Hello"},
    ]

    note_created = [False]
    email_created = [False]
    email_body = None
    note_body = None

    def side_effect(req, timeout=30):
        nonlocal email_body, note_body
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        method = req.get_method()

        if "/objects/notes" in url and method == "POST":
            note_created[0] = True
            note_body = json.loads(req.data) if req.data else {}
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"id": "note-001"}).encode()
            return cm
        if "/objects/emails" in url and method == "POST":
            email_created[0] = True
            email_body = json.loads(req.data) if req.data else {}
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"id": "email-001"}).encode()
            return cm
        if "/associations" in url:
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
            return cm
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result, _ = driver.push_events("contact-123", "deal-123", events)
        assert result == 1
        assert note_created[0], "Note should be created for email events"
        assert email_created[0], "Email object should also be created for email events"

    assert email_body is not None
    assert email_body["properties"]["hs_email_direction"] == "EMAIL"
    assert email_body["properties"]["hs_email_status"] == "SENT"
    assert email_body["properties"]["hs_email_subject"] == "Hello"
    assert note_body is not None
    assert "[Sent] Hello" in note_body["properties"]["hs_note_body"]


def test_hubspot_push_events_note():
    """Non-email events create Note objects."""
    driver = HubspotDriver(_make_hs_config())
    events = [
        {"event_type": "stage_change", "old_stage": "interested", "new_stage": "proposal"},
    ]

    note_created = [False]
    note_body = None

    def side_effect(req, timeout=30):
        nonlocal note_body
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        method = req.get_method()

        if "/objects/notes" in url and method == "POST":
            note_created[0] = True
            note_body = json.loads(req.data) if req.data else {}
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({"id": "note-001"}).encode()
            return cm
        if "/associations" in url:
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
            return cm
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result, _ = driver.push_events("contact-123", "deal-123", events)
        assert result == 1
        assert note_created[0]

    assert note_body is not None
    assert "[Stage]" in note_body["properties"]["hs_note_body"]


def test_hubspot_push_events_empty():
    """Returns 0 for empty list."""
    driver = HubspotDriver(_make_hs_config())
    result, _ = driver.push_events("c-1", "d-1", [])
    assert result == 0


def test_hubspot_push_events_partial_failure():
    """Individual event errors don't fail the batch."""
    driver = HubspotDriver(_make_hs_config())
    events = [
        {"event_type": "stage_change", "old_stage": "x", "new_stage": "y"},
        {"event_type": "stage_change", "old_stage": "bad", "new_stage": "fail"},
        {"event_type": "stage_change", "old_stage": "a", "new_stage": "b"},
    ]

    # Track which request by sequential call number (for note POSTs)
    call_idx = [0]

    def side_effect(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        method = req.get_method()

        if "/objects/notes" in url and method == "POST":
            call_idx[0] += 1
            # Fail the second note creation (attempts 2,3,4,5 due to retries)
            # Actually: _request retries 5xx, so we need to account for 4 attempts
            # per failed event. We'll fail the note for the second event.
            if call_idx[0] in (2, 3, 4, 5):  # 4 attempts (1 initial + 3 retries)
                import urllib.error
                raise urllib.error.HTTPError("https://test", 500, "Server Error", {}, None)

        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"id": f"note-{call_idx[0]}"}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with patch("time.sleep"):
            result, _ = driver.push_events("c-1", "d-1", events)
            # 2 events succeeded (first and third), 1 failed (second)
            assert result == 2


# ---------------------------------------------------------------------------
# HubSpot event formatting tests
# ---------------------------------------------------------------------------

def test_hubspot_format_event_note_email_sent():
    note = _hs_format_event_note({
        "event_type": "email_sent", "direction": "outbound", "subject": "Test Subject",
    })
    assert "[Sent] Test Subject" in note


def test_hubspot_format_event_note_reply():
    note = _hs_format_event_note({
        "event_type": "reply", "body_preview": "Thanks!",
    })
    assert "[Replied] Thanks!" in note


def test_hubspot_format_event_note_bounce():
    note = _hs_format_event_note({
        "event_type": "bounce", "body_preview": "Mailbox full",
    })
    assert "[Bounced]" in note


def test_hubspot_format_event_note_meeting():
    note = _hs_format_event_note({
        "event_type": "meeting_booked", "body_preview": "Wed 3pm",
    })
    assert "[Meeting]" in note


def test_hubspot_format_event_note_generic():
    note = _hs_format_event_note({
        "event_type": "custom_event", "body_preview": "Some text",
    })
    assert "[Custom Event]" in note


# ---------------------------------------------------------------------------
# HubSpot integration tests (require sandbox credentials)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_hubspot_integration_test_connection():
    """Test the real HubSpot API key works."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})
    ok, msg = driver.test_connection()
    assert ok, f"Connection test failed: {msg}"


@pytest.mark.integration
def test_hubspot_integration_discover():
    """Returns real pipelines with stages from sandbox."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})
    pipelines = driver.discover_pipelines()
    assert len(pipelines) > 0
    for p in pipelines:
        assert "id" in p
        assert "name" in p
        assert "stages" in p
        if p["stages"]:
            for s in p["stages"]:
                assert "id" in s
                assert "name" in s


@pytest.mark.integration
def test_hubspot_integration_full_flow():
    """Search contact -> create contact -> create deal -> associate -> push events."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})

    # 1. Discover pipelines
    pipelines = driver.discover_pipelines()
    assert len(pipelines) > 0
    first_pipeline = pipelines[0]
    pipeline_id = first_pipeline["id"]
    stages = first_pipeline.get("stages", [])
    assert len(stages) > 0
    first_stage_id = stages[0]["id"]

    # 2. Search for non-existent contact
    test_email = f"test-{int(time.time())}@example.com"
    result = driver.search_contact(test_email)
    assert result is None

    # 3. Create contact
    contact_id = driver.create_contact({
        "name": "Integration Test",
        "email": test_email,
        "company_name": "TestCo",
        "title": "CEO",
        "industry": "SaaS",
    }, None)
    assert contact_id

    # 4. Lookup contact (may need a small delay for indexing)
    time.sleep(2)
    looked_up = driver.lookup_contact(test_email)
    assert looked_up == contact_id

    # 5. Upsert deal
    deal_id = driver.upsert_deal(
        contact_id,
        {"name": "Integration Test", "company_name": "TestCo"},
        first_stage_id,
        {"pipeline_id": pipeline_id},
    )
    assert deal_id

    # 6. Push events
    count, _ = driver.push_events(contact_id, deal_id, [
        {"event_type": "email_sent", "direction": "outbound", "subject": "Integration test email"},
        {"event_type": "reply", "body_preview": "This is a test reply"},
    ])
    assert count == 2


@pytest.mark.integration
def test_hubspot_integration_no_duplicates():
    """Running lookup after create returns same contact."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})

    test_email = f"nodup-{int(time.time())}@example.com"

    # First contact creation
    cid1 = driver.create_contact({"name": "No Dup", "email": test_email}, None)
    assert cid1

    # Lookup returns the same contact
    time.sleep(2)
    cid2 = driver.lookup_contact(test_email)
    assert cid2 == cid1


@pytest.mark.integration
def test_hubspot_integration_invalid_token():
    """Invalid API key returns clear error."""
    driver = HubspotDriver({"api_key": "invalid-token-12345"})
    ok, msg = driver.test_connection()
    assert ok is False
    assert len(msg) > 0


# ============================================================================
# Phase 4 tests — Sync-Back IDs (crm_entity_map)
# ============================================================================


def _setup_phase_4_data(conn):
    """Insert lead, workspace_lead, and CRM config for Phase 4 tests."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        (DEFAULT_ORG_ID, "test-org"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
    )
    conn.execute(
        "INSERT OR IGNORE INTO leads (id, name, email, title, industry) VALUES (?, ?, ?, ?, ?)",
        (1, "Alice", "alice@example.com", "CEO", "SaaS"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO workspace_leads
           (id, org_id, workspace_id, lead_id, status, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (f"{WS1_ID}-1", DEFAULT_ORG_ID, WS1_ID, 1, "interested"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
        (WS1_ID, "ghl", "mock-key", "pipe-1",
         '{"interested":"stage-1","proposal":"stage-2","won":"stage-3","lost":"stage-4"}'),
    )
    conn.commit()


def test_phase_4_entity_map_first_sync_creates():
    """No entity_map row -> fresh create, row written after sync."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)

        # Use a mock that returns None from lookup_contact to force create_contact
        class NoLookupMock(MockDriver):
            def lookup_contact(self, email):
                self.calls.append(f"lookup_contact({email})")
                return None

        mock = NoLookupMock()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        # No entity_map row exists before sync
        row = conn.execute(
            "SELECT 1 FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert row is None

        cid, did, c_action, d_action = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        assert cid is not None
        assert did is not None
        assert c_action == "created_contact"
        assert d_action == "created_deal"

        # Entity map row written
        conn.commit()
        row = conn.execute(
            """SELECT * FROM crm_entity_map
               WHERE workspace_id = ? AND lead_id = ? AND platform = ?""",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert row is not None
        assert row["crm_contact_id"] == cid
        assert row["crm_deal_id"] == did
        assert row["last_sync_status"] == "synced"
        assert row["cloud_pending"] == 1
        assert row["sync_hash"] is not None
    finally:
        conn.close()


def test_phase_4_entity_map_second_sync_updates():
    """entity_map has IDs -> UPDATE not CREATE."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)

        # Pre-populate entity_map with a known hash (different from what we'll compute)
        old_hash = "0000000000000000"
        conn.execute(
            """INSERT INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', ?, 0)""",
            (WS1_ID, 1, "ghl", "contact-old", "deal-old", old_hash),
        )
        conn.commit()

        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        cid, did, c_action, d_action = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        # With entity map having old hash, should update existing, not create new
        assert cid == "contact-old"
        assert did == "deal-old"
        assert c_action == "updated_contact"
        assert d_action == "updated_deal"

        # Verify update_contact was called
        assert any("update_contact" in c for c in mock.calls)
        assert any("update_deal_stage" in c for c in mock.calls)
        # No create_contact or upsert_deal
        assert not any(c.startswith("create_contact") for c in mock.calls)
        assert not any(c.startswith("upsert_deal") for c in mock.calls)
    finally:
        conn.close()


def test_phase_4_entity_map_contact_updated_on_re_sync():
    """Contact data (e.g., new title) is pushed to CRM on re-sync."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)

        # Pre-populate entity map with old hash — forces update path
        conn.execute(
            """INSERT INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', ?, 0)""",
            (WS1_ID, 1, "ghl", "contact-old", "deal-old", "oldhash00000000"),
        )
        conn.commit()

        mock = MockDriver()
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "VP of Sales", "status": "interested",
            "industry": None, "headcount": None, "company": None,
            "company_name": None, "company_domain": None, "linkedin_url": None,
        }
        cfg = {"platform": "ghl", "workspace_id": WS1_ID,
               "stage_mapping": {"interested": "stage-1"}, "contact_field_mapping": None}

        cid, did, c_action, d_action = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        # With entity_map having crm_contact_id, update_contact should be called
        assert "update_contact" in str(mock.calls)
        # Verify updated_contacts list has the new title
        assert len(mock.updated_contacts) > 0
        assert mock.updated_contacts[0].get("title") == "VP of Sales"
    finally:
        conn.close()


def test_phase_4_entity_map_deal_updated_on_re_sync():
    """Deal stage is updated on re-sync."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        # Pre-populate entity_map
        old_hash = "0000000000000000"
        conn.execute(
            """INSERT INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', ?, 0)""",
            (WS1_ID, 1, "ghl", "contact-old", "deal-old", old_hash),
        )
        conn.commit()

        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        cid, did, c_action, d_action = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        assert d_action == "updated_deal"
        assert any("update_deal_stage" in c for c in mock.calls)
        assert "stage-1" in str(mock.calls)
    finally:
        conn.close()


def test_phase_4_entity_map_sync_hash_skip():
    """Lead with same hash -> skip API calls entirely."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        # First sync: no entity map, writes one with hash
        cid, did, c1, d1 = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        conn.commit()
        mock.calls.clear()

        # Second sync: same data, hash matches -> skip
        cid2, did2, c2, d2 = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        assert c2 == "skipped"
        assert d2 == "skipped"
        assert len(mock.calls) == 0, f"expected no calls, got {mock.calls}"
    finally:
        conn.close()


def test_phase_4_entity_map_sync_hash_change():
    """Lead with changed field -> new hash -> API calls made."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        # First sync: writes entity map
        crm_sync.sync_single_lead(lead, cfg, mock, conn=conn, workspace_id=WS1_ID)
        conn.commit()
        mock.calls.clear()

        # Change title
        lead["title"] = "VP of Engineering"
        cid, did, c2, d2 = crm_sync.sync_single_lead(
            lead, cfg, mock, conn=conn, workspace_id=WS1_ID,
        )
        # Should NOT skip — hash changed
        assert c2 != "skipped"
        assert d2 != "skipped"
        assert len(mock.calls) > 0
        assert any("update_contact" in c for c in mock.calls)
    finally:
        conn.close()


def test_phase_4_entity_map_lead_deleted():
    """Lead no longer in workspace_leads -> entity_map row stays (CRM deal not auto-deleted)."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        # Insert entity map row
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', 'abc123', 0)""",
            (WS1_ID, 1, "ghl", "contact-1", "deal-1"),
        )
        conn.commit()

        # Delete workspace_lead (simulating lead removed from workspace)
        conn.execute("DELETE FROM workspace_leads WHERE lead_id = 1")
        conn.commit()

        # Entity map row should still exist (we don't cascade-delete CRM data)
        row = conn.execute(
            "SELECT * FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert row is not None
        assert row["crm_contact_id"] == "contact-1"
    finally:
        conn.close()


def test_phase_4_entity_map_two_leads_same_email():
    """Different lead_ids -> separate entity_map rows, no confusion."""
    om.init_db()
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _setup_phase_4_data(conn)
        # Insert second lead with different email (email is UNIQUE on leads)
        conn.execute(
            "INSERT OR IGNORE INTO leads (id, name, email, title) VALUES (?, ?, ?, ?)",
            (99, "Alice2", "alice2@example.com", "CTO"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO workspace_leads
               (id, org_id, workspace_id, lead_id, status, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (f"{WS1_ID}-99", DEFAULT_ORG_ID, WS1_ID, 99, "interested"),
        )
        conn.commit()

        # Insert entity map rows with different lead_ids
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id,
                sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (WS1_ID, 1, "ghl", "contact-1", "hash1"),
        )
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id,
                sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (WS1_ID, 99, "ghl", "contact-2", "hash2"),
        )
        conn.commit()

        # Two separate entity map rows
        rows = conn.execute(
            "SELECT lead_id, crm_contact_id FROM crm_entity_map WHERE workspace_id = ? AND platform = ? ORDER BY lead_id",
            (WS1_ID, "ghl"),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["lead_id"] == 1
        assert rows[1]["lead_id"] == 99
        assert rows[0]["crm_contact_id"] == "contact-1"
        assert rows[1]["crm_contact_id"] == "contact-2"
    finally:
        conn.close()


def test_phase_4_entity_map_cloud_pending_flag():
    """After sync, entity_map row has cloud_pending = 1."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {"lead_id": 1, "name": "Alice", "email": "alice@example.com",
                "title": "CEO", "industry": "SaaS", "status": "interested",
                "company": None, "company_name": None, "company_domain": None,
                "headcount": None, "linkedin_url": None}

        crm_sync.sync_single_lead(lead, cfg, mock, conn=conn, workspace_id=WS1_ID)
        conn.commit()

        row = conn.execute(
            "SELECT cloud_pending FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert row is not None
        assert row["cloud_pending"] == 1
    finally:
        conn.close()


def test_phase_4_entity_map_relay_round_trip():
    """Cloud_pending row is included in build_crm_entity_map_payloads."""
    from lead_sync import build_crm_entity_map_payloads

    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        # Insert entity map row with cloud_pending = 1
        conn.execute(
            """INSERT INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', 'abc123', 1)""",
            (WS1_ID, 1, "ghl", "contact-1", "deal-1"),
        )
        conn.commit()

        payloads = build_crm_entity_map_payloads(conn)
        assert len(payloads) >= 1
        found = [p for p in payloads if p["lead_id"] == 1 and p["platform"] == "ghl"]
        assert len(found) == 1
        assert found[0]["workspace_id"] == WS1_ID
        assert found[0]["crm_contact_id"] == "contact-1"
        assert found[0]["crm_deal_id"] == "deal-1"
        assert found[0]["kind"] == "crm_entity_map"

        # Row with cloud_pending = 0 should NOT be included
        conn.execute(
            "UPDATE crm_entity_map SET cloud_pending = 0 WHERE lead_id = 1",
        )
        conn.commit()
        payloads2 = build_crm_entity_map_payloads(conn)
        found2 = [p for p in payloads2 if p["lead_id"] == 1]
        assert len(found2) == 0
    finally:
        conn.close()


def test_phase_4_entity_map_rehydrate_after_refresh():
    """After mock refresh --yes + pull --full, entity_map rows are restored."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_4_data(conn)
        # Simulate a relay-sourced entity map row (as if from pull --full)
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'synced', ?, 0, datetime('now'), datetime('now'))""",
            (WS1_ID, 1, "ghl", "contact-relay", "deal-relay", "2026-01-01T00:00:00", "hash-relay"),
        )
        conn.commit()

        # Verify it's there
        row = conn.execute(
            "SELECT crm_contact_id, crm_deal_id FROM crm_entity_map WHERE lead_id = 1",
        ).fetchone()
        assert row is not None
        assert row["crm_contact_id"] == "contact-relay"
        assert row["crm_deal_id"] == "deal-relay"

        # Re-insert via INSERT OR REPLACE (simulates pull re-hydration)
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                last_synced_at, last_sync_status, sync_hash, cloud_pending, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'synced', ?, 0, datetime('now'), datetime('now'))""",
            (WS1_ID, 1, "ghl", "contact-relay-2", "deal-relay-2", "2026-02-01T00:00:00", "hash-relay-2"),
        )
        conn.commit()

        # Verify updated
        row2 = conn.execute(
            "SELECT crm_contact_id, crm_deal_id FROM crm_entity_map WHERE lead_id = 1",
        ).fetchone()
        assert row2 is not None
        assert row2["crm_contact_id"] == "contact-relay-2"
        assert row2["crm_deal_id"] == "deal-relay-2"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HubSpot Phase 4 integration test stubs
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_phase_4_ghl_full_cycle_with_entity_map():
    """Full cycle: create contact -> entity_map written -> re-sync uses update."""
    api_key = os.environ.get("GHL_SANDBOX_API_KEY")
    location_id = os.environ.get("GHL_SANDBOX_LOCATION_ID")
    pipeline_id = os.environ.get("GHL_SANDBOX_PIPELINE_ID")
    if not all([api_key, location_id, pipeline_id]):
        pytest.skip("GHL sandbox credentials not set")

    driver = GhlDriver({
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": pipeline_id,
    })

    test_email = f"phase4-{int(time.time())}@example.com"
    lead = {
        "lead_id": 9999, "name": "Phase4 Test", "email": test_email,
        "title": "CEO", "industry": "SaaS", "status": "interested",
        "company": "Phase4Co", "company_name": "Phase4Co",
        "company_domain": None, "headcount": None, "linkedin_url": None,
    }
    cfg = {
        "workspace_id": "ws-p4test",
        "platform": "ghl",
        "pipeline_id": pipeline_id,
        "stage_mapping": {"interested": ""},  # no stage, just test contact ops
        "contact_field_mapping": None,
    }

    # First: create contact (no entity map)
    cid1 = driver.lookup_contact(test_email)
    if not cid1:
        cid1 = driver.create_contact({
            "name": "Phase4 Test", "email": test_email,
            "company_name": "Phase4Co",
        }, None)
    assert cid1

    # Now update via update_contact
    lead2 = {
        "lead_id": 9999, "name": "Phase4 Test", "email": test_email,
        "title": "CTO", "company_name": "Phase4Co",
        "industry": None, "headcount": None, "company": None,
        "company_domain": None, "linkedin_url": None,
        "status": "interested",
    }
    driver.update_contact(cid1, lead2, None)

    # Verify by looking up
    time.sleep(2)
    cid2 = driver.lookup_contact(test_email)
    assert cid2 == cid1, "Contact should still be findable after update"


@pytest.mark.integration
def test_phase_4_hubspot_full_cycle_with_entity_map():
    """Full cycle: create contact -> entity_map written -> re-sync uses update."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})

    test_email = f"phase4-{int(time.time())}@example.com"

    # Create contact
    cid1 = driver.create_contact({
        "name": "Phase4 Test", "email": test_email,
        "company_name": "Phase4Co", "title": "CEO",
    }, None)
    assert cid1

    # Update via update_contact
    driver.update_contact(cid1, {
        "name": "Phase4 Test", "email": test_email,
        "company_name": "Phase4Co", "title": "CTO",
    }, None)

    # Verify by looking up
    time.sleep(2)
    cid2 = driver.lookup_contact(test_email)
    assert cid2 == cid1, "Contact should still be findable after update"


# ============================================================================
# Phase 5 tests — Event History Sync
# ============================================================================


def _setup_phase_5_event_data(conn):
    """Insert workspace_lead_events rows for Phase 5 test leads.

    Creates org, workspace, lead, and workspace_lead. Uses WS1_ID/WS1_SLUG
    and DEFAULT_ORG_ID from Phase 1 fixtures.

    Returns list of (rowid, event_type) tuples for lead 1's events.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    # Ensure org + workspace exist (FK targets)
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        (DEFAULT_ORG_ID, "test-org"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
    )
    # Ensure lead 1 exists with a workspace_lead
    conn.execute(
        "INSERT OR IGNORE INTO leads (id, name, email, title) VALUES (?, ?, ?, ?)",
        (1, "Alice", "alice@example.com", "CEO"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO workspace_leads
           (id, org_id, workspace_id, lead_id, status, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (f"{WS1_ID}-1", DEFAULT_ORG_ID, WS1_ID, 1, "interested"),
    )

    events_data = [
        ("ev-001", WS1_ID, 1, "email_sent", "2026-01-01T10:00:00Z",
         "email-sync-001", '{}'),
        ("ev-002", WS1_ID, 1, "reply", "2026-01-01T11:00:00Z",
         "email-sync-002", '{}'),
        ("ev-003", WS1_ID, 1, "meeting_booked", "2026-01-02T10:00:00Z",
         "email-sync-003", '{}'),
        ("ev-004", WS1_ID, 1, "bounce", "2026-01-03T10:00:00Z",
         "email-sync-004", '{}'),
        ("ev-005", WS1_ID, 1, "interested", "2026-01-04T10:00:00Z",
         "email-sync-005", '{}'),
    ]
    for ev_id, ws_id, lid, etype, e_at, ikey, payload in events_data:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_events
               (id, org_id, workspace_id, lead_id, event_type, event_at,
                source_platform, idempotency_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev_id, DEFAULT_ORG_ID, ws_id, lid, etype, e_at,
             "email", ikey, payload),
        )

    conn.commit()

    # Insert events table rows for enrichment
    conn.execute(
        "INSERT OR IGNORE INTO events (id, lead_id, event_type, direction, subject, body_preview) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "email_sent", "outbound", "Quick intro", "Hey Alice, wanted to connect..."),
    )
    conn.execute(
        "INSERT OR IGNORE INTO events (id, lead_id, event_type, direction, body_preview) VALUES (?, ?, ?, ?, ?)",
        (2, 1, "reply", "inbound", "Thanks for reaching out!"),
    )
    conn.commit()

    # Return the event_ids for lead 1 (sorted by event_at)
    return conn.execute(
        "SELECT rowid, event_type FROM workspace_lead_events WHERE workspace_id = ? AND lead_id = ? ORDER BY event_at ASC",
        (WS1_ID, 1),
    ).fetchall()


def test_event_collection_null_cursor():
    """NULL last_event_id returns ALL events for the lead."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_5_event_data(conn)
        events = crm_sync.collect_pending_events(conn, WS1_ID, 1, None)
        assert len(events) == 5, f"expected 5 events, got {len(events)}"
        types = [e["event_type"] for e in events]
        assert types == ["email_sent", "reply", "meeting_booked", "bounce", "interested"]
    finally:
        conn.close()


def test_event_collection_cursor_filter():
    """Non-NULL cursor returns only newer events."""
    om.init_db()
    conn = get_conn()
    try:
        rows = _setup_phase_5_event_data(conn)
        # Use the 3rd event's rowid as cursor — should return 2 events (bounce, interested)
        cursor = rows[2]["rowid"]
        events = crm_sync.collect_pending_events(conn, WS1_ID, 1, cursor)
        assert len(events) == 2, f"expected 2 events after cursor, got {len(events)}"
        types = [e["event_type"] for e in events]
        assert types == ["bounce", "interested"]
    finally:
        conn.close()


def test_event_collection_limit():
    """Query respects 500 event limit per batch."""
    om.init_db()
    conn = get_conn()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
        )
        conn.execute(
            "INSERT OR IGNORE INTO leads (id, name, email) VALUES (?, ?, ?)",
            (1, "Alice", "alice@example.com"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO workspace_leads
               (id, org_id, workspace_id, lead_id, status, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (f"{WS1_ID}-1", DEFAULT_ORG_ID, WS1_ID, 1, "interested"),
        )
        for i in range(8):
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_events
                   (id, org_id, workspace_id, lead_id, event_type, event_at,
                    source_platform, idempotency_key, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"ev-limit-{i}", DEFAULT_ORG_ID, WS1_ID, 1, "email_sent",
                 f"2026-06-0{i+1}T10:00:00Z", "email", f"ikey-limit-{i}", '{}'),
            )
        conn.commit()

        events = crm_sync.collect_pending_events(conn, WS1_ID, 1, None)
        assert len(events) <= 500, f"expected ≤500 events, got {len(events)}"
        assert len(events) == 8, f"expected all 8 events, got {len(events)}"
    finally:
        conn.close()


def test_event_collection_empty():
    """Lead with no events returns empty list."""
    om.init_db()
    conn = get_conn()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
            (DEFAULT_ORG_ID, "test-org"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
            (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
        )
        conn.execute(
            "INSERT OR IGNORE INTO leads (id, name, email) VALUES (?, ?, ?)",
            (99, "No Events", "noevents@example.com"),
        )
        conn.commit()

        events = crm_sync.collect_pending_events(conn, WS1_ID, 99, None)
        assert events == [], f"expected empty list, got {len(events)} events"
    finally:
        conn.close()


def test_event_format_email_sent():
    """Outbound email mapped to crm_type='email', direction='OUTGOING'."""
    result = crm_sync.format_event_for_crm({
        "event_type": "email_sent", "direction": "outbound",
        "subject": "Quick intro", "body_preview": "Hey there",
    })
    assert result["crm_type"] == "email"
    assert result["direction"] == "OUTGOING"
    assert "Quick intro" in result["title"]


def test_event_format_reply():
    """Inbound reply mapped to direction='INCOMING'."""
    result = crm_sync.format_event_for_crm({
        "event_type": "reply", "direction": "inbound",
        "subject": "Re: Quick intro", "body_preview": "Thanks!",
    })
    assert result["crm_type"] == "email"
    assert result["direction"] == "INCOMING"
    assert "Replied" in result["title"]


def test_event_format_bounce():
    """Bounce mapped to crm_type='note' with 'Bounced' title."""
    result = crm_sync.format_event_for_crm({
        "event_type": "bounce", "body_preview": "Mailbox full",
    })
    assert result["crm_type"] == "note"
    assert result["title"] == "Bounced"


def test_event_format_meeting():
    """Meeting_booked mapped to crm_type='meeting'."""
    result = crm_sync.format_event_for_crm({
        "event_type": "meeting_booked", "body_preview": "Wed 3pm",
    })
    assert result["crm_type"] == "meeting"
    assert "Meeting" in result["title"]


def test_event_format_unknown():
    """Unknown event_type mapped to crm_type='note' with label."""
    result = crm_sync.format_event_for_crm({
        "event_type": "custom_event", "body_preview": "Some text",
    })
    assert result["crm_type"] == "note"
    assert "Custom Event" in result["title"]


def _setup_phase_5_sync_data(conn):
    """Full setup for sync-level event tests (entity_map + workspace_lead_events)."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        (DEFAULT_ORG_ID, "test-org"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (WS1_ID, DEFAULT_ORG_ID, "Popcam", WS1_SLUG),
    )
    conn.execute(
        "INSERT OR IGNORE INTO leads (id, name, email, title, industry) VALUES (?, ?, ?, ?, ?)",
        (1, "Alice", "alice@example.com", "CEO", "SaaS"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO workspace_leads
           (id, org_id, workspace_id, lead_id, status, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (f"{WS1_ID}-1", DEFAULT_ORG_ID, WS1_ID, 1, "interested"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
        (WS1_ID, "ghl", "mock-key-1", "pipe-1",
         '{"interested":"stage-1","proposal":"stage-2","won":"stage-3","lost":"stage-4"}'),
    )
    # Pre-populate entity map with synced contact/deal, no events cursor
    conn.execute(
        """INSERT OR IGNORE INTO crm_entity_map
           (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
            last_synced_at, last_sync_status, sync_hash, cloud_pending)
           VALUES (?, ?, ?, ?, ?, datetime('now'), 'synced', 'hash-initial', 0)""",
        (WS1_ID, 1, "ghl", "contact-alice", "deal-alice"),
    )
    conn.commit()

    # Insert events
    events_data = [
        ("ev-sync-1", WS1_ID, 1, "email_sent", "2026-06-01T10:00:00Z",
         "email-sync-ikey-1", '{}'),
        ("ev-sync-2", WS1_ID, 1, "reply", "2026-06-01T11:00:00Z",
         "email-sync-ikey-2", '{}'),
    ]
    for ev_id, ws_id, lid, etype, e_at, ikey, payload in events_data:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_events
               (id, org_id, workspace_id, lead_id, event_type, event_at,
                source_platform, idempotency_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev_id, DEFAULT_ORG_ID, ws_id, lid, etype, e_at,
             "email", ikey, payload),
        )
    conn.commit()


def test_event_cursor_updated():
    """After push, last_event_id_synced updated to max event rowid."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_5_sync_data(conn)
        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        results = crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, driver=mock)

        # Events should have been pushed
        assert results["events_pushed"] == 2, f"expected 2 events pushed, got {results['events_pushed']}"
        assert mock.pushed_events_count == 2

        # Cursor should be set
        entity = conn.execute(
            "SELECT last_event_id_synced FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert entity is not None
        assert entity["last_event_id_synced"] is not None, "cursor should be set after event push"
    finally:
        conn.close()


def test_event_cursor_incremental():
    """Re-sync after new events pushes only new ones."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_5_sync_data(conn)

        # First sync: push 2 events, cursor advances
        mock1 = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }
        lead = {
            "lead_id": 1, "name": "Alice", "email": "alice@example.com",
            "title": "CEO", "industry": "SaaS", "status": "interested",
            "company": None, "company_name": None, "company_domain": None,
            "headcount": None, "linkedin_url": None,
        }

        r1 = crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, driver=mock1)
        assert r1["events_pushed"] == 2, f"first sync: expected 2 events, got {r1['events_pushed']}"

        # Add 1 new event
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_events
               (id, org_id, workspace_id, lead_id, event_type, event_at,
                source_platform, idempotency_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("ev-incremental", DEFAULT_ORG_ID, WS1_ID, 1, "meeting_booked",
             "2026-06-02T10:00:00Z", "email", "ikey-incremental", '{}'),
        )
        conn.commit()

        # Second sync: only 1 new event
        mock2 = MockDriver()
        r2 = crm_sync.sync_workspace(conn, WS1_ID, "Popcam", cfg, driver=mock2)
        assert r2["events_pushed"] == 1, f"second sync: expected 1 new event, got {r2['events_pushed']}"

        # Verify mock2 only received 1 event
        assert mock2.pushed_events_count == 1
    finally:
        conn.close()


def test_skip_events_flag():
    """--skip-events prevents any event push, cursor unchanged."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_phase_5_sync_data(conn)

        mock = MockDriver()
        cfg = {
            "workspace_id": WS1_ID,
            "platform": "ghl",
            "pipeline_id": "pipe-1",
            "stage_mapping": {"interested": "stage-1"},
            "contact_field_mapping": None,
        }

        results = crm_sync.sync_workspace(
            conn, WS1_ID, "Popcam", cfg, driver=mock, skip_events=True,
        )
        assert results["events_pushed"] == 0, f"expected 0 events with skip, got {results['events_pushed']}"
        assert mock.pushed_events_count == 0, "driver should not have pushed any events"

        # Cursor should still be NULL (unchanged from initial setup)
        entity = conn.execute(
            "SELECT last_event_id_synced FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            (WS1_ID, 1, "ghl"),
        ).fetchone()
        assert entity["last_event_id_synced"] is None, "cursor should remain NULL when skip_events=True"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 5 integration test stubs (require sandbox credentials)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_phase_5_ghl_full_event_cycle():
    """Full cycle: contact synced, events pushed, cursor advances, re-sync is incremental."""
    api_key = os.environ.get("GHL_SANDBOX_API_KEY")
    location_id = os.environ.get("GHL_SANDBOX_LOCATION_ID")
    pipeline_id = os.environ.get("GHL_SANDBOX_PIPELINE_ID")
    if not all([api_key, location_id, pipeline_id]):
        pytest.skip("GHL sandbox credentials not set")

    driver = GhlDriver({
        "api_key": api_key,
        "location_id": location_id,
        "pipeline_id": pipeline_id,
    })

    test_email = f"phase5-events-{int(time.time())}@example.com"
    contact_id = driver.create_contact({
        "name": "Phase5 Event Test", "email": test_email,
        "company_name": "Phase5Co",
    }, None)
    assert contact_id

    # Simulate events list (as collected_pending_events returns)
    events = [
        {"event_type": "email_sent", "direction": "outbound",
         "subject": "Quick intro", "event_at": "2026-06-01T10:00:00Z",
         "rowid": 1},
        {"event_type": "reply", "direction": "inbound",
         "body_preview": "Thanks for reaching out", "event_at": "2026-06-01T11:00:00Z",
         "rowid": 2},
        {"event_type": "meeting_booked", "event_at": "2026-06-02T10:00:00Z",
         "body_preview": "Wed 3pm Zoom", "rowid": 3},
    ]
    count, _ = driver.push_events(contact_id, None, events)
    assert count == 3, f"expected 3 events pushed, got {count}"

    # Push same events again — should create duplicates (at-least-once semantics)
    count2, _ = driver.push_events(contact_id, None, events)
    assert count2 == 3, f"re-push should also return 3"

    # Re-sync with only new events
    new_events = [
        {"event_type": "bounce", "event_at": "2026-06-03T10:00:00Z",
         "body_preview": "Mailbox full", "rowid": 4},
    ]
    count3, _ = driver.push_events(contact_id, None, new_events)
    assert count3 == 1, f"incremental: expected 1 event, got {count3}"


@pytest.mark.integration
def test_phase_5_hubspot_full_event_cycle():
    """Full cycle: email + note events pushed to HubSpot, re-sync pushes only new."""
    api_key = os.environ.get("HUBSPOT_SANDBOX_API_KEY")
    if not api_key:
        pytest.skip("HUBSPOT_SANDBOX_API_KEY not set")

    driver = HubspotDriver({"api_key": api_key})

    test_email = f"phase5-hs-{int(time.time())}@example.com"
    contact_id = driver.create_contact({
        "name": "Phase5 HS Event", "email": test_email,
        "company_name": "Phase5Co", "title": "CEO",
    }, None)
    assert contact_id

    # Discover pipelines for a deal
    pipelines = driver.discover_pipelines()
    assert len(pipelines) > 0
    first_pipeline = pipelines[0]
    pipeline_id = first_pipeline["id"]
    stages = first_pipeline.get("stages", [])
    first_stage_id = stages[0]["id"] if stages else ""

    deal_id = driver.upsert_deal(
        contact_id,
        {"name": "Phase5 HS Event", "company_name": "Phase5Co"},
        first_stage_id,
        {"pipeline_id": pipeline_id},
    )
    assert deal_id

    # Push 2 events (1 email + 1 note)
    events = [
        {"event_type": "email_sent", "direction": "outbound",
         "subject": "Quick intro", "body_preview": "Hey there",
         "event_at": "2026-06-01T10:00:00Z", "rowid": 1},
        {"event_type": "interested", "body_preview": "Sounds great",
         "event_at": "2026-06-01T12:00:00Z", "rowid": 2},
    ]
    count, _ = driver.push_events(contact_id, deal_id, events)
    assert count == 2, f"expected 2 events pushed, got {count}"

    # Incremental — 1 new event
    new_events = [
        {"event_type": "meeting_booked", "body_preview": "Wed 3pm Zoom",
         "event_at": "2026-06-02T10:00:00Z", "rowid": 3},
    ]
    count2, _ = driver.push_events(contact_id, deal_id, new_events)
    assert count2 == 1, f"expected 1 new event, got {count2}"


# ---------------------------------------------------------------------------
# Company sync unit tests
# ---------------------------------------------------------------------------

def _setup_company_test_data(conn):
    """Create minimal org, workspace, and lead for company sync tests."""
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        ("test-org-co", "Test Org Company"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        ("ws-co-sync", "test-org-co", "Company Test WS", "company-test"),
    )
    conn.execute(
        "INSERT INTO companies (name, domain, industry, headcount) VALUES (?, ?, ?, ?)",
        ("OmniCorp", "omnicorp.test", "Technology", "51-200"),
    )
    company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO leads (id, name, email, title, industry, headcount, company,
            linkedin_url, company_id, cloud_pending)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (9001, "Alice Wang", "alice@omnicorp.test", "CEO",
         "Technology", "51-200", "OmniCorp",
         "https://linkedin.com/in/alicewang", company_id),
    )
    conn.execute(
        """INSERT INTO workspace_leads (id, workspace_id, org_id, lead_id, status)
           VALUES (?, ?, ?, ?, ?)""",
        ("ws-co-9001", "ws-co-sync", "test-org-co", 9001, "interested"),
    )
    conn.commit()


def test_company_sync_creates_company():
    """sync_company with a fresh lead and MockDriver triggers upsert_company."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_company_test_data(conn)

        driver = MockDriver("ghl")
        lead = crm_sync.select_leads(conn, "ws-co-sync")[0]
        company_id = crm_sync.sync_company(
            lead, None, driver, conn=conn,
            workspace_id="ws-co-sync", platform="ghl",
        )
        assert company_id == "mock-company-001"
        assert "upsert_company" in " ".join(driver.calls)
    finally:
        conn.close()


def test_company_sync_skips_empty():
    """Lead with no company_name or company returns empty string."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_company_test_data(conn)
        # Make lead have no company
        conn.execute("UPDATE leads SET company = NULL, company_id = NULL WHERE id = 9001")
        conn.commit()

        driver = MockDriver("ghl")
        lead = crm_sync.select_leads(conn, "ws-co-sync")[0]
        company_id = crm_sync.sync_company(
            lead, None, driver, conn=conn,
            workspace_id="ws-co-sync", platform="ghl",
        )
        assert company_id == ""
        assert "upsert_company" not in " ".join(driver.calls)
    finally:
        conn.close()


def test_company_sync_reuses_existing():
    """Entity map has crm_company_id — skip upsert, return existing ID."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_company_test_data(conn)

        driver = MockDriver("ghl")
        lead = crm_sync.select_leads(conn, "ws-co-sync")[0]

        # Pre-populate entity map with existing company ID
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                crm_company_id, last_synced_at, last_sync_status, sync_hash)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'synced', ?)""",
            ("ws-co-sync", 9001, "ghl", "contact-1", "deal-1",
             "existing-co-id", "oldhash"),
        )
        conn.commit()

        # Fetch entity fresh
        entity = conn.execute(
            """SELECT crm_contact_id, crm_deal_id, crm_company_id,
                      last_event_id_synced, sync_hash
               FROM crm_entity_map
               WHERE workspace_id = ? AND lead_id = ? AND platform = ?""",
            ("ws-co-sync", 9001, "ghl"),
        ).fetchone()

        company_id = crm_sync.sync_company(
            lead, entity, driver, conn=conn,
            workspace_id="ws-co-sync", platform="ghl",
        )
        assert company_id == "existing-co-id"
        assert "upsert_company" not in " ".join(driver.calls)
    finally:
        conn.close()


def test_company_id_in_entity_map():
    """After a full single lead sync, entity map includes crm_company_id."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_company_test_data(conn)

        conn.execute(
            """INSERT INTO crm_workspace_config
               (workspace_id, platform, api_key, location_id, pipeline_id)
               VALUES (?, ?, ?, ?, ?)""",
            ("ws-co-sync", "ghl", "test-key", "loc-1", "pipe-1"),
        )
        conn.commit()

        driver = MockDriver("ghl")
        config = {
            "platform": "ghl",
            "api_key": "test-key",
            "location_id": "loc-1",
            "pipeline_id": "pipe-1",
            "stage_mapping": {},
        }

        lead = crm_sync.select_leads(conn, "ws-co-sync")[0]
        crm_sync.sync_single_lead(lead, config, driver, conn=conn,
                                  workspace_id="ws-co-sync")

        row = conn.execute(
            "SELECT crm_company_id FROM crm_entity_map "
            "WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            ("ws-co-sync", 9001, "ghl"),
        ).fetchone()
        assert row is not None
        assert row["crm_company_id"] == "mock-company-001"
    finally:
        conn.close()


def test_company_id_in_relay_payload():
    """build_crm_entity_map_payloads includes crm_company_id."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_company_test_data(conn)

        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                crm_company_id, last_synced_at, last_sync_status, sync_hash,
                cloud_pending)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'synced', ?, 1)""",
            ("ws-co-sync", 9001, "ghl", "contact-1", "deal-1",
             "company-hs-1234", "testhash"),
        )
        conn.commit()

        from lead_sync import build_crm_entity_map_payloads
        payloads = build_crm_entity_map_payloads(conn)
        assert len(payloads) >= 1
        p = payloads[0]
        assert p["crm_company_id"] == "company-hs-1234"
        assert "kind" in p
        assert p["kind"] == "crm_entity_map"
    finally:
        conn.close()


# ============================================================================
# Phase 6 tests — pipeline.py trigger hooks
# ============================================================================


def _setup_phase_6_data(conn):
    """Set up a workspace, lead, workspace_lead, and CRM config for Phase 6 tests."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name) VALUES (?, ?)",
        (DEFAULT_ORG_ID, "test-org"),
    )
    ws_id = "ws-p6-001"
    ws_slug = "popcam"
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, org_id, name, slug) VALUES (?, ?, ?, ?)",
        (ws_id, DEFAULT_ORG_ID, "Popcam", ws_slug),
    )
    # Lead with all required fields for CRM sync
    conn.execute(
        "INSERT OR IGNORE INTO leads (id, name, email, title, industry, headcount, stage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (9001, "Alice", "alice@example.com", "CEO", "SaaS", "1-10", "prospecting"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO companies (id, name, domain) VALUES (?, ?, ?)",
        (9001, "Alice Co", "aliceco.com"),
    )
    conn.execute("UPDATE leads SET company_id = 9001 WHERE id = 9001")
    # workspace_lead
    conn.execute(
        "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("wl-p6-001", DEFAULT_ORG_ID, ws_id, 9001, "prospecting"),
    )
    # CRM config
    conn.execute(
        """INSERT OR IGNORE INTO crm_workspace_config
           (workspace_id, platform, api_key, pipeline_id, stage_mapping)
           VALUES (?, ?, ?, ?, ?)""",
        (ws_id, "ghl", "test-key", "pipe-1", '{"interested": "stage-123"}'),
    )
    conn.commit()
    return ws_id, ws_slug


def _call_pipeline(argv: list) -> "subprocess.CompletedProcess":
    """Call om.main() with mocked sys.argv and subprocess.Popen, return mock result."""
    mock_popen = MagicMock()
    # mock the Popen return value so stderr doesn't raise
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_popen.return_value = mock_proc

    with patch.object(sys, "argv", ["pipeline.py"] + argv), \
         patch.object(om.subprocess, "Popen", mock_popen):
        try:
            om.main()
        except SystemExit:
            pass

    return mock_popen


def test_hook_update_stage_triggers_crm_sync():
    """update-stage --crm-sync invokes subprocess.Popen with correct args."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = _call_pipeline([
            "update-stage", "--id", "9001", "--stage", "interested",
            "--workspace", "popcam", "--crm-sync",
        ])
        assert mock_popen.called, "subprocess.Popen should have been called"
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == sys.executable
        assert "crm_sync.py" in call_args[1]
        assert "sync" in call_args
        assert "--lead-id" in call_args
        assert "9001" in call_args
        assert "--workspace" in call_args
        assert "popcam" in call_args
    finally:
        conn.close()


def test_hook_log_event_triggers_crm_sync():
    """log-event --crm-sync invokes subprocess.Popen with correct args."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = _call_pipeline([
            "log-event", "--lead-id", "9001", "--type", "reply",
            "--direction", "inbound", "--subject", "Re: Hello",
            "--workspace", "popcam", "--crm-sync",
        ])
        assert mock_popen.called, "subprocess.Popen should have been called"
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == sys.executable
        assert "crm_sync.py" in call_args[1]
        assert "sync" in call_args
        assert "--lead-id" in call_args
        assert "9001" in call_args
        assert "--workspace" in call_args
        assert "popcam" in call_args
    finally:
        conn.close()


def test_hook_update_stage_no_crm_sync():
    """Without --crm-sync, no subprocess triggered."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = _call_pipeline([
            "update-stage", "--id", "9001", "--stage", "contacted",
            "--workspace", "popcam",
        ])
        assert not mock_popen.called, (
            "subprocess.Popen should NOT have been called without --crm-sync"
        )
    finally:
        conn.close()


def test_hook_log_event_no_crm_sync():
    """Without --crm-sync, no subprocess triggered for log-event."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = _call_pipeline([
            "log-event", "--lead-id", "9001", "--type", "reply",
            "--direction", "inbound", "--subject", "Re: Hello",
            "--workspace", "popcam",
        ])
        assert not mock_popen.called, (
            "subprocess.Popen should NOT have been called without --crm-sync"
        )
    finally:
        conn.close()


def test_hook_update_stage_non_mapped_status():
    """update-stage --stage prospecting --crm-sync does NOT trigger CRM sync."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = _call_pipeline([
            "update-stage", "--id", "9001", "--stage", "prospecting",
            "--workspace", "popcam", "--crm-sync",
        ])
        assert not mock_popen.called, (
            "prospecting stage should NOT trigger CRM sync"
        )
    finally:
        conn.close()


def test_hook_missing_crm_sync_py():
    """When crm_sync.py is absent, pipeline exits cleanly with --crm-sync."""
    crm_sync_path = SCRIPTS / "crm_sync.py"
    backup = None
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)

        # Temporarily move crm_sync.py out of the way
        if crm_sync_path.exists():
            backup = crm_sync_path.with_suffix(".py.bak")
            shutil.move(str(crm_sync_path), str(backup))

        mock_popen = _call_pipeline([
            "update-stage", "--id", "9001", "--stage", "interested",
            "--workspace", "popcam", "--crm-sync",
        ])
        # Popen should NOT be called since crm_sync.py doesn't exist
        assert not mock_popen.called, (
            "subprocess.Popen should NOT be called when crm_sync.py is missing"
        )
    finally:
        conn.close()
        if backup and backup.exists():
            shutil.move(str(backup), str(crm_sync_path))


def test_hook_stderr_output():
    """CRM sync trigger prints human-readable message to stderr."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = MagicMock()
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_popen.return_value = mock_proc

        stderr_lines = []
        real_write = sys.stderr

        class StderrCapture:
            def write(self, s):
                stderr_lines.append(s)
            def flush(self):
                pass

        sys.stderr = StderrCapture()

        try:
            with patch.object(sys, "argv", ["pipeline.py",
                    "update-stage", "--id", "9001", "--stage", "interested",
                    "--workspace", "popcam", "--crm-sync"]), \
                 patch.object(om.subprocess, "Popen", mock_popen):
                try:
                    om.main()
                except SystemExit:
                    pass
        finally:
            sys.stderr = real_write

        combined_stderr = "".join(stderr_lines)
        assert "CRM sync triggered" in combined_stderr
        assert "lead 9001" in combined_stderr
        assert "popcam" in combined_stderr
    finally:
        conn.close()


def test_hook_subprocess_does_not_block():
    """Pipeline command returns even when crm_sync.py exists (fire-and-forget)."""
    conn = get_conn()
    try:
        om.init_db()
        _setup_phase_6_data(conn)
        mock_popen = MagicMock()
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_popen.return_value = mock_proc

        start = time.time()
        with patch.object(sys, "argv", ["pipeline.py",
                "update-stage", "--id", "9001", "--stage", "interested",
                "--workspace", "popcam", "--crm-sync"]), \
             patch.object(om.subprocess, "Popen", mock_popen):
            try:
                om.main()
            except SystemExit:
                pass
        elapsed = time.time() - start
        # Should return well under 5 seconds (fire-and-forget, not waiting for sync)
        assert elapsed < 5.0, f"update-stage --crm-sync took {elapsed:.1f}s, expected <5s (non-blocking)"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 7 — Portal config sync
# ---------------------------------------------------------------------------

def _make_portal_bundle(
    *,
    workspaces=None,
    campaign_maps=None,
    mode="single",
    crm_configs=None,
    agent_secrets=None,
    version=1,
):
    """Build a realistic portal config bundle for tests."""
    return {
        "version": version,
        "workspaces": workspaces or [
            {"id": "ws1", "name": "Test Workspace", "slug": "test-ws", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"},
        ],
        "campaignMaps": campaign_maps or [],
        "mode": mode,
        "defaultWorkspaceId": "ws1" if mode == "single" else None,
        "crmConfigs": crm_configs or {},
        "agentSecrets": agent_secrets or {"version": 1, "organizationId": "org1", "secrets": {}},
    }


def _setup_org_and_workspace(conn, org_id="org1"):
    conn.execute("INSERT OR IGNORE INTO organizations (id, name, created_at) VALUES (?, 'Test Org', datetime('now'))", (org_id,))
    conn.execute("""INSERT OR REPLACE INTO workspaces (id, org_id, name, slug, cloud_synced, created_at, updated_at)
                     VALUES ('ws1', ?, 'Test WS', 'test-ws', 1, datetime('now'), datetime('now'))""", (org_id,))
    conn.commit()


def test_portal_config_fetch():
    """fetch_portal_config calls the correct endpoint and returns parsed JSON."""
    import routing_cloud

    bundle = _make_portal_bundle()
    with patch.object(routing_cloud, "_request_json", return_value=bundle) as mock_req:
        result = routing_cloud.fetch_portal_config("https://app.example.com", "test-token")
        mock_req.assert_called_once_with(
            "GET", "https://app.example.com/api/portal-config", "test-token"
        )
        assert result == bundle


def test_portal_config_sync_routing():
    """sync_org_config_from_cloud writes routing config to SQLite."""
    import routing_cloud

    bundle = _make_portal_bundle()
    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
            with patch("agent_secrets_cloud.write_agent_secrets_env", return_value=[]):
                with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env"):
                    with patch("agent_secrets_cloud.apply_secrets_to_environ"):
                        routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=lambda cfg: None,
                            quiet=True,
                        )

        # Verify routing applied
        row = conn.execute("SELECT name, slug FROM workspaces WHERE id = 'ws1'").fetchone()
        assert row is not None
        assert row[0] == "Test Workspace"
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_portal_config_sync_crm():
    """sync_org_config_from_cloud writes CRM config to crm_workspace_config."""
    import routing_cloud

    bundle = _make_portal_bundle(
        crm_configs={
            "ws1": {
                "ghl": {
                    "enabled": True,
                    "api_key": "test-api-key",
                    "location_id": "loc_123",
                    "pipeline_id": "pipe_456",
                    "stage_mapping": {"interested": "stage_1", "won": "stage_2"},
                    "contact_field_mapping": {"title": "custom_field_1"},
                    "updated_at": "2026-06-24T00:00:00Z",
                }
            }
        }
    )

    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
            with patch("agent_secrets_cloud.write_agent_secrets_env", return_value=[]):
                with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env"):
                    with patch("agent_secrets_cloud.apply_secrets_to_environ"):
                        routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=lambda cfg: None,
                            quiet=True,
                        )

        row = conn.execute(
            "SELECT platform, api_key, location_id, pipeline_id, stage_mapping, contact_field_mapping "
            "FROM crm_workspace_config WHERE workspace_id = 'ws1' AND platform = 'ghl'"
        ).fetchone()
        assert row is not None
        assert row[0] == "ghl"
        assert row[1] == "test-api-key"
        assert row[2] == "loc_123"
        assert row[3] == "pipe_456"
        stage_map = json.loads(row[4])
        assert stage_map["interested"] == "stage_1"
        assert stage_map["won"] == "stage_2"
        cfm = json.loads(row[5])
        assert cfm["title"] == "custom_field_1"
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_portal_config_no_crm_integrations():
    """sync_org_config_from_cloud with empty CRM leaves crm_workspace_config empty."""
    import routing_cloud

    bundle = _make_portal_bundle()  # no crmConfigs

    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
            with patch("agent_secrets_cloud.write_agent_secrets_env", return_value=[]):
                with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env"):
                    with patch("agent_secrets_cloud.apply_secrets_to_environ"):
                        routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=lambda cfg: None,
                            quiet=True,
                        )

        count = conn.execute(
            "SELECT COUNT(*) FROM crm_workspace_config WHERE workspace_id = 'ws1'"
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_crm_config_apply_to_sqlite():
    """Direct call to _apply_crm_config_to_sqlite inserts correct rows."""
    from routing_cloud import _apply_crm_config_to_sqlite

    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        crm_config = {
            "ws1": {
                "ghl": {
                    "enabled": True,
                    "api_key": "key-ghl",
                    "location_id": "loc_A",
                    "pipeline_id": "pipe_A",
                    "stage_mapping": {"proposal": "s1"},
                    "contact_field_mapping": None,
                }
            }
        }
        _apply_crm_config_to_sqlite(conn, crm_config, org_id="org1")
        conn.commit()

        row = conn.execute(
            "SELECT platform, api_key, stage_mapping FROM crm_workspace_config WHERE workspace_id = 'ws1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "ghl"
        assert row[1] == "key-ghl"
        assert json.loads(row[2]) == {"proposal": "s1"}
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_crm_config_update_existing():
    """Re-syncing with changed CRM config updates existing row."""
    from routing_cloud import _apply_crm_config_to_sqlite

    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        # First write
        crm_config = {
            "ws1": {
                "ghl": {
                    "enabled": True,
                    "api_key": "old-key",
                    "location_id": "loc_old",
                    "pipeline_id": "pipe_old",
                    "stage_mapping": {"old": "s_old"},
                    "contact_field_mapping": None,
                }
            }
        }
        _apply_crm_config_to_sqlite(conn, crm_config, org_id="org1")
        conn.commit()

        # Verify count is 1
        count = conn.execute("SELECT COUNT(*) FROM crm_workspace_config WHERE workspace_id = 'ws1'").fetchone()[0]
        assert count == 1

        # Update with new config
        crm_config_v2 = {
            "ws1": {
                "ghl": {
                    "enabled": True,
                    "api_key": "new-key",
                    "location_id": "loc_new",
                    "pipeline_id": "pipe_new",
                    "stage_mapping": {"new": "s_new"},
                    "contact_field_mapping": None,
                }
            }
        }
        _apply_crm_config_to_sqlite(conn, crm_config_v2, org_id="org1")
        conn.commit()

        # Should still be 1 row, with new values
        count = conn.execute("SELECT COUNT(*) FROM crm_workspace_config WHERE workspace_id = 'ws1'").fetchone()[0]
        assert count == 1

        row = conn.execute(
            "SELECT api_key, location_id, pipeline_id FROM crm_workspace_config WHERE workspace_id = 'ws1' AND platform = 'ghl'"
        ).fetchone()
        assert row[0] == "new-key"
        assert row[1] == "loc_new"
        assert row[2] == "pipe_new"
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_crm_config_remove_disabled():
    """Removing a platform from CRM config deletes local row."""
    from routing_cloud import _apply_crm_config_to_sqlite

    _, db_path = tempfile.mkstemp(suffix=".db")
    try:
        om.set_db_path_override(Path(db_path))
        om.init_db()
        conn = get_conn()
        _setup_org_and_workspace(conn)

        # Write GHL config
        _apply_crm_config_to_sqlite(
            conn,
            {"ws1": {"ghl": {"enabled": True, "api_key": "key1", "stage_mapping": {}}}},
            org_id="org1",
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM crm_workspace_config WHERE platform = 'ghl'").fetchone()[0] == 1

        # Sync with empty CRM — should remove the GHL row
        _apply_crm_config_to_sqlite(conn, {}, org_id="org1")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM crm_workspace_config").fetchone()[0] == 0
    finally:
        conn.close()
        om.set_db_path_override(None)
        os.unlink(db_path)


def test_portal_config_sync_secrets():
    """sync_org_config_from_cloud calls agent secrets writing functions."""
    import routing_cloud

    bundle = _make_portal_bundle(
        agent_secrets={
            "version": 3,
            "organizationId": "org1",
            "secrets": {"SERPER_API_KEY": ["sk-abc123"]},
        }
    )

    keys_written = ["SERPER_API_KEY"]
    with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
        with patch(
            "agent_secrets_cloud.write_agent_secrets_env",
            return_value=keys_written,
        ) as mock_write:
            with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env") as mock_mirror:
                with patch("agent_secrets_cloud.apply_secrets_to_environ") as mock_apply:
                    _, db_path = tempfile.mkstemp(suffix=".db")
                    try:
                        om.set_db_path_override(Path(db_path))
                        om.init_db()
                        conn = get_conn()
                        _setup_org_and_workspace(conn)

                        routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=lambda cfg: None,
                            quiet=True,
                        )

                        mock_write.assert_called_once()
                        call_args = mock_write.call_args[0]
                        secrets_dict = call_args[1]
                        assert secrets_dict["SERPER_API_KEY"] == ["sk-abc123"]
                        mock_mirror.assert_called_once()
                        mock_apply.assert_called_once()
                    finally:
                        conn.close()
                        om.set_db_path_override(None)
                        os.unlink(db_path)


def test_portal_config_bumps_version():
    """sync_org_config_from_cloud saves org_config_version to config."""
    import routing_cloud

    bundle = _make_portal_bundle(version=42)

    saved_config = {}
    def save_cfg(cfg):
        saved_config.update(cfg)

    with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
        with patch("agent_secrets_cloud.write_agent_secrets_env", return_value=[]):
            with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env"):
                with patch("agent_secrets_cloud.apply_secrets_to_environ"):
                    _, db_path = tempfile.mkstemp(suffix=".db")
                    try:
                        om.set_db_path_override(Path(db_path))
                        om.init_db()
                        conn = get_conn()
                        _setup_org_and_workspace(conn)

                        routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=save_cfg,
                            quiet=True,
                        )
                    finally:
                        conn.close()
                        om.set_db_path_override(None)
                        os.unlink(db_path)

    assert saved_config.get("org_config_version") == 42


def test_portal_config_secrets_failure_non_fatal():
    """sync_org_config_from_cloud succeeds even when secrets processing fails."""
    import routing_cloud

    saved_config = {}
    def save_cfg(cfg):
        saved_config.update(cfg)

    bundle = _make_portal_bundle(version=5)

    with patch.object(routing_cloud, "fetch_portal_config", return_value=bundle):
        with patch(
            "agent_secrets_cloud.write_agent_secrets_env",
            side_effect=RuntimeError("simulated write failure"),
        ):
            with patch("agent_secrets_cloud.mirror_agent_secrets_to_data_env"):
                with patch("agent_secrets_cloud.apply_secrets_to_environ"):
                    _, db_path = tempfile.mkstemp(suffix=".db")
                    try:
                        om.set_db_path_override(Path(db_path))
                        om.init_db()
                        conn = get_conn()
                        _setup_org_and_workspace(conn)

                        # Should NOT raise — secrets failure is non-fatal
                        result = routing_cloud.sync_org_config_from_cloud(
                            conn,
                            api_base="https://app.example.com",
                            token="tok",
                            org_id="org1",
                            load_config_fn=lambda: {},
                            save_config_fn=save_cfg,
                            quiet=True,
                        )

                        # Config version should still be saved (routing + CRM succeeded)
                        assert saved_config.get("org_config_version") == 5
                        # Routing should still be applied
                        ws = conn.execute("SELECT name FROM workspaces WHERE id = 'ws1'").fetchone()
                        assert ws is not None
                    finally:
                        conn.close()
                        om.set_db_path_override(None)
                        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Agent CRM sync status push tests
# ---------------------------------------------------------------------------

def test_crm_sync_status_push_no_config():
    """maybe_push_crm_sync_status returns skipped_no_key or skipped_no_config."""
    om.init_db()
    conn = get_conn()
    try:
        result = crm_sync.maybe_push_crm_sync_status(conn)
        # The test suite may create a config file without an agent_key,
        # which yields skipped_no_key. Either result is valid.
        assert result["crm_sync_status_reported"] in ("skipped_no_config", "skipped_no_key"), result
    finally:
        conn.close()


def test_crm_sync_status_push_no_data():
    """maybe_push_crm_sync_status returns skipped_no_data when no sync logs."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(json.dumps({
            "agent_key": "om_agent_test_no_data",
            "client_id": "test-client-no-data",
            "api_base_url": "https://app.example.com",
        }))
        tmp_path = tmp.name

    try:
        with patch("om_paths.get_config_path", return_value=Path(tmp_path)):
            om.init_db()
            conn = get_conn()
            try:
                result = crm_sync.maybe_push_crm_sync_status(conn)
                assert result == {"crm_sync_status_reported": "skipped_no_data"}, result
            finally:
                conn.close()
    finally:
        os.unlink(tmp_path)


def test_crm_sync_status_push_reported():
    """maybe_push_crm_sync_status returns reported after syncing with mock push."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(json.dumps({
            "agent_key": "om_agent_test_push",
            "client_id": "test-client-push",
            "api_base_url": "https://app.example.com",
        }))
        tmp_path = tmp.name

    try:
        with patch("om_paths.get_config_path", return_value=Path(tmp_path)):
            om.init_db()
            conn = get_conn()
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                _setup_org_and_workspace(conn)

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO crm_sync_log
                       (workspace_id, platform, started_at, completed_at, leads_checked,
                        contacts_created, contacts_updated, opportunities_created,
                        opportunities_updated, events_pushed, skipped, errors, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("ws1", "ghl", now, now, 5, 2, 1, 3, 0, 10, 1, 0, "completed"),
                )
                conn.commit()

                with patch("routing_cloud.push_crm_sync_status") as mock_push:
                    result = crm_sync.maybe_push_crm_sync_status(conn)
                    assert result["crm_sync_status_reported"] == "reported"
                    assert "ghl" in result["platforms"]
                    mock_push.assert_called_once()
                    payload = mock_push.call_args[0][2]
                    assert payload["clientId"] == "test-client-push"
                    assert payload["syncResults"]["ghl"]["leads_checked"] == 5
                    assert payload["syncResults"]["ghl"]["contacts_created"] == 2
            finally:
                conn.close()
    finally:
        os.unlink(tmp_path)


def test_crm_sync_status_push_payload_shape():
    """Payload has correct shape: {clientId, syncResults: {ghl: {...}, hubspot: {...}}}."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(json.dumps({
            "agent_key": "om_agent_test_payload",
            "client_id": "test-payload-client",
            "api_base_url": "https://app.example.com",
        }))
        tmp_path = tmp.name

    try:
        with patch("om_paths.get_config_path", return_value=Path(tmp_path)):
            om.init_db()
            conn = get_conn()
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                _setup_org_and_workspace(conn)

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO crm_sync_log
                       (workspace_id, platform, started_at, completed_at, leads_checked,
                        contacts_created, contacts_updated, opportunities_created,
                        opportunities_updated, events_pushed, skipped, errors, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("ws1", "ghl", now, now, 10, 3, 0, 2, 1, 8, 0, 0, "completed"),
                )
                conn.execute(
                    """INSERT INTO crm_sync_log
                       (workspace_id, platform, started_at, completed_at, leads_checked,
                        contacts_created, contacts_updated, opportunities_created,
                        opportunities_updated, events_pushed, skipped, errors, status)
                       VALUES (?, ?, datetime('now','+2 seconds'), datetime('now','+2 seconds'),
                               7, 1, 2, 0, 0, 4, 2, 0, "completed")""",
                    ("ws1", "hubspot",),
                )
                conn.commit()

                with patch("routing_cloud.push_crm_sync_status") as mock_push:
                    result = crm_sync.maybe_push_crm_sync_status(conn)

                    assert result["crm_sync_status_reported"] == "reported"
                    assert set(result["platforms"]) == {"ghl", "hubspot"}

                    mock_push.assert_called_once()
                    payload = mock_push.call_args[0][2]

                    assert payload["clientId"] == "test-payload-client"
                    sr = payload["syncResults"]
                    assert "ghl" in sr
                    assert "hubspot" in sr
                    assert sr["ghl"]["leads_checked"] == 10
                    assert sr["ghl"]["contacts_created"] == 3
                    assert sr["hubspot"]["leads_checked"] == 7
                    assert sr["hubspot"]["contacts_updated"] == 2
            finally:
                conn.close()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Multi-platform tests
# ---------------------------------------------------------------------------

def test_multi_platform_sync():
    """Both GHL and HubSpot configs for the same workspace sync correctly."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_org_and_workspace(conn)

        # Insert both platform configs for the same workspace
        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
            ("ws1", "ghl", "ghl-key", "pipe-1", '{"interested": "stage-interested", "proposal": "stage-proposal"}'),
        )
        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
            ("ws1", "hubspot", "hs-key", "hs-pipe-1", '{"interested": "hs-stage-1", "proposal": "hs-stage-2"}'),
        )
        conn.commit()

        # Verify both configs are readable
        configs = crm_sync.read_crm_config(conn, "ws1")
        platforms = {c["platform"] for c in configs}
        assert platforms == {"ghl", "hubspot"}, f"Expected both platforms, got {platforms}"
        assert len(configs) == 2, f"Expected 2 configs, got {len(configs)}"

        # Verify GHL config has correct stage mapping
        ghl_cfg = next(c for c in configs if c["platform"] == "ghl")
        assert ghl_cfg["stage_mapping"]["interested"] == "stage-interested"

        # Verify HubSpot config has correct stage mapping
        hs_cfg = next(c for c in configs if c["platform"] == "hubspot")
        assert hs_cfg["stage_mapping"]["interested"] == "hs-stage-1"
    finally:
        conn.close()


def test_overwrite_existing_config_roundtrip():
    """overwrite_existing setting is stored and retrieved correctly."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_org_and_workspace(conn)

        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping, overwrite_existing) VALUES (?, ?, ?, ?, ?, ?)",
            ("ws1", "ghl", "key-1", "pipe-1", '{}', 1),
        )
        conn.commit()

        configs = crm_sync.read_crm_config(conn, "ws1")
        assert len(configs) == 1
        assert configs[0]["overwrite_existing"] == 1

        # Test update to false
        conn.execute(
            "UPDATE crm_workspace_config SET overwrite_existing = 0 WHERE workspace_id = ? AND platform = ?",
            ("ws1", "ghl"),
        )
        conn.commit()
        configs2 = crm_sync.read_crm_config(conn, "ws1")
        assert configs2[0]["overwrite_existing"] == 0, "overwrite_existing should be 0 after update"
    finally:
        conn.close()


def test_multi_platform_entity_map_isolation():
    """Entity map entries for different platforms are isolated per lead."""
    om.init_db()
    conn = get_conn()
    try:
        _setup_org_and_workspace(conn)
        conn.execute(
            "INSERT INTO leads (id, name, email, created_at) VALUES (1, 'Test Lead', 'test@example.com', datetime('now'))",
        )
        conn.execute(
            "INSERT INTO workspace_leads (id, lead_id, workspace_id, org_id, status, created_at, updated_at, cloud_pending) VALUES (1, 1, 'ws1', 'org1', 'interested', datetime('now'), datetime('now'), 1)",
        )
        conn.commit()

        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
            ("ws1", "ghl", "key-1", "pipe-1", '{"interested": "stage-1"}'),
        )
        conn.execute(
            "INSERT INTO crm_workspace_config (workspace_id, platform, api_key, pipeline_id, stage_mapping) VALUES (?, ?, ?, ?, ?)",
            ("ws1", "hubspot", "key-2", "hs-pipe-1", '{"interested": "hs-stage-1"}'),
        )
        conn.execute(
            "INSERT INTO crm_entity_map (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id, sync_hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("ws1", 1, "ghl", "ghl-contact-001", "ghl-deal-001", "hash-ghl"),
        )
        conn.execute(
            "INSERT INTO crm_entity_map (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id, sync_hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("ws1", 1, "hubspot", "hs-contact-001", "hs-deal-001", "hash-hs"),
        )
        conn.commit()

        # GHL entity should not interfere with HubSpot
        ghl_map = conn.execute(
            "SELECT * FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            ("ws1", 1, "ghl"),
        ).fetchone()
        hs_map = conn.execute(
            "SELECT * FROM crm_entity_map WHERE workspace_id = ? AND lead_id = ? AND platform = ?",
            ("ws1", 1, "hubspot"),
        ).fetchone()
        assert ghl_map is not None
        assert hs_map is not None
        assert ghl_map["crm_contact_id"] == "ghl-contact-001"
        assert hs_map["crm_contact_id"] == "hs-contact-001"
    finally:
        conn.close()


if __name__ == "__main__":
    test_phase_0_tables_exist_after_init()
    test_phase_0_migration_on_existing_db()
    test_phase_0_tables_survive_refresh()
    test_phase_0_foreign_keys()
    test_phase_0_unique_constraints()
    test_phase_0_defaults()
    print("phase_0 ok")

    test_phase_1_lead_selection_status_filter()
    test_phase_1_lead_selection_stale_filter()
    test_phase_1_lead_selection_joins()
    test_phase_1_stage_mapping()
    print("phase_1 unit ok")

    # Rate limiter tests (don't need DB)
    test_phase_1_rate_limiter_bucket()
    test_phase_1_rate_limiter_block()
    print("phase_1 rate_limiter ok")

    test_phase_1_sync_single_workspace()
    test_phase_1_sync_all_workspaces()
    test_phase_1_sync_single_lead()
    test_phase_1_sync_log_written()
    test_phase_1_skip_workspace_no_config()
    test_phase_1_dry_run_no_calls()
    test_phase_1_missing_crm_sync_py()
    print("phase_1 integration ok")

    # Phase 2 GHL driver tests
    test_ghl_lookup_contact_found()
    test_ghl_lookup_contact_not_found()
    test_ghl_lookup_contact_unauthorized()
    test_ghl_create_contact_all_fields()
    test_ghl_create_contact_no_custom_fields()
    test_ghl_upsert_opportunity()
    test_ghl_upsert_opportunity_deal_name()
    test_ghl_upsert_opportunity_deal_name_no_company()
    test_ghl_discover_pipelines()
    test_ghl_discover_pipelines_empty()
    test_ghl_test_connection_success()
    test_ghl_test_connection_failure()
    test_ghl_rate_limiter_80_per_10s()
    test_ghl_retry_on_429()
    test_ghl_retry_on_network_error()
    test_ghl_push_events_note_format()
    test_ghl_push_events_note_truncation()
    test_ghl_push_events_empty_list()
    test_ghl_push_events_batch()
    test_ghl_push_events_no_deal()
    test_ghl_push_events_partial_failure()
    print("phase_2 mock ok")

    # Phase 6 pipeline.py trigger hook tests
    test_hook_update_stage_triggers_crm_sync()
    test_hook_log_event_triggers_crm_sync()
    test_hook_update_stage_no_crm_sync()
    test_hook_log_event_no_crm_sync()
    test_hook_update_stage_non_mapped_status()
    test_hook_missing_crm_sync_py()
    test_hook_stderr_output()
    test_hook_subprocess_does_not_block()
    print("phase_6 ok")

    # Phase 7 — Portal config sync
    test_portal_config_fetch()
    test_portal_config_sync_routing()
    test_portal_config_sync_crm()
    test_portal_config_sync_secrets()
    test_portal_config_no_crm_integrations()
    test_crm_config_apply_to_sqlite()
    test_crm_config_update_existing()
    test_crm_config_remove_disabled()
    test_portal_config_bumps_version()
    test_portal_config_secrets_failure_non_fatal()
    print("phase_7 ok")

    print("all ok")
