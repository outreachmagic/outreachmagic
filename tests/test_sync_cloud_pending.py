#!/usr/bin/env python3
"""Tests for timestamp-based sync (get_last_sync / set_last_sync)."""

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402


class TimestampSyncTests(unittest.TestCase):
    def setUp(self):
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def test_get_last_sync_returns_none_initially(self):
        self.assertIsNone(om.get_last_sync())

    def test_set_and_get_last_sync_roundtrip(self):
        ts = "2026-06-27T12:00:00Z"
        om.set_last_sync(ts)
        # set_last_sync normalizes to SQLite-compatible format
        self.assertEqual(om.get_last_sync(), "2026-06-27 12:00:00")

    def test_set_last_sync_overwrites_previous(self):
        om.set_last_sync("2026-06-01T00:00:00Z")
        om.set_last_sync("2026-06-27T00:00:00Z")
        # set_last_sync normalizes to SQLite-compatible format
        self.assertEqual(om.get_last_sync(), "2026-06-27 00:00:00")

    def test_get_last_sync_normalizes_old_iso_format(self):
        """Legacy configs may have ISO-format last_sync. get_last_sync normalizes."""
        with mock.patch.object(om, 'load_config', return_value={"last_sync": "2026-06-27T12:00:00.500000+00:00"}):
            self.assertEqual(om.get_last_sync(), "2026-06-27 12:00:00")

    def test_get_last_sync_passes_through_sqlite_format(self):
        """If config already has SQLite-compatible format, pass through unchanged."""
        with mock.patch.object(om, 'load_config', return_value={"last_sync": "2026-06-27 12:00:00"}):
            self.assertEqual(om.get_last_sync(), "2026-06-27 12:00:00")

    def test_lead_with_updated_at_after_last_sync_is_pending(self):
        om.set_last_sync("2026-06-01T00:00:00Z")
        result = om.resolve_lead(
            email="pending@example.com",
            name="Pending User",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        lead_id = result["id"]
        # The lead was just created, so updated_at > last_sync
        conn = om.get_conn()
        last_sync = om.get_last_sync()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE updated_at > ?",
            (last_sync,),
        ).fetchone()["n"]
        conn.close()
        self.assertGreaterEqual(count, 1)

    def test_lead_with_updated_at_before_last_sync_is_not_pending(self):
        om.set_last_sync("2099-01-01T00:00:00Z")
        result = om.resolve_lead(
            email="notpending@example.com",
            name="Not Pending",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        conn = om.get_conn()
        last_sync = om.get_last_sync()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE updated_at > ?",
            (last_sync,),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(count, 0)

    def test_workspace_lead_pending_detection(self):
        om.set_last_sync("2026-06-01T00:00:00Z")
        result = om.resolve_lead(
            email="ws-pending@example.com",
            name="WS Pending",
            company="Acme",
            source="csv",
            source_platform="csv",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        ws_row = om.resolve_workspace_identity(conn, "default")
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
        conn.commit()
        last_sync = om.get_last_sync()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_leads WHERE updated_at > ?",
            (last_sync,),
        ).fetchone()["n"]
        conn.close()
        self.assertGreaterEqual(count, 1)

    def test_sync_updates_last_sync(self):
        initial_ts = "2026-06-01T00:00:00Z"
        om.set_last_sync(initial_ts)
        conn = om.get_conn()
        conn.execute(
            "INSERT INTO leads (name, email, channel, stage, original_source, original_source_platform, updated_at) "
            "VALUES ('Sync Lead', 'sync@example.com', 'email', 'prospecting', 'csv', 'csv', '2026-06-15T00:00:00')"
        )
        conn.commit()
        conn.close()

        # Simulate that sync_all would set last_sync to now after pushing
        om.set_last_sync("2026-06-27T12:00:00Z")
        new_ts = om.get_last_sync()
        self.assertNotEqual(new_ts, initial_ts)
        self.assertEqual(new_ts, "2026-06-27 12:00:00")

    def test_resolve_lead_relay_sync_marks_updated_at(self):
        """verify that resolve_lead still works correctly (cloud_pending column removed)."""
        result = om.resolve_lead(
            email="relay@example.com",
            name="Relay User",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        self.assertEqual(result["status"], "created")
        conn = om.get_conn()
        row = conn.execute(
            "SELECT updated_at, original_source FROM leads WHERE id = ?", (result["id"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["updated_at"])
        self.assertEqual(row["original_source"], "relay_sync")

    def test_relay_push_defaults(self):
        for key in (
            "OUTREACHMAGIC_SYNC_BATCH_SIZE",
            "OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS",
        ):
            os.environ.pop(key, None)
        settings = om.get_relay_push_settings()
        self.assertEqual(settings["batch_size"], 200)
        self.assertEqual(settings["timeout_seconds"], 120)
        self.assertFalse(settings.get("bulk"))

        bulk_settings = om.get_relay_push_settings(bulk=True)
        self.assertEqual(bulk_settings["batch_size"], 5000)
        self.assertTrue(bulk_settings.get("bulk"))

    def test_push_agent_events_marks_only_fully_successful_batches(self):
        conn = om.get_conn()
        conn.execute(
            """INSERT INTO leads (name, email, channel, stage, original_source, original_source_platform)
               VALUES ('E1', 'e1@example.com', 'email', 'prospecting', 'csv', 'csv')"""
        )
        lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, created_at, metadata_json)
               VALUES (?, 'email_sent', 'outbound', 'email', datetime('now'), '{}')""",
            (lid,),
        )
        eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        def fake_batches(agent_key, entries, client_id, **kwargs):
            on_batch = kwargs.get("on_batch_pushed")
            if on_batch and entries:
                on_batch(entries, 0)
            return {"pushed": 0, "error": None, "throttled": False}

        with mock.patch.object(om, "_relay_push_batches", side_effect=fake_batches):
            result = om._push_agent_events_to_relay("om_agent_test")

        self.assertEqual(result.get("events_marked_pushed"), 0)
        conn = om.get_conn()
        row = conn.execute(
            "SELECT 1 FROM relay_ingested WHERE dedupe_key = ?", (f"event:{eid}",)
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_mark_all_lead_snapshots_pending_updates_updated_at(self):
        """mark_all_lead_snapshots_pending now sets updated_at = datetime('now')."""
        result = om.resolve_lead(
            email="marktest@example.com",
            name="Mark Test",
            company="Acme",
            source="relay_sync",
            source_platform="smartlead",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        # Set updated_at to old value
        conn.execute(
            "UPDATE leads SET updated_at = '2020-01-01T00:00:00' WHERE id = ?",
            (lead_id,),
        )
        conn.commit()
        conn.close()

        om.mark_all_lead_snapshots_pending()

        conn = om.get_conn()
        row = conn.execute(
            "SELECT updated_at FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["updated_at"])
        self.assertNotEqual(row["updated_at"], "2020-01-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
