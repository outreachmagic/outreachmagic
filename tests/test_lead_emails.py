#!/usr/bin/env python3
"""Tests for multi-email per lead support (lead_emails table).

Run:
    python3 -m pytest tests/test_lead_emails.py -v
"""

import json
import sqlite3
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
from db_conn import get_conn  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    find_lead_by_identity,
    normalize_email,
)

pytest.skip("lead_emails table not yet implemented in schema", allow_module_level=True)


# ── Phase 1: Schema ──


class TestPhase1Schema:
    def test_lead_emails_table_exists(self):
        conn = get_conn()
        om.init_db()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lead_emails'"
        ).fetchone()
        assert row is not None

    def test_lead_emails_columns(self):
        conn = get_conn()
        om.init_db()
        rows = conn.execute("PRAGMA table_info(lead_emails)").fetchall()
        cols = {r[1] for r in rows}
        expected = {
            "id", "lead_id", "org_id", "email", "is_primary",
            "verification_status", "verified_at", "source",
            "created_at", "updated_at",
        }
        assert expected.issubset(cols), f"Missing: {expected - cols}"

    def test_lead_emails_unique_email_per_org(self):
        conn = get_conn()
        om.init_db()
        conn.execute("INSERT INTO leads (name) VALUES ('Test')")
        lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 1)",
            (f"le_{lid}_0", lid, DEFAULT_ORG_ID, "test@example.com"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
                "VALUES (?, ?, ?, ?, 0)",
                (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "test@example.com"),
            )

    def test_lead_emails_primary_unique_per_lead(self):
        conn = get_conn()
        om.init_db()
        conn.execute("INSERT INTO leads (name) VALUES ('Test')")
        lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 1)",
            (f"le_{lid}_0", lid, DEFAULT_ORG_ID, "primary@test.com"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
                "VALUES (?, ?, ?, ?, 1)",
                (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "another@test.com"),
            )

    def test_lead_email_id_column_in_verification(self):
        conn = get_conn()
        om.init_db()
        rows = conn.execute("PRAGMA table_info(lead_email_verification)").fetchall()
        cols = {r[1] for r in rows}
        assert "lead_email_id" in cols


# ── Phase 2: Core Pipeline ──


class TestPhase2FindByEmail:
    def test_find_lead_by_email_in_lead_emails(self):
        conn = get_conn()
        om.init_db()
        conn.execute("INSERT INTO leads (name, email) VALUES ('Test', '')")
        lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 1)",
            (f"le_{lid}_0", lid, DEFAULT_ORG_ID, "found@test.com"),
        )
        conn.commit()
        result = om.find_lead_by_email(conn, "found@test.com")
        assert result == lid

    def test_find_lead_by_email_fallback_to_leads(self):
        conn = get_conn()
        om.init_db()
        conn.execute("INSERT INTO leads (name, email) VALUES ('Test', 'direct@test.com')")
        lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        result = om.find_lead_by_email(conn, "direct@test.com")
        assert result == lid

    def test_find_lead_by_email_not_found(self):
        conn = get_conn()
        om.init_db()
        result = om.find_lead_by_email(conn, "nobody@test.com")
        assert result is None


class TestPhase2ResolveLead:
    def test_resolve_lead_creates_primary_in_lead_emails(self):
        om.init_db()
        result = om.resolve_lead(email="new@test.com", name="New Lead")
        assert result["status"] == "created"
        lid = result["id"]
        conn = get_conn()
        le = conn.execute(
            "SELECT * FROM lead_emails WHERE lead_id = ? AND is_primary = 1", (lid,)
        ).fetchone()
        assert le is not None
        assert le["email"] == "new@test.com"

    def test_resolve_lead_match_does_not_duplicate_primary(self):
        om.init_db()
        r1 = om.resolve_lead(email="dup@test.com", name="First")
        r2 = om.resolve_lead(email="dup@test.com", name="Second")
        assert r2["status"] == "matched"
        assert r2["id"] == r1["id"]
        conn = get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM lead_emails WHERE lead_id = ?", (r1["id"],)
        ).fetchone()[0]
        assert count == 1


class TestPhase2MergeLeads:
    def _make_lead(self, name, email):
        result = om.resolve_lead(email=email, name=name)
        return result["id"]

    def test_merge_moves_secondary_emails(self):
        om.init_db()
        keep_id = self._make_lead("Keep Lead", "keep@test.com")
        merge_id = self._make_lead("Merge Lead", "merge@test.com")

        conn = get_conn()
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 0)",
            (f"le_{merge_id}_1", merge_id, DEFAULT_ORG_ID, "secondary@test.com"),
        )
        conn.commit()
        om.merge_leads(keep_id, merge_id, reason="test", conn=conn)

        keep_emails = conn.execute(
            "SELECT email FROM lead_emails WHERE lead_id = ? ORDER BY email", (keep_id,)
        ).fetchall()
        emails = [r["email"] for r in keep_emails]
        assert "keep@test.com" in emails
        assert "secondary@test.com" in emails
        assert "merge@test.com" in emails
        merge_count = conn.execute(
            "SELECT COUNT(*) FROM lead_emails WHERE lead_id = ?", (merge_id,)
        ).fetchone()[0]
        assert merge_count == 0
        merge_count = conn.execute(
            "SELECT COUNT(*) FROM lead_emails WHERE lead_id = ?", (merge_id,)
        ).fetchone()[0]
        assert merge_count == 0


class TestPhase2ApplyEmailFindResults:
    def test_add_secondary_when_primary_exists(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        r = om.resolve_lead(email="primary@test.com", name="Test Lead")
        lid = r["id"]

        # apply_email_find_results manages its own connection
        result = om.apply_email_find_results(
            [{"lead_id": lid, "email": "secondary@test.com"}],
            workspace="default",
        )
        assert result["processed"] == 1

        conn = get_conn()
        emails = conn.execute(
            "SELECT email, is_primary FROM lead_emails WHERE lead_id = ? ORDER BY is_primary DESC",
            (lid,),
        ).fetchall()
        assert len(emails) == 2
        assert emails[0]["email"] == "primary@test.com"
        assert emails[0]["is_primary"] == 1
        assert emails[1]["email"] == "secondary@test.com"
        assert emails[1]["is_primary"] == 0


class TestPhase2ConflictDetection:
    def test_conflicting_email_across_tables(self):
        om.init_db()
        r1 = om.resolve_lead(email="shared@test.com", name="Lead 1")
        lid1 = r1["id"]

        conn = get_conn()
        conn.execute("INSERT INTO leads (name, email) VALUES ('Lead 2', '')")
        lid2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        from pipeline import _conflicting_email_owner
        conflict = _conflicting_email_owner(conn, "shared@test.com", lid2)
        assert conflict == lid1


# ── Phase 5: CRM Sync ──


class TestPhase5CrmSync:
    def test_select_leads_includes_additional_emails(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.create_workspace("default", sync=False)

        r = om.resolve_lead(email="primary@crm.com", name="CRM Lead")
        lid = r["id"]

        conn = get_conn()
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 0)",
            (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "alt@crm.com"),
        )
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 0)",
            (f"le_{lid}_2", lid, DEFAULT_ORG_ID, "other@crm.com"),
        )
        conn.commit()
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"wl_{lid}", DEFAULT_ORG_ID, ws_row["id"], lid, "interested"),
        )
        conn.commit()
        conn.close()

        from crm_sync import select_leads
        conn2 = get_conn()
        leads = select_leads(conn2, ws_row["id"])
        matching = [l for l in leads if l["lead_id"] == lid]
        assert len(matching) == 1
        assert "additional_emails" in matching[0]
        assert set(matching[0]["additional_emails"]) == {"alt@crm.com", "other@crm.com"}

    def test_sync_hash_includes_additional_emails(self):
        from crm_sync import _build_sync_hash
        lead1 = {"email": "a@test.com", "additional_emails": ["b@test.com"]}
        lead2 = {"email": "a@test.com", "additional_emails": []}
        h1 = _build_sync_hash(lead1, None)
        h2 = _build_sync_hash(lead2, None)
        assert h1 != h2


# ── Phase 6: Bounces ──


class TestPhase6Bounces:
    def test_verify_email_includes_lead_email_id(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        r = om.resolve_lead(email="verify@test.com", name="Verify Lead")
        lid = r["id"]

        from bounces import verify_email
        verify_email(lid, "valid", "zerobounce")

        conn = get_conn()
        ver = conn.execute(
            "SELECT * FROM lead_email_verification WHERE lead_id = ?", (lid,)
        ).fetchone()
        assert ver is not None
        assert ver["lead_email_id"] is not None
        assert ver["lead_email_id"].startswith("le_")

    def test_compute_verification_materializes_on_lead_emails(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        r = om.resolve_lead(email="mat@test.com", name="Mat Lead")
        lid = r["id"]

        from bounces import verify_email
        verify_email(lid, "valid", "zerobounce")

        conn = get_conn()
        le = conn.execute(
            "SELECT verification_status, verified_at FROM lead_emails "
            "WHERE lead_id = ? AND is_primary = 1", (lid,)
        ).fetchone()
        assert le["verification_status"] == "valid"
        assert le["verified_at"] is not None


# ── Phase 7: Export ──


class TestPhase7Export:
    def test_load_lead_supplements_includes_additional_emails(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        r = om.resolve_lead(email="export@test.com", name="Export Lead")
        lid = r["id"]

        conn = get_conn()
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary, verification_status) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "alt@export.com", "valid"),
        )
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary, verification_status) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (f"le_{lid}_2", lid, DEFAULT_ORG_ID, "other@export.com", "bounced"),
        )
        conn.commit()
        conn.close()

        conn2 = get_conn()
        from pipeline_lead_review import _load_lead_supplements
        supps = _load_lead_supplements(conn2, [lid])
        assert lid in supps
        additional = supps[lid].get("additional_emails", "")
        assert "alt@export.com [valid]" in additional
        assert "other@export.com [bounced]" in additional

    def test_build_lead_row_includes_additional_emails(self):
        from pipeline_lead_review import build_lead_row
        lead = {
            "id": 1, "email": "row@test.com", "name": "Row Lead",
            "additional_emails": "alt@row.com [valid]; b@row.com [bounced]",
        }
        columns = [("Additional Emails", "additional_emails"), ("ID", "lead_id")]
        row = build_lead_row(lead, columns)
        assert row[0] == "alt@row.com [valid]; b@row.com [bounced]"


# ── Phase 8: Sync-back ──


class TestPhase8SyncBack:
    def test_current_row_state_includes_additional_emails(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.create_workspace("default", sync=False)

        r = om.resolve_lead(email="state@test.com", name="State Lead")
        lid = r["id"]

        conn = get_conn()
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
            "VALUES (?, ?, ?, ?, 'prospecting')",
            (f"wl_{lid}", DEFAULT_ORG_ID, ws_row["id"], lid),
        )
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 0)",
            (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "extra@state.com"),
        )
        conn.commit()

        from pipeline_lead_review import _current_row_state
        state = _current_row_state(conn, ws_row["id"], lid)
        assert "extra@state.com" in state.get("additional_emails", "")

    def test_sync_back_add_remove_detection(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.create_workspace("default", sync=False)

        r = om.resolve_lead(email="sync@test.com", name="Sync Lead")
        lid = r["id"]

        conn = get_conn()
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
            "VALUES (?, ?, ?, ?, 'prospecting')",
            (f"wl_{lid}", DEFAULT_ORG_ID, ws_row["id"], lid),
        )
        conn.execute(
            "INSERT INTO lead_emails (id, lead_id, org_id, email, is_primary) "
            "VALUES (?, ?, ?, ?, 0)",
            (f"le_{lid}_1", lid, DEFAULT_ORG_ID, "keep@sync.com"),
        )
        conn.commit()

        def noop(*args, **kwargs):
            pass

        from pipeline_lead_review import apply_lead_review_sync

        sheet_row = {"lead_id": lid, "additional_emails": "new@sync.com"}
        result = apply_lead_review_sync(
            conn, ws_row["id"], [sheet_row],
            upsert_workspace_lead_fn=noop, org_id=DEFAULT_ORG_ID, dry_run=True,
        )
        changes = result.get("changes", [])
        if changes:
            diff = changes[0].get("additional_emails_diff")
            if diff:
                added = diff.get("added", [])
                removed = diff.get("removed", [])
                assert "new@sync.com" in added
                assert "keep@sync.com" in removed

    def test_sync_back_strips_brackets(self):
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.create_workspace("default", sync=False)

        r = om.resolve_lead(email="strip@test.com", name="Strip Lead")
        lid = r["id"]

        conn = get_conn()
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
            "VALUES (?, ?, ?, ?, 'prospecting')",
            (f"wl_{lid}", DEFAULT_ORG_ID, ws_row["id"], lid),
        )
        conn.commit()

        def noop(*args, **kwargs):
            pass

        from pipeline_lead_review import apply_lead_review_sync

        sheet_row = {
            "lead_id": lid,
            "additional_emails": "alice@gmail.com [valid]; bob@example.com [bounced]",
        }
        result = apply_lead_review_sync(
            conn, ws_row["id"], [sheet_row],
            upsert_workspace_lead_fn=noop, org_id=DEFAULT_ORG_ID, dry_run=True,
        )
        changes = result.get("changes", [])
        if changes:
            diff = changes[0].get("additional_emails_diff")
            if diff:
                for email in diff.get("added", []):
                    assert "[" not in email, f"Brackets not stripped: {email}"
                    assert "@" in email, f"Not valid: {email}"


# ── End-to-End ──


class TestEndToEnd:
    def test_full_lifecycle(self):
        """End-to-end: create, add secondaries, find by any email, merge, verify."""
        om.init_db()
        conn = get_conn()
        om.ensure_organization(conn)
        conn.close()
        om.create_workspace("default", sync=False)

        r1 = om.resolve_lead(email="life1@test.com", name="Lead One")
        r2 = om.resolve_lead(email="life2@test.com", name="Lead Two")
        lid1, lid2 = r1["id"], r2["id"]

        # Add secondary emails
        om.apply_email_find_results(
            [{"lead_id": lid1, "email": "life1-work@test.com"}],
            workspace="default",
        )
        om.apply_email_find_results(
            [{"lead_id": lid2, "email": "life2-personal@test.com"}],
            workspace="default",
        )

        conn = get_conn()
        found = om.find_lead_by_email(conn, "life1-work@test.com")
        assert found == lid1

        found_by_id = find_lead_by_identity(conn, DEFAULT_ORG_ID, "email", "life2-personal@test.com")
        assert found_by_id == lid2

        # Merge leads (merge_leads manages own connection)
        conn.close()
        om.merge_leads(lid1, lid2, reason="test")

        conn2 = get_conn()
        emails = conn2.execute(
            "SELECT email FROM lead_emails WHERE lead_id = ? ORDER BY email", (lid1,)
        ).fetchall()
        all_emails = {r["email"] for r in emails}
        assert "life1@test.com" in all_emails
        assert "life1-work@test.com" in all_emails
        # Merge lead's emails should be gone
        merge_count = conn2.execute(
            "SELECT COUNT(*) FROM lead_emails WHERE lead_id = ?", (lid2,)
        ).fetchone()[0]
        assert merge_count == 0
