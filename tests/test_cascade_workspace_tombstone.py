#!/usr/bin/env python3
"""Deleting a lead must file a PUSHABLE workspace tombstone.

The lead_workspace tombstone reads its entity_key as
`(SELECT uid FROM leads WHERE id = OLD.lead_id)`. That works when a
workspace_leads row is deleted on its own, and returns NULL when the delete
arrives by ON DELETE CASCADE from `leads` -- the parent is already gone by the
time the child's BEFORE DELETE trigger runs.

A tombstone with no entity_key cannot be pushed, so it sits in the outbox
forever while the relay keeps serving the snapshot it was meant to retract.
Deleting 10,458 empty leads on the live database produced exactly 10,457
undeliverable rows.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import pipeline_migration as pm  # noqa: E402


class CascadeWorkspaceTombstoneTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _seed(self):
        lead_id = om.add_lead(name="Ann", email="ann@acme.com", company="Acme")["id"]
        conn = om.get_conn()
        try:
            org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
                ("ws-a", org_id, "wsa", "WS A"))
            conn.execute(
                "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id) "
                "VALUES (?,?,?,?)", (f"wl-{lead_id}", org_id, "ws-a", lead_id))
            conn.execute("DELETE FROM outbox")
            conn.commit()
            uid = conn.execute(
                "SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()["uid"]
        finally:
            conn.close()
        return lead_id, uid

    def _tombstones(self):
        conn = om.get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT entity_id, entity_key, workspace_slug FROM outbox "
                "WHERE entity_type = 'lead_workspace' AND op = 'delete'")]
        finally:
            conn.close()

    def test_cascade_delete_files_a_keyed_tombstone(self):
        lead_id, uid = self._seed()
        self.assertTrue(uid)
        conn = om.get_conn()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            conn.commit()
        finally:
            conn.close()

        rows = self._tombstones()
        self.assertEqual(len(rows), 1, "the workspace row must leave a tombstone")
        self.assertEqual(
            rows[0]["entity_key"], uid,
            "a tombstone with no entity_key can never be pushed -- the relay "
            "would keep serving the snapshot forever",
        )
        self.assertEqual(rows[0]["workspace_slug"], "wsa")

    def test_direct_workspace_row_delete_still_keyed(self):
        """The path that always worked must keep working."""
        lead_id, uid = self._seed()
        conn = om.get_conn()
        try:
            conn.execute("DELETE FROM workspace_leads WHERE lead_id = ?", (lead_id,))
            conn.commit()
        finally:
            conn.close()
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_key"], uid)

    def test_repair_recovers_keyless_tombstones_from_quarantine(self):
        lead_id, uid = self._seed()
        conn = om.get_conn()
        try:
            # Reproduce the stranded shape left by the original bug.
            conn.execute(
                "INSERT INTO outbox (entity_type, entity_id, op, entity_key, workspace_slug, dirty_at) "
                "VALUES ('lead_workspace', ?, 'delete', NULL, 'wsa', datetime('now'))",
                (f"{lead_id}:ws-a",))
            conn.execute(
                "INSERT INTO leads_junk_quarantine (lead_id, uid) VALUES (?, ?)",
                (lead_id, uid))
            conn.commit()
            recovered = pm._repair_keyless_workspace_tombstones(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(recovered, 1)
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_key"], uid)

    def test_repair_drops_tombstones_it_cannot_key(self):
        """An un-keyable tombstone can never be delivered; leaving it queued
        only makes 'pending' permanently wrong."""
        conn = om.get_conn()
        try:
            conn.execute("DELETE FROM outbox")
            conn.execute(
                "INSERT INTO outbox (entity_type, entity_id, op, entity_key, workspace_slug, dirty_at) "
                "VALUES ('lead_workspace', '999999:ws-a', 'delete', NULL, 'wsa', datetime('now'))")
            conn.commit()
            pm._repair_keyless_workspace_tombstones(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._tombstones(), [])


if __name__ == "__main__":
    unittest.main()
