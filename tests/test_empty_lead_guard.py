#!/usr/bin/env python3
"""A relay snapshot with nothing in it must not mint a lead.

The lead_workspace_update path calls resolve_lead_from_agent_sync(entity_key,
{}) with an EMPTY payload whenever the entity_key fails to resolve locally, so
every such key produced a name="Unknown" lead with no email, no LinkedIn, no
company and no events. find_lead_by_identifier's own comment already called
this "the junk-lead factory".

A single `pull --full` on 2026-07-13 produced 10,457 of them in 17 minutes --
every uid-keyed relay snapshot whose payload had nothing in it, mostly stale
rows left over from the pre-uid rekey. They are not recoverable data and cannot
be enriched: there is nothing to search on.
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

import lead_sync  # noqa: E402
import pipeline as om  # noqa: E402


class EmptySnapshotGuardTests(unittest.TestCase):
    def setUp(self):
        db_path = om.get_db_path()
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()
        om.init_db()
        conn = om.get_conn()
        om.ensure_organization(conn)
        conn.close()

    def _lead_count(self):
        conn = om.get_conn()
        try:
            return conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        finally:
            conn.close()

    # -- refused ------------------------------------------------------------

    def test_uid_key_with_empty_payload_creates_nothing(self):
        before = self._lead_count()
        result = lead_sync.resolve_lead_from_agent_sync("uid:deadbeefdeadbeef", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "empty_snapshot")
        self.assertEqual(self._lead_count(), before, "no lead may be created")

    def test_uid_key_with_unknown_name_creates_nothing(self):
        """The exact shape of the 10,457: a uid and the literal name 'Unknown'."""
        before = self._lead_count()
        result = lead_sync.resolve_lead_from_agent_sync(
            "uid:deadbeefdeadbeef", {"name": "Unknown"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(self._lead_count(), before)

    def test_blank_entity_key_with_empty_payload_creates_nothing(self):
        before = self._lead_count()
        result = lead_sync.resolve_lead_from_agent_sync("", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(self._lead_count(), before)

    # -- still allowed ------------------------------------------------------

    def test_a_payload_with_an_email_still_creates(self):
        result = lead_sync.resolve_lead_from_agent_sync(
            "uid:deadbeefdeadbeef", {"email": "real@acme.com", "name": "Real Person"})
        self.assertNotEqual(result.get("status"), "error")
        self.assertTrue(result.get("id"))

    def test_name_and_company_is_refused_by_weak_identity_not_by_this_guard(self):
        """Relay ingest already declines name+company alone -- an inbound event
        with no matchable identity is quarantined rather than turned into an
        unmatchable lead. This guard must not be what stops it, or the reason
        reported for a genuinely different problem would be wrong."""
        payload = {"name": "Real Person", "company": "Acme"}
        self.assertTrue(
            lead_sync._payload_can_create_lead(payload, "uid:deadbeefdeadbeef"),
            "the empty-snapshot guard has no quarrel with a real name + company",
        )
        result = lead_sync.resolve_lead_from_agent_sync("uid:deadbeefdeadbeef", payload)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result.get("reason"), "empty_snapshot")
        self.assertTrue(result.get("weak_identity"))

    def test_a_typed_entity_key_is_identity_enough(self):
        """email:/linkedin:/external_id: keys are a legitimate first sighting --
        the key itself carries the identity, unlike uid:."""
        for key in ("email:someone@acme.com", "someone@acme.com",
                    "external_id:abc-123"):
            with self.subTest(key=key):
                self.assertTrue(lead_sync._payload_can_create_lead({}, key))

    def test_uid_key_is_not_identity_enough(self):
        self.assertFalse(lead_sync._payload_can_create_lead({}, "uid:deadbeef"))

    # -- the predicate itself ------------------------------------------------

    def test_predicate_accepts_any_real_profile_field(self):
        for field in ("email", "linkedin", "linkedin_sales_nav_id", "external_id",
                      "company", "title", "company_domain"):
            with self.subTest(field=field):
                self.assertTrue(
                    lead_sync._payload_can_create_lead({field: "x"}, "uid:deadbeef"))

    def test_predicate_ignores_whitespace_and_unknown(self):
        for payload in ({"name": "   "}, {"name": "unknown"}, {"name": "UNKNOWN"},
                        {"email": ""}, {}):
            with self.subTest(payload=payload):
                self.assertFalse(
                    lead_sync._payload_can_create_lead(payload, "uid:deadbeef"))


if __name__ == "__main__":
    unittest.main()
