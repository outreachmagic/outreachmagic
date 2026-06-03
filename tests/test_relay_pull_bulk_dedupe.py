#!/usr/bin/env python3
"""Bulk relay_ingested prefetch for pull pages."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from relay_ingest import (  # noqa: E402
    mark_relay_ingested,
    mark_relay_ingested_many,
    prefetch_relay_ingested,
    relay_dedupe_key,
)


class RelayPullBulkDedupeTests(unittest.TestCase):
    def setUp(self):
        om.init_db()

    def test_prefetch_relay_ingested_batch(self):
        mark_relay_ingested("relay:1", None)
        mark_relay_ingested("relay:2", None)
        found = prefetch_relay_ingested(["relay:1", "relay:99", "relay:2", "relay:1"])
        self.assertEqual(found, {"relay:1", "relay:2"})

    def test_mark_relay_ingested_many_single_commit(self):
        mark_relay_ingested_many([("relay:a", None), ("relay:b", None), ("relay:a", None)])
        self.assertEqual(prefetch_relay_ingested(["relay:a", "relay:b", "relay:c"]), {"relay:a", "relay:b"})

    def test_ingest_page_skips_duplicate_without_ingest(self):
        events = [
            {"platform": "agent", "relay_id": 2_000_000_001, "action": "lead_core_update",
             "client_id": "other", "entity_key": "a@b.com", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"email": "a@b.com"}},
            {"platform": "smartlead", "relay_id": 2_000_000_088, "event_type": "email_sent",
             "lead": "b@b.com", "received_at": "2026-01-01T00:00:01Z", "raw": {}},
        ]
        mark_relay_ingested(relay_dedupe_key(events[0]), None)
        mark_relay_ingested(relay_dedupe_key(events[1]), None)

        ingest_calls = []

        def fake_ingest(*_args, **_kwargs):
            ingest_calls.append(1)
            return 1

        with mock.patch.object(om, "ingest_relay_event", side_effect=fake_ingest):
            stats = om._ingest_relay_page(events, quiet=True)

        self.assertEqual(stats["skipped_duplicates"], 2)
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(len(ingest_calls), 0)


if __name__ == "__main__":
    unittest.main()
