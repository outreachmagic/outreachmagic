#!/usr/bin/env python3
"""Tests for skills/outreachmagic/scripts/enrich.py"""

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))

import enrich  # noqa: E402
import shared as cc  # noqa: E402


def _clear_key_pool_session():
    """Reset api_key_pool session tracking to avoid cross-test pollution."""
    api_key_pool_path = ROOT / "skills" / "outreachmagic" / "scripts"
    if str(api_key_pool_path) not in sys.path:
        sys.path.insert(0, str(api_key_pool_path))
    try:
        from api_key_pool import clear_session_state as _clear
        _clear()
    except ImportError:
        pass


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


class TestSerperAttemptedTags(unittest.TestCase):
    def test_append_serper_attempted_idempotent(self):
        self.assertEqual(
            enrich.append_serper_attempted(["nace"]),
            ["nace", enrich.SERPER_ATTEMPTED_TAG],
        )
        self.assertEqual(
            enrich.append_serper_attempted(["nace", enrich.SERPER_ATTEMPTED_TAG]),
            ["nace", enrich.SERPER_ATTEMPTED_TAG],
        )

    def test_skip_reason_has_linkedin(self):
        self.assertEqual(
            enrich.skip_reason_from_tags([], "linkedin.com/in/jane"),
            "has_linkedin",
        )

    def test_skip_reason_serper_attempted(self):
        self.assertEqual(
            enrich.skip_reason_from_tags([enrich.SERPER_ATTEMPTED_TAG], None),
            "skipped_serper_attempted",
        )

    def test_skip_reason_empty_when_needs_enrichment(self):
        self.assertEqual(enrich.skip_reason_from_tags(["nace"], None), "")

    def test_map_to_outreachmagic_always_tags_serper_attempted(self):
        person = {"full_name": "Jane Doe", "company_name": "Acme", "tags": ["nace"]}
        enrichment = {
            "company_domain": "acme.com",
            "linkedin_url": "https://www.linkedin.com/in/janedoe",
            "confidence": "high",
        }
        mapped = enrich.map_to_outreachmagic(person, enrichment)
        self.assertIn(enrich.SERPER_ATTEMPTED_TAG, mapped["profile"]["tags"])
        self.assertIn("nace", mapped["profile"]["tags"])

    def test_build_stamp_profile(self):
        profile = enrich.build_stamp_profile(42, name="Jane", company="Acme")
        self.assertEqual(profile["id"], 42)
        self.assertIn(enrich.SERPER_ATTEMPTED_TAG, profile["tags"])


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

    def test_max_people_override_beats_config(self):
        people = [{"full_name": f"Person {i}", "company_name": "Acme"} for i in range(3)]
        with patch.object(enrich, "load_config", return_value={"max_people_per_run": 2}):
            normalized = enrich.normalize_input({"people": people}, max_people=4)
        self.assertEqual(len(normalized), 3)

    def test_max_people_raises_when_exceeded(self):
        people = [{"full_name": f"Person {i}", "company_name": "Acme"} for i in range(4)]
        with patch.object(enrich, "load_config", return_value={"max_people_per_run": 50}):
            with self.assertRaises(ValueError) as ctx:
                enrich.normalize_input({"people": people}, max_people=2)
        self.assertIn("exceeds limit of 2", str(ctx.exception))


class TestSerperSearchOutFile(unittest.TestCase):
    @patch.object(enrich, "serper_search", return_value={"organic": []})
    @patch.object(enrich, "load_config", return_value={})
    def test_writes_output_file(self, _cfg, _search):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "results.json"
            buf = StringIO()
            err = StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                enrich.cmd_serper_search("Jane Doe Acme", label="lead-1", out_file=str(out_path))
            self.assertTrue(out_path.is_file())
            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["query"], "Jane Doe Acme")
            self.assertEqual(data["label"], "lead-1")
            self.assertIn("Wrote results to", err.getvalue())


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

    @patch.object(enrich, "_single_lead_lookup")
    def test_ambiguous_on_company_mismatch(self, mock_lookup):
        om = Path("/tmp/om")
        mock_lookup.return_value = {
            "id": 1,
            "name": "Jane Doe",
            "company_display": "Beta Inc",
            "email": None,
            "linkedin_url": "linkedin.com/in/jane",
            "tags": [],
        }
        result = enrich.check_lead_exists(om, "Jane Doe", "Acme Corp")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["lead_id"])
        self.assertEqual(result["ambiguous_lead_id"], 1)

    @patch.object(enrich, "_single_lead_lookup")
    def test_skip_tagged_returns_skipped_status(self, mock_lookup):
        om = Path("/tmp/om")
        mock_lookup.return_value = {
            "id": 5,
            "name": "Jane Doe",
            "company_display": "Acme",
            "email": None,
            "linkedin_url": None,
            "tags": [enrich.SERPER_ATTEMPTED_TAG],
        }
        result = enrich.check_lead_exists(
            om, "Jane Doe", "Acme Corp", skip_tagged=True,
        )
        self.assertEqual(result["status"], "skipped_serper_attempted")
        self.assertEqual(result["skip_reason"], "skipped_serper_attempted")

    @patch.object(enrich, "_single_lead_lookup")
    def test_serper_attempted_without_skip_flag_keeps_exists_status(self, mock_lookup):
        om = Path("/tmp/om")
        mock_lookup.return_value = {
            "id": 5,
            "name": "Jane Doe",
            "company_display": "Acme",
            "email": None,
            "linkedin_url": None,
            "tags": [enrich.SERPER_ATTEMPTED_TAG],
        }
        result = enrich.check_lead_exists(om, "Jane Doe", "Acme Corp")
        self.assertEqual(result["status"], "exists_no_linkedin")
        self.assertEqual(result["skip_reason"], "skipped_serper_attempted")

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
                "tags": ["nace"],
            },
            input_company="Acme",
            force=False,
        )
        self.assertEqual(result["status"], "exists_linkedin_email")
        self.assertEqual(result["tags"], ["nace"])

        result = {}
        enrich._apply_lead_match(
            result,
            {
                "id": 2,
                "name": "Bob",
                "company_display": "Acme",
                "email": None,
                "linkedin_url": "linkedin.com/in/bob",
                "tags": [],
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
                "tags": [],
            },
            input_company="Acme",
            force=False,
        )
        self.assertEqual(result["status"], "exists_no_linkedin_email")


class TestBatchCheck(unittest.TestCase):
    @patch.object(enrich, "stamp_serper_attempted_leads")
    @patch.object(enrich, "check_lead_exists")
    def test_persist_tags_stamps_linkedin_complete(self, mock_check, mock_stamp):
        om = Path("/tmp/om")
        mock_check.side_effect = [
            {"status": "exists_linkedin_email", "lead_id": 1, "tags": []},
            {"status": "exists_no_linkedin", "lead_id": 2, "tags": []},
            {"status": "not_found", "lead_id": None, "tags": []},
        ]
        mock_stamp.return_value = {"status": "added", "changed": 1}
        people = [
            {"full_name": "A", "company_name": "Co"},
            {"full_name": "B", "company_name": "Co"},
            {"full_name": "C", "company_name": "Co"},
        ]
        results, meta = enrich.batch_check(
            om, people, workspace="ws1", persist_tags=True,
        )
        self.assertEqual(len(results), 3)
        mock_stamp.assert_called_once()
        stamp_args = mock_stamp.call_args
        self.assertEqual(stamp_args[0][1], "ws1")
        self.assertEqual(stamp_args[0][2], [1])
        self.assertTrue(meta.get("persisted"))

    @patch.object(enrich, "check_lead_exists")
    def test_skip_tagged_propagates(self, mock_check):
        om = Path("/tmp/om")
        mock_check.return_value = {
            "status": "skipped_serper_attempted",
            "lead_id": 9,
            "tags": [enrich.SERPER_ATTEMPTED_TAG],
        }
        results, _meta = enrich.batch_check(
            om,
            [{"full_name": "Jane", "company_name": "Acme"}],
            workspace="ws1",
            skip_tagged=True,
        )
        self.assertEqual(results[0]["status"], "skipped_serper_attempted")
        mock_check.assert_called_once()
        self.assertTrue(mock_check.call_args.kwargs.get("skip_tagged"))


class TestStampSerperAttemptedLeads(unittest.TestCase):
    @patch.object(cc, "run_tag_bulk")
    def test_excludes_already_tagged(self, mock_bulk):
        om = Path("/tmp/om")
        mock_bulk.return_value = {"status": "added", "changed": 1}
        enrich.stamp_serper_attempted_leads(
            om,
            "ws1",
            [1, 2],
            known_tags_by_lead={1: [enrich.SERPER_ATTEMPTED_TAG], 2: []},
        )
        mock_bulk.assert_called_once()
        self.assertEqual(mock_bulk.call_args[0][2], [2])


class TestSerperSearch(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            enrich.serper_search("test", {"serper_endpoint": "https://example.com"})


class TestHermesEnv(unittest.TestCase):
    def setUp(self):
        cc._AGENT_ENV_LOADED = False
        self._saved = {
            k: os.environ[k]
            for k in ("SERPER_API_KEY", "OUTREACHMAGIC_AGENT_KEY", "HERMES_HOME")
            if k in os.environ
        }
        for k in self._saved:
            del os.environ[k]
        # Prevent _load_synced_agent_secrets from polluting env with real keys
        self._load_patcher = patch.object(cc, "_load_synced_agent_secrets")
        self._mock_load = self._load_patcher.start()

    def tearDown(self):
        self._load_patcher.stop()
        cc._AGENT_ENV_LOADED = False
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
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text(
                "SERPER_API_KEY=from-hermes-env\n"
                "OUTREACHMAGIC_AGENT_KEY=om_agent_test\n"
            )
            os.environ["HERMES_HOME"] = str(home)
            os.environ["OM_ALLOW_LOCAL_API_KEYS"] = "1"
            cc._AGENT_ENV_LOADED = False
            enrich.ensure_hermes_env_loaded()
            self.assertEqual(os.environ.get("SERPER_API_KEY"), "from-hermes-env")
            self.assertEqual(
                os.environ.get("OUTREACHMAGIC_AGENT_KEY"), "om_agent_test"
            )
            cfg = enrich.load_config()
            self.assertEqual(cfg["serper_api_key"], "from-hermes-env")

    def test_does_not_override_existing_shell_env(self):
        os.environ["SERPER_API_KEY"] = "shell-wins"
        os.environ["OM_ALLOW_LOCAL_API_KEYS"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("SERPER_API_KEY=from-file\n")
            os.environ["HERMES_HOME"] = str(home)
            cc._AGENT_ENV_LOADED = False
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("full_name,company_name\nJane,Acme\n")
            path = f.name
        try:
            data = enrich.load_people_file(path)
            self.assertEqual(len(data["people"]), 1)
        finally:
            Path(path).unlink()

    @patch.object(enrich, "load_config")
    @patch.object(enrich, "find_outreachmagic")
    @patch.object(enrich, "run_import_profiles")
    def test_backfill_import_failure_returns_error(self, mock_import, mock_find, _mock_cfg):
        import subprocess

        mock_find.return_value = Path("/tmp/om-test")
        mock_import.side_effect = subprocess.TimeoutExpired(cmd="import", timeout=60)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("linkedin,title\nhttps://linkedin.com/in/jane,VP\n")
            path = f.name
        try:
            buf = StringIO()
            with redirect_stdout(buf):
                enrich.cmd_backfill(path, "title", workspace="ws1")
            self.assertIn("error", buf.getvalue())
        finally:
            Path(path).unlink()


class TestFormatReport(unittest.TestCase):
    def test_skipped_serper_attempted_in_report(self):
        report = enrich.format_report([
            {
                "status": "skipped_serper_attempted",
                "_input": {"full_name": "Jane", "company_name": "Acme"},
            }
        ])
        self.assertIn("serper_attempted", report)
        self.assertIn("Skipped", report)


class TestCmdBatchCheckIntegration(unittest.TestCase):
    @patch.object(enrich, "batch_check")
    @patch.object(enrich, "find_outreachmagic")
    @patch.object(enrich, "load_config")
    def test_cmd_batch_check_stderr_on_persist(self, mock_cfg, mock_find, mock_batch):
        mock_cfg.return_value = {"dedup_before_search": True, "max_people_per_run": 50}
        mock_find.return_value = Path("/tmp/om")
        mock_batch.return_value = ([{"status": "not_found"}], {"persisted": True, "changed": 3, "requested": 3})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"people": [{"full_name": "Jane", "company_name": "Acme"}]}, f)
            path = f.name
        try:
            out = StringIO()
            err = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                enrich.cmd_batch_check(path, "ws1", persist_tags=True)
            self.assertIn("serper_attempted", err.getvalue())
            self.assertTrue(out.getvalue().startswith("["))
        finally:
            Path(path).unlink()


class TestRunTagBulk(unittest.TestCase):
    @patch.object(cc, "_run_subprocess_json")
    @patch.object(cc, "get_pipeline_path")
    def test_chunks_large_id_lists(self, mock_path, mock_run):
        mock_path.return_value = Path("/tmp/pipeline.py")
        mock_run.return_value = {"status": "added", "changed": 500}
        om = Path("/tmp/om")
        ids = list(range(1200))
        cc.run_tag_bulk(om, "ws1", ids, ["serper_attempted"], skill_dir=None)
        self.assertEqual(mock_run.call_count, 3)


class TestBuildSerperQueries(unittest.TestCase):
    def test_linkedin_query_unquoted_company(self):
        person = {
            "full_name": "Sean Williams",
            "company_name": "KPMG LLP",
            "stated_role": "Manager, Talent Acquisition",
        }
        queries = enrich.build_serper_queries(person)
        labels = [q["label"] for q in queries]
        self.assertIn("linkedin_profile", labels)
        self.assertNotIn("linkedin_profile_strict", labels)
        self.assertNotIn("linkedin_profile_broad", labels)
        linkedin = next(q for q in queries if q["label"] == "linkedin_profile")
        self.assertEqual(
            linkedin["query"],
            "site:linkedin.com/in Sean Williams Manager, Talent Acquisition KPMG LLP",
        )
        self.assertNotIn('"KPMG LLP"', linkedin["query"])

    def test_linkedin_query_without_role(self):
        person = {
            "full_name": "Jane Doe",
            "company_name": "Acme Corp",
            "stated_role": "",
        }
        queries = enrich.build_serper_queries(person)
        linkedin = next(q for q in queries if q["label"] == "linkedin_profile")
        self.assertEqual(linkedin["query"], "site:linkedin.com/in Jane Doe Acme Corp")


class TestSerperSearchPool(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        # Prevent _load_synced_agent_secrets from polluting env with real keys
        self._load_patcher = patch.object(cc, "_load_synced_agent_secrets")
        self._mock_load = self._load_patcher.start()
        # Reset API key pool session state (cross-test pollution from failover tracking)
        _clear_key_pool_session()

    def tearDown(self):
        self._load_patcher.stop()
        os.environ.clear()
        os.environ.update(self._saved)

    @patch.object(enrich, "_serper_search_with_key")
    def test_serper_search_uses_key_pool(self, mock_search):
        os.environ["OM_ALLOW_LOCAL_API_KEYS"] = "1"
        os.environ["SERPER_API_KEY"] = "primary"
        mock_search.return_value = {"organic": []}
        cfg = enrich.load_config()
        result = enrich.serper_search("site:linkedin.com/in Jane Doe Acme", cfg)
        self.assertEqual(result, {"organic": []})
        mock_search.assert_called_once_with("primary", "site:linkedin.com/in Jane Doe Acme", cfg)

    @patch.object(enrich, "_serper_search_with_key")
    def test_serper_search_failover_on_credit_exhaustion(self, mock_search):
        os.environ["OM_ALLOW_LOCAL_API_KEYS"] = "1"
        os.environ["SERPER_API_KEY"] = "exhausted"
        os.environ["SERPER_API_KEY__1"] = "backup"

        def side_effect(key, query, config):
            if key == "exhausted":
                raise ValueError('Serper HTTP 400: {"message":"Not enough credits"}')
            return {"organic": [{"title": "Jane Doe"}]}

        mock_search.side_effect = side_effect
        cfg = enrich.load_config()
        result = enrich.serper_search("site:linkedin.com/in Jane Doe Acme", cfg)
        self.assertEqual(result["organic"][0]["title"], "Jane Doe")
        self.assertEqual(mock_search.call_count, 2)


if __name__ == "__main__":
    unittest.main()
