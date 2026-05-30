#!/usr/bin/env python3
"""Tests for skills/lead-enrich/scripts/enrich.py"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "lead-enrich" / "scripts"))

import enrich  # noqa: E402


class TestCompanyMatch(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(enrich.companies_match("Acme Corp", "Acme Corp"))

    def test_suffix_variants(self):
        self.assertTrue(enrich.companies_match("Acme Inc", "Acme Corporation"))

    def test_substring(self):
        self.assertTrue(enrich.companies_match("Acme", "Acme International"))

    def test_mismatch(self):
        self.assertFalse(enrich.companies_match("Acme Corp", "Beta Industries"))

    def test_empty_actual_allows_match(self):
        self.assertTrue(enrich.companies_match("Acme", ""))


class TestNormalizeInput(unittest.TestCase):
    def test_batch_tags_flow_down(self):
        data = {
            "people": [{"full_name": "Jane Doe", "company_name": "Acme"}],
            "tags": ["nace"],
            "import_name": "conf-2026",
        }
        people = enrich.normalize_input(data)
        self.assertEqual(people[0]["tags"], ["nace"])
        self.assertEqual(people[0]["import_name"], "conf-2026")


class TestMapToOutreachmagic(unittest.TestCase):
    def test_company_domain_structured(self):
        person = {"full_name": "Jane Doe", "company_name": "Acme"}
        enrichment = {
            "company_domain": "acme.com",
            "company_website": "https://acme.com",
            "linkedin_url": "https://www.linkedin.com/in/janedoe",
            "confidence": "high",
        }
        mapped = enrich.map_to_outreachmagic(person, enrichment)
        profile = mapped["profile"]
        self.assertEqual(profile["company_domain"], "acme.com")
        self.assertIn("linkedin.com/in/janedoe", profile["linkedin"])
        self.assertTrue(mapped["can_import_via_import_profiles"])


class TestCurlRedaction(unittest.TestCase):
    def test_no_raw_key_in_curl(self):
        cfg = {"serper_endpoint": "https://google.serper.dev/search", "serper_num_results": 10}
        cmd = enrich.build_curl_command("test query", cfg)
        self.assertIn("$SERPER_API_KEY", cmd)
        self.assertNotIn("sk-", cmd)


class TestFindOutreachmagic(unittest.TestCase):
    def test_sibling_skills_directory_path(self):
        sibling = enrich._find_skill_dir().parent / "outreachmagic"
        if (sibling / "scripts" / "pipeline.py").exists():
            found = enrich.find_outreachmagic({})
            self.assertIsNotNone(found)
            self.assertTrue(str(found).endswith("outreachmagic"))


class TestCheckLeadStatus(unittest.TestCase):
    def test_force_skips_dedup(self):
        om = enrich.find_outreachmagic({})
        if not om:
            self.skipTest("outreachmagic not present")
        result = enrich.check_lead_exists(
            om, "Definitely Not A Real Person 99999", "Fake Co", force=True
        )
        self.assertEqual(result["status"], "not_found")
        self.assertTrue(result.get("force"))

    @patch.object(enrich, "_history_lookup")
    def test_ambiguous_on_company_mismatch(self, mock_lookup):
        om = Path("/tmp/om")
        mock_lookup.return_value = {
            "id": 1,
            "name": "Jane Doe",
            "company_display": "Beta Inc",
            "email": None,
            "linkedin_url": "linkedin.com/in/jane",
        }
        result = enrich.check_lead_exists(om, "Jane Doe", "Acme Corp")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["lead_id"])
        self.assertEqual(result["ambiguous_lead_id"], 1)

    def test_apply_lead_match_email_aware_statuses(self):
        result: dict = {}
        enrich._apply_lead_match(
            result,
            {
                "id": 1,
                "name": "Jane",
                "company_display": "Acme",
                "email": "j@acme.com",
                "linkedin_url": "linkedin.com/in/jane",
            },
            input_company="Acme",
            force=False,
        )
        self.assertEqual(result["status"], "exists_linkedin_email")

        result = {}
        enrich._apply_lead_match(
            result,
            {
                "id": 2,
                "name": "Bob",
                "company_display": "Acme",
                "email": None,
                "linkedin_url": "linkedin.com/in/bob",
            },
            input_company="Acme",
            force=False,
        )
        self.assertEqual(result["status"], "exists_linkedin_no_email")

        result = {}
        enrich._apply_lead_match(
            result,
            {
                "id": 3,
                "name": "Pat",
                "company_display": "Acme",
                "email": "p@acme.com",
                "linkedin_url": None,
            },
            input_company="Acme",
            force=False,
        )
        self.assertEqual(result["status"], "exists_no_linkedin_email")


class TestSerperSearch(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            enrich.serper_search("test", {"serper_endpoint": "https://example.com"})


class TestHermesEnv(unittest.TestCase):
    def setUp(self):
        enrich._HERMES_ENV_LOADED = False
        self._saved = {
            k: os.environ[k]
            for k in ("SERPER_API_KEY", "OUTREACHMAGIC_AGENT_KEY", "HERMES_HOME")
            if k in os.environ
        }
        for k in self._saved:
            del os.environ[k]

    def tearDown(self):
        enrich._HERMES_ENV_LOADED = False
        for k in ("SERPER_API_KEY", "OUTREACHMAGIC_AGENT_KEY", "HERMES_HOME"):
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_parse_dotenv_line(self):
        self.assertEqual(
            enrich._parse_dotenv_line('export SERPER_API_KEY="abc123"'),
            ("SERPER_API_KEY", "abc123"),
        )
        self.assertIsNone(enrich._parse_dotenv_line("# comment"))

    def test_loads_serper_from_hermes_env_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text(
                "SERPER_API_KEY=from-hermes-env\n"
                "OUTREACHMAGIC_AGENT_KEY=om_agent_test\n"
            )
            os.environ["HERMES_HOME"] = str(home)
            enrich._HERMES_ENV_LOADED = False
            enrich.ensure_hermes_env_loaded()
            self.assertEqual(os.environ.get("SERPER_API_KEY"), "from-hermes-env")
            self.assertEqual(
                os.environ.get("OUTREACHMAGIC_AGENT_KEY"), "om_agent_test"
            )
            cfg = enrich.load_config()
            self.assertEqual(cfg["serper_api_key"], "from-hermes-env")

    def test_does_not_override_existing_shell_env(self):
        import tempfile

        os.environ["SERPER_API_KEY"] = "shell-wins"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("SERPER_API_KEY=from-file\n")
            os.environ["HERMES_HOME"] = str(home)
            enrich._HERMES_ENV_LOADED = False
            enrich.ensure_hermes_env_loaded()
            self.assertEqual(os.environ["SERPER_API_KEY"], "shell-wins")


class TestTeamAndBackfill(unittest.TestCase):
    def test_team_entry(self):
        self.assertTrue(enrich.is_team_entry("Walter Center Team", "IU"))
        self.assertFalse(enrich.is_team_entry("Jane Doe", "Acme"))

    def test_build_backfill_profile(self):
        row = {
            "linkedin": "https://linkedin.com/in/jane",
            "title": "VP Sales",
            "industry": "SaaS",
        }
        profile = enrich.build_backfill_profile(row, frozenset({"title", "industry"}))
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile["title"], "VP Sales")
        self.assertEqual(profile["industry"], "SaaS")
        self.assertIn("linkedin.com/in/jane", profile["linkedin"])

    def test_load_people_file_csv(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("full_name,company_name\nJane,Acme\n")
            path = f.name
        try:
            data = enrich.load_people_file(path)
            self.assertEqual(len(data["people"]), 1)
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
