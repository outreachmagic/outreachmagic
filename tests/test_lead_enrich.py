#!/usr/bin/env python3
"""Tests for skills/lead-enrich/scripts/enrich.py"""

import json
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


class TestSerperSearch(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            enrich.serper_search("test", {"serper_endpoint": "https://example.com"})


if __name__ == "__main__":
    unittest.main()
