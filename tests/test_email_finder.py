#!/usr/bin/env python3
"""Tests for skills/email-finder/scripts/email_finder.py"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
EMAIL_PY = ROOT / "skills" / "email-finder" / "scripts" / "email_finder.py"
sys.path.insert(0, str(EMAIL_PY.parent))

spec = importlib.util.spec_from_file_location("email_finder_script", EMAIL_PY)
lemail = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lemail)


class TestValidityNotes(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(lemail._validity_note_text("valid", found=True), "trykitt verify: valid")

    def test_catch_all(self):
        self.assertEqual(lemail._validity_note_text("valid-risky", found=True), "trykitt verify: catch_all")

    def test_not_found(self):
        self.assertEqual(lemail._validity_note_text("", found=False), "trykitt: no email found")


class TestBuildImportProfile(unittest.TestCase):
    def test_found_tags(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={"email": "j@acme.com", "validity": "valid"},
        )
        self.assertEqual(profile["tags"], ["trykitt_attempted", "email_found"])
        self.assertEqual(profile["notes"], "trykitt verify: valid")

    def test_miss_tags(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={},
        )
        self.assertEqual(profile["tags"], ["trykitt_attempted"])
        self.assertNotIn("email", profile)

    def test_icypeas_found_tags_and_notes(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={"email": "j@acme.com", "validity": "ultra_sure", "provider": "icypeas"},
        )
        self.assertEqual(profile["tags"], ["icypeas_attempted", "email_found"])
        self.assertEqual(profile["notes"], "icypeas certainty: ultra_sure")

    def test_fallback_attempt_tags(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={
                "provider": "icypeas",
                "provider_attempts": [
                    {"provider": "trykitt", "status": "not_found"},
                    {"provider": "icypeas", "status": "found"},
                ],
                "email": "j@acme.com",
            },
        )
        self.assertEqual(profile["tags"], ["trykitt_attempted", "icypeas_attempted", "email_found"])


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
        lemail.cc._AGENT_ENV_LOADED = False
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
            lemail.cc._AGENT_ENV_LOADED = False
            cfg = lemail.load_config()
        result = lemail.trykitt_find(cfg, full_name="Jane", domain="acme.com")
        self.assertEqual(result["status"], "no_key")


class TestIcypeasFind(unittest.TestCase):
    @patch.dict("os.environ", {"ICYPEAS_API_KEY": "test_icypeas"})
    def test_no_key_in_config_still_works(self):
        lemail.cc._AGENT_ENV_LOADED = False
        cfg = lemail.load_config()
        with patch.object(lemail, "_icypeas_poll_result", return_value={"status": "not_found"}) as mock_poll:
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps({"item": {"_id": "abc123"}}).encode()
                mock_resp.__enter__ = lambda s: mock_resp
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp
                result = lemail.icypeas_find(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["status"], "not_found")
        mock_poll.assert_called_once()

    def test_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            lemail.cc._AGENT_ENV_LOADED = False
            cfg = lemail.load_config()
        result = lemail.icypeas_find(cfg, full_name="Jane", domain="acme.com")
        self.assertEqual(result["status"], "no_key")

    @patch.dict("os.environ", {"ICYPEAS_API_KEY": "test_icypeas"})
    def test_polling_reads_email(self):
        lemail.cc._AGENT_ENV_LOADED = False
        cfg = lemail.load_config()
        cfg["icypeas_poll_attempts"] = 2
        cfg["icypeas_poll_delay_seconds"] = 0
        payload_in_progress = {"items": [{"status": "IN_PROGRESS", "results": {"emails": []}}]}
        payload_done = {
            "items": [{
                "status": "DEBITED",
                "results": {"emails": [{"email": "jane@acme.com", "certainty": "ultra_sure"}]},
            }]
        }
        with patch("urllib.request.urlopen") as mock_urlopen:
            first = MagicMock()
            first.read.return_value = json.dumps(payload_in_progress).encode()
            first.__enter__ = lambda s: first
            first.__exit__ = MagicMock(return_value=False)
            second = MagicMock()
            second.read.return_value = json.dumps(payload_done).encode()
            second.__enter__ = lambda s: second
            second.__exit__ = MagicMock(return_value=False)
            mock_urlopen.side_effect = [first, second]
            result = lemail._icypeas_poll_result(cfg, "abc123", domain="acme.com", full_name="Jane Doe")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["email"], "jane@acme.com")
        self.assertEqual(result["provider"], "icypeas")

    @patch.dict("os.environ", {"ICYPEAS_API_KEY": "test_icypeas"})
    def test_init_success_false_returns_error(self):
        lemail.cc._AGENT_ENV_LOADED = False
        cfg = lemail.load_config()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"success": False, "message": "bad request"}).encode()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = lemail.icypeas_find(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["status"], "error")
        self.assertIn("bad request", result["error"])


class TestFallbackOrder(unittest.TestCase):
    @patch.object(lemail, "icypeas_find")
    @patch.object(lemail, "trykitt_find")
    def test_trykitt_first_then_icypeas(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.return_value = {"status": "not_found", "provider": "trykitt"}
        mock_icypeas.return_value = {"status": "found", "email": "jane@acme.com", "provider": "icypeas"}
        result = lemail.run_find_with_fallback(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["provider"], "icypeas")
        self.assertEqual(result["provider_attempts"][0]["provider"], "trykitt")
        self.assertEqual(result["provider_attempts"][1]["provider"], "icypeas")
        mock_trykitt.assert_called_once()
        mock_icypeas.assert_called_once()

    @patch.object(lemail, "icypeas_find")
    @patch.object(lemail, "trykitt_find")
    def test_icypeas_only_when_trykitt_disabled(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": False, "icypeas_enabled": True}
        mock_icypeas.return_value = {"status": "found", "email": "jane@acme.com", "provider": "icypeas"}
        result = lemail.run_find_with_fallback(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["provider"], "icypeas")
        mock_trykitt.assert_not_called()
        mock_icypeas.assert_called_once()

    @patch.object(lemail, "icypeas_find")
    @patch.object(lemail, "trykitt_find")
    def test_attempted_flag_false_for_no_key(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.return_value = {"status": "not_found", "provider": "trykitt"}
        mock_icypeas.return_value = {"status": "no_key", "provider": "icypeas", "error": "ICYPEAS_API_KEY not set"}
        result = lemail.run_find_with_fallback(cfg, full_name="Jane Doe", domain="acme.com")
        attempts = result["provider_attempts"]
        self.assertTrue(attempts[0]["attempted"])
        self.assertFalse(attempts[1]["attempted"])


class TestBatchCollectThenSave(unittest.TestCase):
    @patch.object(lemail, "trykitt_find")
    @patch.object(lemail, "check_existing_email")
    @patch.object(lemail.cc, "run_import_profiles")
    def test_single_import_at_end(self, mock_import, mock_check, mock_find):
        mock_check.return_value = {"email": None}
        mock_find.side_effect = [
            {"status": "found", "email": "a@acme.com", "validity": "valid"},
            {"status": "not_found"},
        ]
        mock_import.return_value = {"matched": 2}
        om = Path("/tmp/om")
        with patch.object(lemail, "find_outreachmagic", return_value=om), patch.object(
            lemail, "load_config", return_value={"max_people_per_run": 50}
        ):
            tmp = Path("/tmp/test_batch.json")
            tmp.write_text(json.dumps([
                {"name": "A", "domain": "acme.com"},
                {"name": "B", "domain": "acme.com"},
            ]))
            with patch("sys.stdout") as mock_stdout:
                lemail.cmd_batch_find(str(tmp), "ws1", delay=0)
        mock_import.assert_called_once()
        profiles = mock_import.call_args[0][1]
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["tags"], ["trykitt_attempted", "email_found"])


class TestTagOnMiss(unittest.TestCase):
    @patch.object(lemail.cc, "run_import_profiles")
    def test_tag_trykitt_attempted_on_miss(self, mock_import):
        mock_import.return_value = {"matched": 1, "lead_id": 42}
        om = Path("/tmp/om")
        out = lemail.tag_provider_attempt(
            om,
            full_name="Jane Doe",
            company="Acme",
            domain="acme.com",
            linkedin="janedoe",
            workspace="ws1",
            provider="trykitt",
        )
        self.assertTrue(out["tagged"])
        mock_import.assert_called_once()
        profiles = mock_import.call_args[0][1]
        self.assertEqual(profiles[0]["tags"], ["trykitt_attempted"])
        self.assertNotIn("email", profiles[0])

    @patch.object(lemail.cc, "run_import_profiles")
    def test_tag_icypeas_attempted_on_miss(self, mock_import):
        mock_import.return_value = {"matched": 1, "lead_id": 42}
        om = Path("/tmp/om")
        out = lemail.tag_provider_attempt(
            om,
            full_name="Jane Doe",
            company="Acme",
            domain="acme.com",
            linkedin="janedoe",
            workspace="ws1",
            provider="icypeas",
        )
        self.assertTrue(out["tagged"])
        profiles = mock_import.call_args[0][1]
        self.assertEqual(profiles[0]["tags"], ["icypeas_attempted"])


class TestSaveMissTagging(unittest.TestCase):
    @patch.object(lemail, "batch_import_results")
    @patch.object(lemail, "run_find_with_fallback")
    @patch.object(lemail, "find_outreachmagic")
    @patch.object(lemail, "load_config")
    @patch.object(lemail, "check_existing_email")
    def test_cmd_find_save_miss_imports_single_combined_profile(
        self, mock_check, mock_load, mock_find_om, mock_run_fallback, mock_batch_import
    ):
        mock_check.return_value = {"email": None}
        mock_load.return_value = {}
        mock_find_om.return_value = Path("/tmp/om")
        mock_run_fallback.return_value = {
            "status": "not_found",
            "provider": "icypeas",
            "provider_attempts": [
                {"provider": "trykitt", "status": "not_found", "attempted": True},
                {"provider": "icypeas", "status": "not_found", "attempted": True},
            ],
        }
        mock_batch_import.return_value = {"imported": 1, "import": {"matched": 1}}

        with patch("sys.stdout"):
            lemail.cmd_find(
                "Jane Doe",
                "acme.com",
                linkedin="",
                workspace="ws1",
                save=True,
                company="Acme",
            )

        mock_batch_import.assert_called_once()
        profiles = mock_batch_import.call_args[0][1]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["tags"], ["trykitt_attempted", "icypeas_attempted"])


if __name__ == "__main__":
    unittest.main()
