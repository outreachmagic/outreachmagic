"""Companion skills must ship identical companion_common.py."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = ROOT / "skills" / "email-finder" / "scripts" / "companion_common.py"
LEAD = ROOT / "skills" / "lead-enrich" / "scripts" / "companion_common.py"

sys.path.insert(0, str(EMAIL.parent))
import companion_common as cc  # noqa: E402


class CompanionCommonSyncTests(unittest.TestCase):
    def test_copies_are_byte_identical(self):
        self.assertTrue(EMAIL.is_file())
        self.assertTrue(LEAD.is_file())
        self.assertEqual(EMAIL.read_bytes(), LEAD.read_bytes())

    def test_chunk_timeout_200_leads(self):
        self.assertEqual(cc._chunk_timeout(200), 160)

    def test_chunk_timeout_cap(self):
        self.assertEqual(cc._chunk_timeout(1000), 300)

    def test_per_item_constant(self):
        self.assertEqual(cc.CHUNK_TIMEOUT_PER_ITEM_S, 0.8)

    def test_pooled_api_key_var_includes_backup_slots(self):
        self.assertTrue(cc._is_pooled_api_key_var("SERPER_API_KEY__1"))
        self.assertFalse(cc._is_pooled_api_key_var("OTHER_KEY__1"))


if __name__ == "__main__":
    unittest.main()
