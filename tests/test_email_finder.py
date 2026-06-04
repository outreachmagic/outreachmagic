#!/usr/bin/env python3
"""Tests for skills/email-finder/scripts/email_finder.py"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
EMAIL_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"
sys.path.insert(0, str(EMAIL_SCRIPTS))

EMAIL_PY = EMAIL_SCRIPTS / "email_finder.py"
spec = importlib.util.spec_from_file_location("email_finder_script", EMAIL_PY)
lemail = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lemail)

import normalize as norm  # noqa: E402
import providers as prov  # noqa: E402


class TestValidityNotes(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            lemail._validity_note_text("valid", found=True),
            "trykitt verify: valid",
        )

    def test_catch_all(self):
        self.assertEqual(
            lemail._validity_note_text("valid-risky", found=True),
            "trykitt verify: catch_all",
        )

    def test_not_found(self):
        self.assertEqual(
            lemail._validity_note_text("", found=False),
            "trykitt: no email found",
        )


class TestRowFields(unittest.TestCase):
    def test_fullname_key(self):
        name, domain, _c, _li, lid = norm.row_fields({"fullName": "Jane Doe", "domain": "acme.com"})
        self.assertEqual(name, "Jane Doe")
        self.assertEqual(domain, "acme.com")
        self.assertIsNone(lid)

    def test_lead_id(self):
        _n, _d, _c, _li, lid = norm.row_fields({"name": "A", "domain": "x.com", "lead_id": "42"})
        self.assertEqual(lid, 42)


class TestBuildImportProfile(unittest.TestCase):
    def test_found_tags(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={"email": "j@acme.com", "validity": "valid"},
            normalize_linkedin_fn=lemail._normalize_linkedin,
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
            normalize_linkedin_fn=lemail._normalize_linkedin,
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
            normalize_linkedin_fn=lemail._normalize_linkedin,
        )
        self.assertEqual(profile["tags"], ["icypeas_attempted", "email_found"])
        self.assertEqual(profile["notes"], "icypeas certainty: ultra_sure")


class TestNormalizeLinkedin(unittest.TestCase):
    def test_slug(self):
        self.assertIn("/in/janedoe", norm.normalize_linkedin("janedoe"))

    def test_full_url(self):
        self.assertTrue(
            norm.normalize_linkedin("https://linkedin.com/in/janedoe").startswith("https://")
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
                "credits": {"jobCredits": 0.005},
            }).encode()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = prov.trykitt_find(
                cfg, full_name="Jane Doe", domain="acme.com", linkedin="janedoe"
            )
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["email"], "jane@acme.com")

    def test_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            lemail.cc._AGENT_ENV_LOADED = False
            cfg = lemail.load_config()
        result = prov.trykitt_find(cfg, full_name="Jane", domain="acme.com")
        self.assertEqual(result["status"], "no_key")


class TestIcypeasFind(unittest.TestCase):
    @patch.dict("os.environ", {"ICYPEAS_API_KEY": "test_icypeas"})
    def test_no_key_in_config_still_works(self):
        lemail.cc._AGENT_ENV_LOADED = False
        cfg = lemail.load_config()
        with patch.object(prov, "icypeas_poll_result", return_value={"status": "not_found"}) as mock_poll:
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps({"item": {"_id": "abc123"}}).encode()
                mock_resp.__enter__ = lambda s: mock_resp
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp
                result = prov.icypeas_find(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["status"], "not_found")
        mock_poll.assert_called_once()

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
            result = prov.icypeas_poll_result(cfg, "abc123", domain="acme.com", full_name="Jane Doe")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["email"], "jane@acme.com")


class TestValidityMapping(unittest.TestCase):
    def test_icypeas_probable_is_catch_all(self):
        self.assertEqual(prov.validity_to_verify_status("probable", provider="icypeas"), "catch_all")

    def test_trykitt_valid_risky_is_catch_all(self):
        self.assertEqual(prov.validity_to_verify_status("valid-risky", provider="trykitt"), "catch_all")


class TestImportProfileLeadId(unittest.TestCase):
    def test_lead_id_in_profile(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="https://linkedin.com/in/jane",
            find_result={"email": "j@acme.com", "validity": "valid"},
            normalize_linkedin_fn=lemail._normalize_linkedin,
            lead_id=42,
        )
        self.assertEqual(profile["id"], 42)
        self.assertEqual(profile["linkedin"], lemail._normalize_linkedin("https://linkedin.com/in/jane"))


class TestFallbackOrder(unittest.TestCase):
    @patch.object(prov, "icypeas_find")
    @patch.object(prov, "trykitt_find")
    def test_trykitt_first_then_icypeas(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.return_value = {"status": "not_found", "provider": "trykitt"}
        mock_icypeas.return_value = {"status": "found", "email": "jane@acme.com", "provider": "icypeas"}
        result = prov.run_find_with_fallback(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["provider"], "icypeas")
        mock_trykitt.assert_called_once()
        mock_icypeas.assert_called_once()

    @patch.object(prov, "icypeas_find")
    @patch.object(prov, "trykitt_find")
    def test_credit_exhaustion_falls_through(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.side_effect = prov.CreditsExhaustedError("trykitt out of credits")
        mock_icypeas.return_value = {
            "status": "found",
            "email": "jane@acme.com",
            "provider": "icypeas",
        }
        result = prov.run_find_with_fallback(cfg, full_name="Jane Doe", domain="acme.com")
        self.assertEqual(result["provider"], "icypeas")
        mock_icypeas.assert_called_once()

    @patch.object(prov, "icypeas_find")
    @patch.object(prov, "trykitt_find")
    def test_single_provider_flag(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.return_value = {"status": "found", "email": "j@acme.com", "provider": "trykitt"}
        result = prov.run_find_with_fallback(
            cfg, full_name="Jane", domain="acme.com", provider_names=["trykitt"],
        )
        self.assertEqual(result["provider"], "trykitt")
        mock_icypeas.assert_not_called()


class TestBatchRun(unittest.TestCase):
    @patch.object(lemail.cc, "run_verify_email_batch")
    @patch.object(lemail.cc, "run_import_profiles")
    @patch.object(lemail, "run_batch")
    def test_cmd_batch_find_delegates(self, mock_run, mock_import, mock_verify):
        mock_run.return_value = {"count": 1, "stats": {}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump([{"fullName": "A", "domain": "acme.com"}], tmp)
            tmp.flush()
            opts = lemail.BatchOptions(yes=True, skip_om=True)
            with patch("sys.stdout"):
                lemail.cmd_batch_find(tmp.name, opts)
        mock_run.assert_called_once()

    @patch.object(lemail.cc, "run_batch_lead_lookup")
    @patch.object(lemail.cc, "run_import_profiles")
    @patch("batch_runner.run_find_with_fallback")
    def test_batch_single_import(self, mock_find, mock_import, mock_lookup):
        mock_lookup.return_value = {
            "results": [
                {"index": 0, "status": "not_found"},
                {"index": 1, "status": "not_found"},
            ],
        }
        mock_find.side_effect = [
            {"status": "found", "email": "a@acme.com", "validity": "valid", "provider": "trykitt"},
            {"status": "not_found", "provider": "trykitt", "provider_attempts": [
                {"provider": "trykitt", "status": "not_found", "attempted": True},
            ]},
        ]
        mock_import.return_value = {
            "results": [
                {"lead_id": 1},
                {"lead_id": 2},
            ],
        }
        om = Path("/tmp/om")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "batch.json"
            inp.write_text(json.dumps([
                {"name": "A", "domain": "acme.com"},
                {"name": "B", "domain": "acme.com"},
            ]))
            out_base = str(Path(td) / "out")
            opts = lemail.BatchOptions(
                yes=True,
                skip_om=False,
                output_base=out_base,
                workers=1,
                delay=0,
            )
            cfg = {
                "max_people_per_run": 500,
                "trykitt_enabled": True,
                "icypeas_enabled": False,
                "trykitt_api_key": "testkey1234567890123456789012",
            }
            with patch.object(lemail, "find_outreachmagic", return_value=om), patch(
                "batch_runner.run_health_check", return_value=(True, [], []),
            ), patch.object(lemail.cc, "run_verify_email_batch", return_value={"recorded": 1}):
                from batch_runner import run_batch
                result = run_batch(
                    str(inp),
                    cfg,
                    om,
                    opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=lemail._normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
                mock_import.assert_called_once()
                self.assertTrue((Path(td) / "out.csv").exists())
                self.assertEqual(result["stats"]["found"], 1)


class TestTagOnMiss(unittest.TestCase):
    @patch.object(lemail.cc, "run_import_profiles")
    def test_tag_trykitt_attempted_on_miss(self, mock_import):
        mock_import.return_value = {"results": [{"lead_id": 42}]}
        om = Path("/tmp/om")
        out = lemail.tag_provider_attempt(
            om,
            full_name="Jane Doe",
            company="Acme",
            domain="acme.com",
            workspace="ws1",
            provider="trykitt",
        )
        self.assertTrue(out["tagged"])
        profiles = mock_import.call_args[0][1]
        self.assertEqual(profiles[0]["tags"], ["trykitt_attempted"])


if __name__ == "__main__":
    unittest.main()
