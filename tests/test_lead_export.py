#!/usr/bin/env python3
"""Contacts export: presets, the field picker, and one filter set.

"Export whatever is on screen right now, all N of it" is only true if the
export and the contacts list build the same WHERE. So lead_export calls
dashboard_queries.lead_filter_clause() rather than growing its own filters --
which is exactly what pipeline_tags.export_leads had become, and why folding it
into a shim was part of this.
"""

import csv
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

import dashboard_queries as dq  # noqa: E402
import lead_export  # noqa: E402
import pipeline as om  # noqa: E402


class LeadExportTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        self.org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, org_id, slug, name) VALUES (?,?,?,?)",
            ("ws-a", self.org_id, "wsa", "WS A"))
        conn.commit()
        conn.close()
        self.ws = "ws-a"

    def _lead(self, *, tags=None, status=None, **kw):
        lead_id = om.add_lead(**kw)["id"]
        conn = om.get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id, status) "
                "VALUES (?,?,?,?,?)",
                (f"wl-{lead_id}", self.org_id, self.ws, lead_id, status or "prospecting"))
            for tag in tags or []:
                conn.execute(
                    "INSERT OR IGNORE INTO workspace_lead_tags (workspace_id, lead_id, tag) "
                    "VALUES (?,?,?)", (self.ws, lead_id, tag))
            conn.commit()
        finally:
            conn.close()
        return lead_id

    def _rows(self, **kw):
        conn = om.get_conn()
        try:
            return lead_export.export_rows(conn, self.ws, **kw)
        finally:
            conn.close()

    def _csv(self, **kw):
        conn = om.get_conn()
        try:
            result = lead_export.export_to_csv(conn, self.ws, workspace_slug="wsa", **kw)
        finally:
            conn.close()
        with open(result["file"], newline="", encoding="utf-8") as fh:
            return result, list(csv.DictReader(fh))

    # -- presets -------------------------------------------------------------

    def test_every_preset_produces_a_readable_csv(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp", title="VP")
        for preset in lead_export.PRESETS:
            result, rows = self._csv(preset=preset)
            self.assertEqual(result["count"], 1, preset)
            self.assertEqual(len(rows), 1, preset)

    def test_sequencer_preset_carries_the_merge_data(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        om.personalize_set(lead_id, "first_name", "Janey")
        _result, rows = self._csv(preset="sequencer-upload")
        self.assertEqual(rows[0]["email"], "jane@acme.com")
        self.assertEqual(rows[0]["personalized_first_name"], "Janey")

    def test_first_and_last_name_split_out_of_the_stored_name(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        _result, rows = self._csv(preset="sequencer-upload")
        self.assertEqual(rows[0]["first_name"], "Jane")
        self.assertEqual(rows[0]["last_name"], "Doe")

    def test_a_one_word_name_does_not_lose_the_first_name(self):
        self._lead(name="Cher", email="cher@acme.com", company="Acme Corp")
        _result, rows = self._csv(preset="sequencer-upload")
        self.assertEqual(rows[0]["first_name"], "Cher")
        self.assertEqual(rows[0]["last_name"], "")

    # -- an explicit selection --------------------------------------------
    #
    # "Configure export" used to send the filters and nothing else, so ticking
    # 50 rows and hitting Export produced the whole filtered list. A tick list
    # is a literal answer to "which rows" and has to win outright.

    def test_lead_ids_exports_only_the_selection(self):
        a = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        b = self._lead(name="John Roe", email="john@acme.com", company="Acme Corp")
        self._lead(name="Not Picked", email="nope@acme.com", company="Acme Corp")
        _cols, rows = self._rows(preset="sequencer-upload", lead_ids=[a, b])
        self.assertEqual(
            {r["email"] for r in rows}, {"jane@acme.com", "john@acme.com"})

    def test_an_empty_selection_exports_nothing(self):
        # Not "no id filter, so export everything" — that is the bug, restated.
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        _cols, rows = self._rows(preset="sequencer-upload", lead_ids=[])
        self.assertEqual(rows, [])

    def test_lead_ids_reaches_a_company_placeholder(self):
        # The contacts list hides placeholders by default. If one is on screen
        # (record_type=all) and gets ticked, exporting it must not silently drop
        # it back out — the server pairs lead_ids with record_type="all".
        lead_id = self._lead(name="Acme Corp", email=None, company="Acme Corp")
        conn = om.get_conn()
        try:
            conn.execute("UPDATE leads SET record_type = 'company_placeholder' WHERE id = ?",
                         (lead_id,))
            conn.commit()
        finally:
            conn.close()
        _cols, hidden = self._rows(preset="sequencer-upload", lead_ids=[lead_id])
        self.assertEqual(hidden, [])
        _cols, shown = self._rows(
            preset="sequencer-upload", lead_ids=[lead_id], record_type="all")
        self.assertEqual(len(shown), 1)

    def test_too_many_lead_ids_is_rejected(self):
        with self.assertRaises(ValueError):
            self._rows(preset="sequencer-upload",
                       lead_ids=list(range(dq.MAX_EXPLICIT_LEAD_IDS + 1)))

    def test_unknown_preset_is_rejected_with_the_valid_list(self):
        with self.assertRaises(lead_export.LeadExportError) as ctx:
            self._rows(preset="whatever")
        self.assertIn("sequencer-upload", str(ctx.exception))

    # -- the field picker ----------------------------------------------------

    def test_explicit_fields_beat_the_preset(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        cols, rows = self._rows(preset="full", fields=["email", "company"])
        self.assertEqual(cols, ["email", "company"])
        self.assertEqual(set(rows[0]), {"email", "company"})

    def test_field_options_report_the_workspaces_own_personalization(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        om.personalize_set(lead_id, "icebreaker", "saw your post")
        conn = om.get_conn()
        try:
            opts = lead_export.export_field_options(conn, self.ws)
        finally:
            conn.close()
        self.assertIn("personalized_icebreaker", opts["personalization"])
        self.assertIn("email", opts["base"])
        self.assertIn("sequencer-upload", opts["presets"])

    def test_an_unknown_column_is_rejected(self):
        with self.assertRaises(lead_export.LeadExportError):
            self._rows(fields=["email", "not_a_column"])

    def test_a_field_name_that_is_not_a_safe_identifier_is_rejected(self):
        """Column names reach SQL as headers and bind values; only the name has
        to be safe, but it does have to be."""
        with self.assertRaises(lead_export.LeadExportError):
            self._rows(fields=["personalized_x; DROP TABLE leads--"])

    def test_duplicate_fields_collapse(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        cols, _rows = self._rows(fields=["email", "email", "name"])
        self.assertEqual(cols, ["email", "name"])

    def test_no_columns_selected_is_an_error(self):
        with self.assertRaises(lead_export.LeadExportError):
            self._rows(fields=["  "])

    # -- shared filters ------------------------------------------------------

    def test_filters_match_the_contacts_list_exactly(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme", tags=["nace"])
        self._lead(name="Bob Roe", email="bob@acme.com", company="Acme")
        conn = om.get_conn()
        try:
            listed = dq.search_leads(conn, self.ws, tag="nace")["leads"]
        finally:
            conn.close()
        _cols, exported = self._rows(fields=["email"], tag="nace")
        self.assertEqual(
            {r["email"] for r in listed}, {r["email"] for r in exported})

    def test_placeholders_are_excluded_by_default_like_the_list(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        stub = self._lead(name="Acme Corp", company="Acme Corp")
        om.set_lead_record_type(stub, "company_placeholder")
        _cols, rows = self._rows(fields=["name"])
        self.assertEqual([r["name"] for r in rows], ["Jane Doe"])
        _cols, all_rows = self._rows(fields=["name"], record_type="all")
        self.assertEqual(len(all_rows), 2)

    def test_an_unknown_filter_is_rejected_rather_than_ignored(self):
        """Silently dropping a filter would export more rows than asked for."""
        with self.assertRaises(lead_export.LeadExportError):
            self._rows(fields=["email"], not_a_filter="x")

    def test_status_filter(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme", status="replied")
        self._lead(name="Bob Roe", email="bob@acme.com", company="Acme", status="prospecting")
        _cols, rows = self._rows(fields=["name"], status="replied")
        self.assertEqual([r["name"] for r in rows], ["Jane Doe"])

    # -- message blocks ------------------------------------------------------

    def test_message_blocks_carry_the_latest_each_way(self):
        lead_id = self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        self._event(lead_id, "email_sent", "outbound", "First touch", "hello there",
                    "2026-07-01 09:00:00")
        self._event(lead_id, "email_sent", "outbound", "Follow up", "circling back",
                    "2026-07-05 09:00:00")
        self._event(lead_id, "email_reply", "inbound", "Re: Follow up", "sure, let's talk",
                    "2026-07-06 09:00:00")
        _cols, rows = self._rows(preset="replies-review")
        row = rows[0]
        self.assertEqual(row["last_message_sent_subject"], "Follow up")
        self.assertEqual(row["last_message_sent_body"], "circling back")
        self.assertEqual(row["last_message_received_subject"], "Re: Follow up")
        self.assertEqual(row["last_message_received_body"], "sure, let's talk")

    def test_a_lead_with_no_messages_gets_empty_blocks_not_an_error(self):
        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme")
        _cols, rows = self._rows(preset="replies-review")
        self.assertIsNone(rows[0]["last_message_sent_at"])
        self.assertIsNone(rows[0]["last_message_received_body"])

    def _event(self, lead_id, event_type, direction, subject, body, created_at):
        conn = om.get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO events
                       (lead_id, event_type, direction, channel, subject,
                        metadata_json, created_at)
                   VALUES (?,?,?,'email',?,?,?)""",
                (lead_id, event_type, direction, subject,
                 f'{{"body": "{body}"}}', created_at))
            conn.execute(
                """INSERT INTO workspace_lead_events
                       (org_id, workspace_id, lead_id, event_id, event_type,
                        event_at, idempotency_key)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.org_id, self.ws, lead_id, cur.lastrowid, event_type,
                 created_at, f"k-{cur.lastrowid}"))
            conn.commit()
        finally:
            conn.close()

    # -- output --------------------------------------------------------------

    def test_csv_lands_in_the_export_dir_with_the_selected_headers(self):
        from om_paths import get_export_dir

        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        result, rows = self._csv(fields=["email", "name", "company"])
        self.assertTrue(str(result["file"]).startswith(str(get_export_dir())))
        self.assertEqual(list(rows[0].keys()), ["email", "name", "company"])

    def test_truncation_is_reported_not_silent(self):
        for n in range(3):
            self._lead(name=f"P{n}", email=f"p{n}@acme.com", company="Acme")
        result, _rows = self._csv(fields=["email"], limit=2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])

    def test_the_legacy_export_shim_still_works(self):
        import pipeline_tags

        self._lead(name="Jane Doe", email="jane@acme.com", company="Acme Corp")
        legacy = pipeline_tags.export_leads(workspace="wsa", fmt="json")
        self.assertEqual(legacy["count"], 1)
        routed = pipeline_tags.export_leads(workspace="wsa", preset="sequencer-upload")
        self.assertEqual(routed["count"], 1)
        self.assertIn("email", routed["columns"])


if __name__ == "__main__":
    unittest.main()
