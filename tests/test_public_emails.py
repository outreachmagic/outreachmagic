#!/usr/bin/env python3
"""Public mailboxes are records, not a second address on somebody's contact.

We often cannot find Bill Smith's address but can find hello@acme.com. Writing
to hello@ asking for Bill beats writing to nobody. The question is where to put
hello@ without corrupting the record for the real Bill Smith.

Putting it on Bill (or on his lead_emails) loses, both times for the same
reason: dedup matches on email through lead_identities, so the second contact
given the same fallback merges into the first and two real people silently
become one lead. So the mailbox is its own row and Bill points at it.

The property these tests exist to pin, and the reason this shape was chosen
over the alternatives: when Bill's real address arrives, leads.email stops
being NULL and the fallback stops being used, with nothing to clean up.
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

import dashboard_queries as dq  # noqa: E402
import pipeline as om  # noqa: E402
import public_emails as pe  # noqa: E402
from constants import RECORD_TYPE_PUBLIC_EMAIL  # noqa: E402


class ClassificationTests(unittest.TestCase):
    def test_generic_local_parts_are_public(self):
        for addr in ("info@acme.test", "hello@acme.test", "careers@acme.test"):
            self.assertEqual(pe.classify_email(addr), "public", addr)

    def test_a_named_person_is_not_a_shared_mailbox(self):
        # bsmith@acme.test found on a company site belongs to one human. Turning
        # it into a shared record is how two people become one lead.
        self.assertEqual(pe.classify_email("bsmith@acme.test"), "personal")

    def test_shared_mailbox_domains_are_never_company_mailboxes(self):
        # info@gmail.com is Google's, not anyone's employer's.
        self.assertEqual(pe.classify_email("info@gmail.com"), "")


class PublicEmailRecordTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.execute(
            "INSERT INTO workspaces (id, org_id, slug, name) VALUES ('ws', 'default', 'ws', 'WS')")
        self.company_id = conn.execute(
            "INSERT INTO companies (name, domain) VALUES ('Acme', 'acme.test')").lastrowid
        conn.commit()
        conn.close()
        self.bill = om.add_lead(name="Bill Smith", company="Acme")["id"]
        conn = om.get_conn()
        conn.execute("UPDATE leads SET company_id = ?, email = NULL WHERE id = ?",
                     (self.company_id, self.bill))
        conn.commit()
        conn.close()

    def _mailbox(self, email="hello@acme.test"):
        return pe.create_public_email(
            email, company_id=self.company_id, title="Website contact form")["lead_id"]

    def test_creating_one_is_idempotent(self):
        first = self._mailbox()
        again = pe.create_public_email("hello@acme.test", company_id=self.company_id)
        self.assertEqual(again["status"], "exists")
        self.assertEqual(again["lead_id"], first)

    def test_a_personal_address_is_refused(self):
        with self.assertRaises(pe.PublicEmailError):
            pe.create_public_email("bsmith@acme.test", company_id=self.company_id)

    def test_it_is_not_a_contact(self):
        mailbox = self._mailbox()
        conn = om.get_conn()
        try:
            rt = conn.execute("SELECT record_type FROM leads WHERE id = ?",
                              (mailbox,)).fetchone()["record_type"]
        finally:
            conn.close()
        self.assertEqual(rt, RECORD_TYPE_PUBLIC_EMAIL)

    def test_it_is_absent_from_the_contacts_list_by_default(self):
        # This is the whole leanness argument: lead_filter_clause already
        # defaults to record_type='contact', so no new exclusion had to be
        # written for contacts, exports, CRM sync or the email finder.
        mailbox = self._mailbox()
        conn = om.get_conn()
        try:
            conn.execute(
                "INSERT INTO workspace_leads (workspace_id, org_id, lead_id, status) "
                "VALUES ('ws', 'default', ?, 'prospecting')", (mailbox,))
            conn.execute(
                "INSERT INTO workspace_leads (workspace_id, org_id, lead_id, status) "
                "VALUES ('ws', 'default', ?, 'prospecting')", (self.bill,))
            conn.commit()
            default_ids = [r["lead_id"] for r in conn.execute(
                "SELECT lead_id FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id "
                "WHERE " + dq.lead_filter_clause("ws")[0], dq.lead_filter_clause("ws")[1])]
            asked_for = [r["lead_id"] for r in conn.execute(
                "SELECT lead_id FROM workspace_leads wl JOIN leads l ON l.id = wl.lead_id "
                "WHERE " + dq.lead_filter_clause("ws", record_type="public_email")[0],
                dq.lead_filter_clause("ws", record_type="public_email")[1])]
        finally:
            conn.close()
        self.assertIn(self.bill, default_ids)
        self.assertNotIn(mailbox, default_ids)
        self.assertEqual(asked_for, [mailbox])


class FallbackTests(PublicEmailRecordTests):
    def test_the_fallback_supplies_an_address_and_then_gets_out_of_the_way(self):
        mailbox = self._mailbox()
        pe.link_fallback(self.bill, mailbox)
        conn = om.get_conn()
        try:
            before = pe.effective_email(conn, self.bill)
            self.assertEqual(before["effective_email"], "hello@acme.test")
            self.assertTrue(before["is_fallback"])

            # Bill's real address turns up. Nothing is unlinked, no flag is
            # cleared -- the COALESCE simply stops reaching the fallback. That
            # is the property the other two designs could not give us.
            conn.execute("UPDATE leads SET email = 'bill@acme.test' WHERE id = ?",
                         (self.bill,))
            conn.commit()
            after = pe.effective_email(conn, self.bill)
        finally:
            conn.close()
        self.assertEqual(after["effective_email"], "bill@acme.test")
        self.assertFalse(after["is_fallback"])

    def test_two_contacts_can_share_one_mailbox_without_merging(self):
        mailbox = self._mailbox()
        jane = om.add_lead(name="Jane Roe", company="Acme")["id"]
        pe.link_fallback(self.bill, mailbox)
        pe.link_fallback(jane, mailbox)
        conn = om.get_conn()
        try:
            # Two distinct people, both reachable at the same mailbox.
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM leads WHERE fallback_email_lead_id = ? ORDER BY name",
                (mailbox,))]
        finally:
            conn.close()
        self.assertEqual(names, ["Bill Smith", "Jane Roe"])

    def test_a_collision_is_reported_not_silently_resolved(self):
        # Mailing hello@ twice with two salutations is bad. Dropping one of the
        # two from the campaign without saying so is worse.
        mailbox = self._mailbox()
        jane = om.add_lead(name="Jane Roe", company="Acme")["id"]
        pe.link_fallback(self.bill, mailbox)
        pe.link_fallback(jane, mailbox)
        conn = om.get_conn()
        try:
            collisions = pe.fallback_collisions(conn)["collisions"]
        finally:
            conn.close()
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["mailbox"], "hello@acme.test")
        self.assertEqual(collisions[0]["leads"], 2)

    def test_a_real_person_cannot_be_used_as_a_fallback(self):
        jane = om.add_lead(name="Jane Roe", email="jane@acme.test")["id"]
        with self.assertRaises(pe.PublicEmailError):
            pe.link_fallback(self.bill, jane)

    def test_a_mailbox_cannot_fall_back_to_a_mailbox(self):
        one = self._mailbox()
        two = self._mailbox("info@acme.test")
        with self.assertRaises(pe.PublicEmailError):
            pe.link_fallback(one, two)


class RoundTripTests(PublicEmailRecordTests):
    """A public mailbox that does not survive a round trip is a contact again.

    The push already sent any non-'contact' record_type; the apply side gated it
    against a hard-coded pair. So a rebuild-from-relay demoted every public
    mailbox back to 'contact' -- straight back into the contacts list, the
    exports and the email finder.
    """

    def test_record_type_survives_the_apply_side(self):
        import lead_sync

        mailbox = self._mailbox()
        conn = om.get_conn()
        try:
            lead_sync.apply_agent_lead_core_payload(
                mailbox, {"record_type": "public_email", "email": "hello@acme.test"},
                conn=conn)
            conn.commit()
            rt = conn.execute("SELECT record_type FROM leads WHERE id = ?",
                              (mailbox,)).fetchone()["record_type"]
        finally:
            conn.close()
        self.assertEqual(rt, RECORD_TYPE_PUBLIC_EMAIL)

    def test_the_allow_list_is_the_shared_vocabulary(self):
        # The bug was a second hard-coded copy of the record-type vocabulary.
        # Assert the apply side accepts everything constants declares, so
        # adding a fourth type cannot reintroduce it.
        import lead_sync
        from constants import LEAD_RECORD_TYPES

        conn = om.get_conn()
        try:
            for rt in LEAD_RECORD_TYPES:
                lead = om.add_lead(name=f"probe {rt}", email=f"{rt}@probe.test")["id"]
                lead_sync.apply_agent_lead_core_payload(lead, {"record_type": rt}, conn=conn)
                conn.commit()
                got = conn.execute("SELECT record_type FROM leads WHERE id = ?",
                                   (lead,)).fetchone()["record_type"]
                self.assertEqual(got, rt)
        finally:
            conn.close()

    def test_the_fallback_link_travels_as_a_uid_not_a_row_id(self):
        import lead_sync
        from workspace_routing import DEFAULT_ORG_ID

        mailbox = self._mailbox()
        pe.link_fallback(self.bill, mailbox)
        conn = om.get_conn()
        try:
            payload = lead_sync.build_lead_core_sync_payload(
                conn, DEFAULT_ORG_ID, self.bill)
            mailbox_uid = conn.execute(
                "SELECT uid FROM leads WHERE id = ?", (mailbox,)).fetchone()["uid"]
        finally:
            conn.close()
        # Local row ids are not addressable from the relay side.
        self.assertNotIn("fallback_email_lead_id", payload)
        self.assertEqual(payload.get("fallback_email_uid"), mailbox_uid)

    def test_an_unresolvable_fallback_uid_is_left_unset_not_stubbed(self):
        # Snapshot ordering is not guaranteed. Minting a placeholder lead to
        # satisfy the FK would put a nameless row in the contacts list to fix a
        # pointer; the next pull can resolve it instead.
        import lead_sync

        conn = om.get_conn()
        try:
            before = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
            lead_sync.apply_agent_lead_core_payload(
                self.bill, {"fallback_email_uid": "uid-that-has-not-arrived"}, conn=conn)
            conn.commit()
            row = conn.execute(
                "SELECT fallback_email_lead_id FROM leads WHERE id = ?",
                (self.bill,)).fetchone()
            after = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        finally:
            conn.close()
        self.assertIsNone(row["fallback_email_lead_id"])
        self.assertEqual(before, after)


class SerperIngestTests(PublicEmailRecordTests):
    def test_only_generic_addresses_on_the_company_domain_become_records(self):
        conn = om.get_conn()
        try:
            result = pe.ingest_serper_emails(conn, self.bill, [
                {"email": "hello@acme.test", "kind": "public",
                 "matches_company_domain": True, "context": "Contact us"},
                {"email": "bsmith@acme.test", "kind": "personal",
                 "matches_company_domain": True, "context": "Team"},
                {"email": "info@unrelated.test", "kind": "public",
                 "matches_company_domain": False, "context": "Other"},
            ])
        finally:
            conn.close()
        self.assertEqual([c["email"] for c in result["created"]], ["hello@acme.test"])
        # The personal address is a lead for the email finder, not a shared
        # mailbox -- surfaced rather than swallowed.
        self.assertIn("bsmith@acme.test", result["personal_addresses_for_review"])
        self.assertIn("info@unrelated.test", result["skipped"])

    def test_ingest_does_not_link_anything_on_its_own(self):
        conn = om.get_conn()
        try:
            pe.ingest_serper_emails(conn, self.bill, [
                {"email": "hello@acme.test", "kind": "public",
                 "matches_company_domain": True, "context": "Contact us"}])
            link = conn.execute(
                "SELECT fallback_email_lead_id FROM leads WHERE id = ?",
                (self.bill,)).fetchone()["fallback_email_lead_id"]
        finally:
            conn.close()
        # "This mailbox exists" is a fact; "reach Bill here" is a judgement.
        self.assertIsNone(link)


if __name__ == "__main__":
    unittest.main()
