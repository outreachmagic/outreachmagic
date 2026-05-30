#!/usr/bin/env python3
"""Tests for skills/lead-email/scripts/email.py"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
EMAIL_PY = ROOT / "skills" / "lead-email" / "scripts" / "lead_email.py"
sys.path.insert(0, str(EMAIL_PY.parent))

spec = importlib.util.spec_from_file_location("lead_email_script", EMAIL_PY)
lemail = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lemail)


class TestVerifyStatus(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(lemail._verify_status_from_validity("valid"), "valid")

    def test_risky(self):
        self.assertEqual(lemail._verify_status_from_validity("valid-risky"), "risky")

    def test_empty(self):
        self.assertIsNone(lemail._verify_status_from_validity(""))


class TestNormalizeLinkedin(unittest.TestCase):
    def test_slug(self):
        self.assertIn("/in/janedoe", lemail._normalize_linkedin("janedoe"))

    def test_full_url(self):
        self.assertTrue(
            lemail._normalize_linkedin("https://linkedin.com/in/janedoe").startswith("https://")
        )


class TestTrykittFind(unittest.TestCase):
    @patch.dict("os.environ", {"TRYKITT_API_KEY": "testkey1234567890123456789012"})
    def test_no_key_in_config_still_works(self):
        cfg = lemail.load_config()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "email": "jane@acme.com",
                "validity": "valid",
            }).encode()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = lemail.trykitt_find(
                cfg, full_name="Jane Doe", domain="acme.com", linkedin="janedoe"
            )
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["email"], "jane@acme.com")

    def test_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            import companion_common as cc
            cc._AGENT_ENV_LOADED = False
            cfg = lemail.load_config()
        result = lemail.trykitt_find(cfg, full_name="Jane", domain="acme.com")
        self.assertEqual(result["status"], "no_key")


if __name__ == "__main__":
    unittest.main()
