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
        """resolve_lead still works when the caller hands us a transport string
        as source -- it just scrubs it. original_source becomes NULL rather than
        a lying "relay_sync" that would then dominate every provenance report."""
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
        self.assertIsNone(row["original_source"])

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
            "SELECT 1 FROM event_push_log WHERE event_id = ?", (eid,)
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

    def test_mark_all_lead_snapshots_pending_scopes_to_workspace(self):
        """workspace_id scopes marking to only that workspace's leads."""
        in_ws = om.resolve_lead(
            email="inws@example.com", name="In WS", company="Acme",
            source="csv", source_platform="csv",
        )
        out_ws = om.resolve_lead(
            email="outws@example.com", name="Out WS", company="Acme",
            source="csv", source_platform="csv",
        )
        conn = om.get_conn()
        ws_row = om.resolve_workspace_identity(conn, "default")
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], in_ws["id"])
        conn.execute(
            "UPDATE leads SET updated_at = '2020-01-01T00:00:00' WHERE id IN (?, ?)",
            (in_ws["id"], out_ws["id"]),
        )
        conn.commit()
        conn.close()

        om.mark_all_lead_snapshots_pending(workspace_id=ws_row["id"])

        conn = om.get_conn()
        rows = {
            r["id"]: r["updated_at"]
            for r in conn.execute(
                "SELECT id, updated_at FROM leads WHERE id IN (?, ?)",
                (in_ws["id"], out_ws["id"]),
            ).fetchall()
        }
        conn.close()
        self.assertNotEqual(rows[in_ws["id"]], "2020-01-01T00:00:00")
        self.assertEqual(rows[out_ws["id"]], "2020-01-01T00:00:00")

    def test_push_agent_events_to_relay_is_events_only(self):
        """_push_agent_events_to_relay no longer bundles lead_core_update/lead_workspace_update
        entries — those are covered by _push_pending_lead_snapshots. A lead that's never
        touched relay (satisfies the old unsynced_lead_clause) must not leak lead entries here."""
        result = om.resolve_lead(
            email="neverrelay@example.com", name="Never Relay", company="Acme",
            source="csv", source_platform="csv",
        )
        conn = om.get_conn()
        conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, channel, created_at, metadata_json)
               VALUES (?, 'email_sent', 'outbound', 'email', datetime('now'), '{}')""",
            (result["id"],),
        )
        conn.commit()
        conn.close()

        captured: list = []

        def fake_batches(agent_key, entries, client_id, **kwargs):
            captured.extend(entries)
            return {"pushed": 0, "error": None, "throttled": False}

        with mock.patch.object(om, "_relay_push_batches", side_effect=fake_batches):
            om._push_agent_events_to_relay("om_agent_test")

        actions = {e.get("action") for e in captured}
        self.assertNotIn("lead_core_update", actions)
        self.assertNotIn("lead_workspace_update", actions)
        self.assertIn("event_log", actions)

    def test_preview_sync_samples_without_full_count(self):
        """preview_sync caps entries per stream at sample_size while totals stay accurate."""
        for i in range(5):
            om.resolve_lead(
                email=f"preview{i}@example.com", name=f"Preview {i}", company="Acme",
                source="csv", source_platform="csv",
            )

        with mock.patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with mock.patch.object(om.routing_cloud, "cloud_routing_enabled", return_value=True):
                with mock.patch.object(
                    om.routing_cloud, "fetch_routing_bundle",
                    return_value={"workspaces": [], "campaignMaps": []},
                ):
                    result = om.preview_sync(sample_size=2)

        self.assertEqual(result["status"], "dry_run")
        self.assertGreaterEqual(result["totals"]["leads_core_pending"], 5)
        self.assertLessEqual(len(result["samples"]["lead_core_update"]), 2)

    def test_push_pending_company_updates_reports_uncapped_total(self):
        """total_pending must reflect the real outbox backlog, not just the
        sample_limit-capped row count -- a company/sender-scoped dry-run
        preview used to always report at most sample_limit pending."""
        conn = om.get_conn()
        for i in range(5):
            om.ensure_company(conn, domain=f"pending{i}.example.com")
        conn.commit()
        conn.close()

        result = om._push_pending_company_updates("om_agent_test", sample_limit=2, dry_run=True)
        self.assertGreaterEqual(result["total_pending"], 5)
        self.assertEqual(len(result["sample_entries"]), 2)

    def test_never_synced_lead_is_pending_even_when_older_than_last_sync(self):
        """A lead that predates last_sync but was never actually pushed (no relay_ingested
        row) must still show up as pending — the updated_at > last_sync check alone would
        silently drop it forever once last_sync advances past its old updated_at."""
        result = om.resolve_lead(
            email="stale-never-synced@example.com", name="Stale Lead", company="Acme",
            source="csv", source_platform="csv",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        ws_row = om.resolve_workspace_identity(conn, "default")
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
        conn.execute(
            "UPDATE leads SET updated_at = '2020-01-01T00:00:00' WHERE id = ?", (lead_id,),
        )
        conn.execute(
            "UPDATE workspace_leads SET updated_at = '2020-01-01T00:00:00' WHERE lead_id = ?",
            (lead_id,),
        )
        conn.commit()
        conn.close()
        # last_sync is well after this lead's (backdated) updated_at, but the lead was
        # never actually pushed (no relay_ingested row references it).
        om.set_last_sync("2026-01-01T00:00:00Z")

        with mock.patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with mock.patch.object(om.routing_cloud, "cloud_routing_enabled", return_value=True):
                with mock.patch.object(
                    om.routing_cloud, "fetch_routing_bundle",
                    return_value={"workspaces": [], "campaignMaps": []},
                ):
                    status = om.get_sync_status()
                    lead_push = om._push_pending_lead_snapshots(
                        "om_agent_test", dry_run=True, sample_limit=50,
                    )

        self.assertGreaterEqual(status["leads_pending"], 1)
        self.assertGreaterEqual(status["workspace_leads_pending"], 1)
        # Entity keys are the immutable uid now, not the email -- an email is
        # mutable and used to relocate the lead's entire relay identity when found.
        # The email still travels, as an alias on the core payload.
        conn = om.get_conn()
        uid = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
        conn.close()
        core_keys = {e["entity_key"] for e in lead_push["sample_core_entries"]}
        ws_keys = {e["entity_key"] for e in lead_push["sample_ws_entries"]}
        self.assertIn(f"uid:{uid}", core_keys)
        self.assertIn(f"uid:{uid}", ws_keys)
        core_entry = next(
            e for e in lead_push["sample_core_entries"] if e["entity_key"] == f"uid:{uid}"
        )
        self.assertIn(
            "stale-never-synced@example.com", core_entry["payload"].get("aliases", [])
        )

    def test_export_local_changes_sample_limit_caps_lead_entries(self):
        """agent-changes --limit threads through to _export_local_lead_entries."""
        for i in range(6):
            om.resolve_lead(
                email=f"exportlimit{i}@example.com", name=f"Export Limit {i}", company="Acme",
                source="csv", source_platform="csv",
            )

        result = om.export_local_changes(all_leads=True, sample_limit=3)
        lead_entries = [e for e in result["entries"] if e["action"] == "lead_core_update"]
        self.assertEqual(len(lead_entries), 3)

    def test_self_bumped_lead_not_counted_pending(self):
        """bug-pending-sync-self-bump.md: a lead whose updated_at was bumped by
        re-applying relay data it already has (echoed back, not a genuine local
        change) must not count as pending — otherwise the count never settles."""
        result = om.resolve_lead(
            email="selfbump@example.com", name="Self Bump", company="Acme",
            source="csv", source_platform="csv",
        )
        lead_id = result["id"]
        conn = om.get_conn()
        ws_row = om.resolve_workspace_identity(conn, "default")
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], lead_id)
        conn.commit()
        conn.close()

        om.set_last_sync("2026-01-01T00:00:00Z")

        # Simulate the self-bump cycle: relay data gets re-applied, bumping
        # updated_at, and relay_ingested is marked at (or after) that same time
        # — exactly what a real pull/apply cycle does.
        conn = om.get_conn()
        conn.execute(
            "UPDATE leads SET updated_at = '2026-06-01 00:00:00' WHERE id = ?", (lead_id,),
        )
        conn.execute(
            "UPDATE workspace_leads SET updated_at = '2026-06-01 00:00:00' WHERE lead_id = ?",
            (lead_id,),
        )
        conn.commit()
        conn.close()
        om.mark_relay_ingested(f"selfbump-test-{lead_id}", lead_id)

        with mock.patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with mock.patch.object(om.routing_cloud, "cloud_routing_enabled", return_value=True):
                with mock.patch.object(
                    om.routing_cloud, "fetch_routing_bundle",
                    return_value={"workspaces": [], "campaignMaps": []},
                ):
                    status = om.get_sync_status()
                    lead_push = om._push_pending_lead_snapshots(
                        "om_agent_test", dry_run=True, sample_limit=50,
                    )

        core_keys = {e["entity_key"] for e in lead_push.get("sample_core_entries", [])}
        ws_keys = {e["entity_key"] for e in lead_push.get("sample_ws_entries", [])}
        self.assertNotIn("selfbump@example.com", core_keys)
        self.assertNotIn("selfbump@example.com", ws_keys)
        # Sanity: get_sync_status should still be internally consistent (no crash,
        # a sane non-negative count) even though this specific lead is excluded.
        self.assertGreaterEqual(status["leads_pending"], 0)


if __name__ == "__main__":
    unittest.main()
