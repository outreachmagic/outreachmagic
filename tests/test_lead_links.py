#!/usr/bin/env python3
"""One conversation, two people, one count.

A contact replies positively and forwards us to a colleague, who replies too.
That is one opportunity. Merging the two is wrong -- both are real people with
their own address, title and history -- but the Campaigns page counting two
positives is also wrong, and the count is what reaches every "how did we do"
conversation.

The link is non-destructive and directional. Reporting resolves each lead to its
conversation ROOT and counts distinct roots, so a lead in no conversation counts
exactly as it did before.
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

import json  # noqa: E402

import lead_links  # noqa: E402
import reporting_queries as dq  # noqa: E402
import sync_contract  # noqa: E402
import pipeline as om  # noqa: E402

WS = "acme-ws"


class LeadLinkTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        self.conn = om.get_conn()
        om.ensure_organization(self.conn)
        self.conn.execute(
            "INSERT INTO workspaces (id, org_id, slug, name) VALUES (?, ?, ?, ?)",
            (WS, "default", WS, "Acme"))
        self.company_id = self.conn.execute(
            "INSERT INTO companies (name, domain) VALUES (?, ?)",
            ("Acme", "acme.test")).lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _lead(self, name, *, sentiment=None, at="2026-01-05T10:00:00"):
        lead_id = self.conn.execute(
            "INSERT INTO leads (name, company_id, uid) VALUES (?, ?, ?)",
            (name, self.company_id, f"uid_{name.lower().replace(' ', '_')}")).lastrowid
        self.conn.execute(
            """INSERT INTO workspace_leads
                   (workspace_id, org_id, lead_id, status,
                    current_status_sentiment, current_sentiment_since)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (WS, "default", lead_id, "replied", sentiment, at if sentiment else None))
        self.conn.commit()
        return lead_id

    def _campaign_send(self, lead_id, campaign_name, at="2026-01-02T09:00:00"):
        campaign_id = self.conn.execute(
            "INSERT OR IGNORE INTO campaigns (name) VALUES (?)", (campaign_name,)).lastrowid
        if not campaign_id:
            campaign_id = self.conn.execute(
                "SELECT id FROM campaigns WHERE name = ?", (campaign_name,)).fetchone()["id"]
        event_id = self.conn.execute(
            """INSERT INTO events (lead_id, event_type, direction, campaign_id,
                                   metadata_json, created_at)
               VALUES (?, 'email_sent', 'outbound', ?, ?, ?)""",
            (lead_id, campaign_id, json.dumps({"platform": "plusvibe"}), at)).lastrowid
        self.conn.execute(
            """INSERT INTO workspace_lead_events
                   (workspace_id, org_id, lead_id, event_id, event_type, event_at,
                    idempotency_key)
               VALUES (?, 'default', ?, ?, 'email_sent', ?, ?)""",
            (WS, lead_id, event_id, at, f"{WS}:{event_id}"))
        self.conn.execute(
            "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id) VALUES (?, ?)",
            (campaign_id, lead_id))
        self.conn.commit()
        return campaign_id

    # -- the core rule -------------------------------------------------------

    def test_two_linked_positives_count_as_one(self):
        alice = self._lead("Alice Origin", sentiment="positive")
        bob = self._lead("Bob Forwarded", sentiment="positive")
        before = dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None)
        self.assertEqual(before, 2)

        lead_links.link_leads(self.conn, alice, bob, note="forwarded to facilities")
        after = dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None)
        self.assertEqual(after, 1, "one conversation is one positive")

    def test_unlinked_leads_are_counted_exactly_as_before(self):
        """The join must be a no-op until somebody records a link."""
        for i in range(4):
            self._lead(f"Person {i}", sentiment="positive")
        self.assertEqual(
            dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None), 4)

    def test_a_chain_of_three_counts_once(self):
        a = self._lead("A Origin", sentiment="positive")
        b = self._lead("B Middle", sentiment="positive")
        c = self._lead("C End", sentiment="positive")
        lead_links.link_leads(self.conn, a, b)
        lead_links.link_leads(self.conn, b, c)
        self.assertEqual(lead_links.conversation_root(self.conn, c), a)
        self.assertEqual(
            dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None), 1)

    def test_both_records_survive_the_link(self):
        """The whole reason this is not a merge."""
        alice = self._lead("Alice Origin", sentiment="positive")
        bob = self._lead("Bob Forwarded", sentiment="positive")
        lead_links.link_leads(self.conn, alice, bob)
        names = [r["name"] for r in self.conn.execute(
            "SELECT name FROM leads ORDER BY id").fetchall()]
        self.assertEqual(names, ["Alice Origin", "Bob Forwarded"])

    # -- campaign attribution ------------------------------------------------

    def test_the_conversation_is_credited_to_the_campaign_that_started_it(self):
        alice = self._lead("Alice Origin", sentiment="positive")
        bob = self._lead("Bob Forwarded", sentiment="positive")
        self._campaign_send(alice, "acme | field marketing")
        lead_links.link_leads(self.conn, alice, bob)

        rows = {c["name"]: c for c in
                dq.campaign_audit(self.conn, WS, since="all")["campaigns"]}
        self.assertEqual(rows["acme | field marketing"]["positive"], 1)
        self.assertEqual(
            sum(c["positive"] for c in
                dq.campaign_audit(self.conn, WS, since="all")["campaigns"]), 1)

    # -- guards --------------------------------------------------------------

    def test_a_lead_cannot_be_linked_to_itself(self):
        alice = self._lead("Alice Origin")
        with self.assertRaises(lead_links.LeadLinkError):
            lead_links.link_leads(self.conn, alice, alice)

    def test_a_cycle_is_refused(self):
        """A conversation has one origin; a loop has none."""
        a, b = self._lead("A"), self._lead("B")
        lead_links.link_leads(self.conn, a, b)
        with self.assertRaises(lead_links.LeadLinkError):
            lead_links.link_leads(self.conn, b, a)

    def test_an_unknown_lead_is_refused(self):
        alice = self._lead("Alice Origin")
        with self.assertRaises(lead_links.LeadLinkError):
            lead_links.link_leads(self.conn, alice, 999999)

    def test_linking_is_idempotent(self):
        a, b = self._lead("A"), self._lead("B")
        first = lead_links.link_leads(self.conn, a, b)
        second = lead_links.link_leads(self.conn, a, b)
        self.assertEqual(first["status"], "linked")
        self.assertEqual(second["status"], "exists")

    def test_unlinking_restores_the_separate_counts(self):
        alice = self._lead("Alice Origin", sentiment="positive")
        bob = self._lead("Bob Forwarded", sentiment="positive")
        lead_links.link_leads(self.conn, alice, bob)
        self.assertEqual(
            dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None), 1)
        lead_links.unlink_leads(self.conn, alice, bob)
        self.assertEqual(
            dq._sentiment_count_in_range(self.conn, WS, "positive", "all", None), 2)

    def test_a_forward_may_cross_companies(self):
        """The introductions worth most are often to another organisation."""
        other_company = self.conn.execute(
            "INSERT INTO companies (name, domain) VALUES (?, ?)",
            ("Other Co", "other.test")).lastrowid
        alice = self._lead("Alice Origin", sentiment="positive")
        outsider = self.conn.execute(
            "INSERT INTO leads (name, company_id, uid) VALUES (?, ?, ?)",
            ("Consultant", other_company, "uid_consultant")).lastrowid
        self.conn.commit()
        result = lead_links.link_leads(self.conn, alice, outsider)
        self.assertEqual(result["status"], "linked")

    # -- sync ----------------------------------------------------------------

    def test_a_link_is_a_first_class_synced_entity(self):
        self.assertIn("lead_link", sync_contract.ENTITY_TYPES)
        self.assertEqual(sync_contract.entity_type_for("lead_links"), "lead_link")
        self.assertEqual(sync_contract.OWNER_TABLES["lead_links"], "lead_link")

    def test_creating_a_link_queues_it_for_push(self):
        """The outbox trigger comes from SYNC_MAP, so this is really asking
        whether the table was wired in properly rather than bolted on."""
        a, b = self._lead("A"), self._lead("B")
        lead_links.link_leads(self.conn, a, b)
        queued = self.conn.execute(
            "SELECT entity_type, op FROM outbox WHERE entity_type = 'lead_link'").fetchall()
        self.assertTrue(queued, "a new link must be queued for the relay")
        self.assertEqual(queued[0]["op"], "upsert")

    def test_the_payload_travels_by_uid_not_local_row_id(self):
        a, b = self._lead("A"), self._lead("B")
        link = lead_links.link_leads(self.conn, a, b, note="intro")
        payload = lead_links.build_lead_link_sync_payload(self.conn, link["id"])
        self.assertEqual(payload["from_uid"], "uid_a")
        self.assertEqual(payload["to_uid"], "uid_b")
        self.assertEqual(payload["kind"], "forwarded_to")
        self.assertEqual(payload["note"], "intro")
        # Nulls never reach the wire.
        self.assertTrue(all(v is not None for v in payload.values()))

    def test_the_sync_panel_can_describe_a_queued_link(self):
        a, b = self._lead("A"), self._lead("B")
        link = lead_links.link_leads(self.conn, a, b)
        record, payload, error = dq._resolve_outbox_entity(
            self.conn, "lead_link", link["id"], None)
        self.assertIsNone(error, error)
        self.assertIsNotNone(record)
        self.assertEqual(payload["kind"], "forwarded_to")

    def test_links_can_push_and_can_tombstone(self):
        """The relay gained a lead_link endpoint (wbhk-worker migration 0015).

        A link that exists on one machine only makes that machine disagree with
        every other about how many positive replies there were -- which is the
        whole thing the link is for, so it has to travel.
        """
        from pipeline_workspace import _SNAPSHOT_DELETE_ACTIONS, _delete_wire_entity_key

        self.assertNotIn("lead_link", dq.UNPUSHABLE_ENTITY_TYPES)
        self.assertEqual(_SNAPSHOT_DELETE_ACTIONS["lead_link"], "lead_link_delete")
        # Keyed by its own id, like sender_domain and unlike leads: no uid.
        self.assertEqual(
            _delete_wire_entity_key("lead_link", "lnk_abc"), "lead_link:lnk_abc")

    def test_an_unpushable_entity_would_still_be_declared(self):
        """The declaration is empty, not deleted: the next entity added to
        SYNC_MAP before the relay can receive it needs somewhere to say so."""
        self.assertEqual(dq.UNPUSHABLE_ENTITY_TYPES, {})


if __name__ == "__main__":
    unittest.main()
