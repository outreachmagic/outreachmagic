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

from providers import provider_note_text  # noqa: E402


class TestValidityNotes(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            provider_note_text("trykitt", "valid", found=True),
            "trykitt verify: valid",
        )

    def test_catch_all(self):
        self.assertEqual(
            provider_note_text("trykitt", "valid-risky", found=True),
            "trykitt verify: catch_all",
        )

    def test_not_found(self):
        self.assertEqual(
            provider_note_text("trykitt", "", found=False),
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
            normalize_linkedin_fn=norm.normalize_linkedin,
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
            normalize_linkedin_fn=norm.normalize_linkedin,
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
            normalize_linkedin_fn=norm.normalize_linkedin,
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
    @patch.dict(
        "os.environ",
        {
            "TRYKITT_API_KEY": "testkey1234567890123456789012",
            "OM_ALLOW_LOCAL_API_KEYS": "1",
        },
    )
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
            with patch.object(lemail.cc, "load_dotenv_file"), patch.object(
                lemail.cc, "_load_synced_agent_secrets"
            ):
                cfg = lemail.load_config()
                cfg.pop("trykitt_api_key", None)
                result = prov.trykitt_find(cfg, full_name="Jane", domain="acme.com")
                self.assertEqual(result["status"], "no_key")


class TestIcypeasFind(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {"ICYPEAS_API_KEY": "test_icypeas", "OM_ALLOW_LOCAL_API_KEYS": "1"},
    )
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

    def test_icypeas_valid_risky_is_catch_all(self):
        self.assertEqual(prov.validity_to_verify_status("valid-risky", provider="icypeas"), "catch_all")


class TestImportProfileLeadId(unittest.TestCase):
    def test_lead_id_in_profile(self):
        profile = lemail.build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="https://linkedin.com/in/jane",
            find_result={"email": "j@acme.com", "validity": "valid"},
            normalize_linkedin_fn=norm.normalize_linkedin,
            lead_id=42,
        )
        self.assertEqual(profile["id"], 42)
        self.assertEqual(profile["linkedin"], norm.normalize_linkedin("https://linkedin.com/in/jane"))


class TestIncrementalWriterResume(unittest.TestCase):
    def test_merges_json_and_csv_done_keys(self):
        import batch_runner as br
        with tempfile.TemporaryDirectory() as td:
            base = str(Path(td) / "out")
            writer = br.IncrementalWriter(base)
            writer.append({
                "resume_key": "id:1",
                "lead_id": "1",
                "name": "A",
                "domain": "a.com",
                "email": "",
                "validity": "",
                "error": "",
                "provider": "",
                "api_calls": 1,
                "status": "not_found",
                "timestamp": "2026-01-01T00:00:00Z",
            }, "id:1")
            writer.finalize()
            with open(f"{base}.csv", "a", encoding="utf-8", newline="") as fh:
                w = __import__("csv").writer(fh)
                w.writerow([
                    "id:2", "2", "B", "b.com", "b@b.com", "valid", "",
                    "trykitt", 1, "found", "2026-01-02T00:00:00Z",
                ])
            writer2 = br.IncrementalWriter(base)
            self.assertIn("id:1", writer2.done_keys)
            self.assertIn("id:2", writer2.done_keys)
            self.assertEqual(len(writer2.done_keys), 2)


class TestCreditsExhaustedStatus(unittest.TestCase):
    @patch.object(prov, "icypeas_find")
    @patch.object(prov, "trykitt_find")
    def test_all_providers_credit_errors(self, mock_trykitt, mock_icypeas):
        cfg = {"trykitt_enabled": True, "icypeas_enabled": True}
        mock_trykitt.side_effect = prov.CreditsExhaustedError("trykitt out of credits")
        mock_icypeas.side_effect = prov.CreditsExhaustedError("icypeas out of credits")
        result = prov.run_find_with_fallback(cfg, full_name="Jane", domain="acme.com")
        self.assertEqual(result["status"], "credits_exhausted")


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


class TestBatchPreSkipped(unittest.TestCase):
    @patch.object(lemail.cc, "run_batch_lead_lookup")
    def test_dedup_skipped_rows_not_pending(self, mock_lookup):
        import batch_runner as br

        mock_lookup.return_value = {
            "results": [
                {
                    "index": 0,
                    "status": "found",
                    "lead_id": 99,
                    "email": "exists@acme.com",
                    "tags": [],
                }
            ]
        }
        opts = lemail.BatchOptions(yes=True, skip_om=False, no_save=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump([{"name": "Jane", "domain": "acme.com", "lead_id": 99}], tmp)
            tmp.flush()
            lemail.cc._AGENT_ENV_LOADED = False
            with patch.object(lemail.cc, "load_dotenv_file"):
                cfg = lemail.load_config()
            cfg["trykitt_api_key"] = "k"
            cfg["icypeas_api_key"] = "k"
            with patch.object(br, "run_health_check", return_value=(True, [], [])):
                result = br.run_batch(
                    tmp.name,
                    cfg,
                    Path("/tmp/om"),
                    opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=norm.normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
        self.assertEqual(result["stats"]["skipped"], 1)
        self.assertEqual(result["results"][0]["batch_status"], "skipped")
        self.assertEqual(result["results"][0]["skip_reason"], "has_email")
        self.assertEqual(result["processed"], 0)


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
    @patch.object(lemail.cc, "save_email_find_profiles")
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
            "mode": "apply_email_find_results",
            "recorded": 1,
            "results": [
                {"lead_id": 1},
                {"lead_id": 2},
            ],
        }
        om = Path("/tmp/om")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "batch.json"
            inp.write_text(json.dumps([
                {"lead_id": 1, "name": "A", "domain": "acme.com"},
                {"lead_id": 2, "name": "B", "domain": "acme.com"},
            ]))
            out_base = str(Path(td) / "out")
            opts = lemail.BatchOptions(
                yes=True,
                skip_om=False,
                workspace="ws1",
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
            ):
                from batch_runner import run_batch
                result = run_batch(
                    str(inp),
                    cfg,
                    om,
                    opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=norm.normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
                mock_import.assert_called_once()
                self.assertTrue((Path(td) / "out.csv").exists())
                self.assertEqual(result["stats"]["found"], 1)


class TestCheckpointImport(unittest.TestCase):
    def test_profiles_from_checkpoint_csv_row(self):
        from batch_runner import profiles_from_checkpoint_rows

        profiles = profiles_from_checkpoint_rows(
            [{
                "lead_id": "42",
                "name": "Jane",
                "domain": "acme.com",
                "email": "jane@acme.com",
                "validity": "valid",
                "provider": "trykitt",
                "status": "found",
            }],
            lemail.normalize_linkedin,
        )
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["id"], 42)
        self.assertEqual(profiles[0]["email"], "jane@acme.com")
        self.assertIn("email_found", profiles[0]["tags"])

    def test_load_profiles_from_checkpoint_json(self):
        from batch_runner import load_profiles_for_om_import

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.json"
            path.write_text(json.dumps([
                {
                    "lead_id": 7,
                    "name": "Bob",
                    "domain": "beta.com",
                    "email": "bob@beta.com",
                    "validity": "valid",
                    "provider": "icypeas",
                    "status": "found",
                },
            ]))
            profiles, ws = load_profiles_for_om_import(str(path), normalize_linkedin_fn=lemail.normalize_linkedin)
            self.assertIsNone(ws)
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["list_source"], "icypeas")


class TestMillionVerifier(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_verify_single_uses_get_with_api_param(self, mock_urlopen):
        import millionverifier as mv_mod

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"email": "a@b.com", "result": "ok", "subresult": "", "credits": 100}
        ).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        lemail.cc._AGENT_ENV_LOADED = False
        with patch.object(lemail.cc, "load_dotenv_file"):
            cfg = lemail.load_config()
        cfg["millionverifier_api_key"] = "test_mv"
        out = mv_mod.MillionVerifierProvider(cfg["millionverifier_api_key"]).verify_single("a@b.com")
        self.assertEqual(out["status"], "valid")
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("api=test_mv", called_url)
        self.assertIn("email=a%40b.com", called_url)
        self.assertNotIn("/verify", called_url)


class TestCompanionCommonPipelinePayload(unittest.TestCase):
    def setUp(self):
        lemail.cc._AGENT_ENV_LOADED = False

    def test_append_json_inline_under_threshold(self):
        cmd, temp = lemail.cc._append_json_or_file(["pipeline"], [{"id": 1}])
        self.assertIsNone(temp)
        self.assertEqual(cmd[0], "pipeline")
        self.assertEqual(cmd[1], "--json")

    def test_append_json_uses_file_over_threshold(self):
        big = [{"id": i, "notes": "x" * 500} for i in range(300)]
        cmd, temp = lemail.cc._append_json_or_file(["pipeline"], big)
        self.assertIsNotNone(temp)
        self.assertEqual(cmd[-2], "--file")
        self.assertTrue(Path(temp).is_file())
        Path(temp).unlink(missing_ok=True)

    def test_chunk_timeout_defaults(self):
        self.assertEqual(lemail.cc._chunk_timeout(200), 160)
        self.assertEqual(lemail.cc._chunk_timeout(100), 80)
        self.assertEqual(lemail.cc._chunk_timeout(1000), 300)

    def test_profiles_have_known_lead_ids(self):
        self.assertTrue(lemail.cc.profiles_have_known_lead_ids([{"id": 1}, {"lead_id": 2}]))
        self.assertFalse(lemail.cc.profiles_have_known_lead_ids([{"id": 1}, {"name": "x"}]))

    def test_merge_import_summaries(self):
        merged = lemail.cc._merge_pipeline_summaries([
            {"processed": 100, "matched": 80, "enriched": 50, "created": 0, "results": [{"lead_id": 1}]},
            {"processed": 100, "matched": 70, "enriched": 40, "created": 1, "results": [{"lead_id": 2}]},
        ])
        self.assertEqual(merged["processed"], 200)
        self.assertEqual(merged["matched"], 150)
        self.assertEqual(merged["enriched"], 90)
        self.assertEqual(merged["created"], 1)
        self.assertEqual(len(merged["results"]), 2)
        self.assertEqual(merged["chunks"], 2)

    @patch.object(lemail.cc, "_run_subprocess_json")
    @patch.object(lemail.cc, "_append_json_or_file")
    def test_import_profiles_chunks_over_200(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"processed": 10, "matched": 8, "enriched": 5, "created": 0, "results": []}
        profiles = [{"name": f"P{i}", "company_domain": "acme.com"} for i in range(450)]
        om = Path("/tmp/om-test")
        out = lemail.cc.run_import_profiles(om, profiles, workspace="ws", skill_dir=lemail._find_skill_dir())
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(out["processed"], 30)
        self.assertEqual(out["chunks"], 3)

    @patch.object(lemail.cc, "_run_subprocess_json")
    @patch.object(lemail.cc, "_append_json_or_file")
    def test_verify_batch_chunks(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"recorded": 50, "errors": []}
        items = [{"lead_id": i, "status": "valid", "source": "trykitt"} for i in range(250)]
        out = lemail.cc.run_verify_email_batch(Path("/tmp/om"), items)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(out["recorded"], 100)

    @patch.object(lemail.cc, "_run_subprocess_json")
    def test_import_argv_stays_under_arg_max(self, mock_run):
        mock_run.return_value = {"processed": 1, "matched": 0, "enriched": 0, "created": 0, "results": []}
        profiles = [
            {
                "id": i,
                "name": f"User {i}",
                "company_domain": "acme.com",
                "tags": ["trykitt_attempted", "email_found"],
                "notes": "x" * 400,
            }
            for i in range(600)
        ]
        lemail.cc.run_import_profiles(Path("/tmp/om"), profiles, workspace="ws")
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            json_arg_len = 0
            if "--json" in cmd:
                json_arg_len = len(cmd[cmd.index("--json") + 1])
            self.assertLess(json_arg_len, lemail.cc.JSON_ARG_THRESHOLD + 1)
            self.assertTrue("--file" in cmd or json_arg_len <= lemail.cc.JSON_ARG_THRESHOLD)
            total_argv = sum(len(str(a)) for a in cmd)
            self.assertLess(total_argv, 500_000)

    @patch.object(lemail.cc, "_run_subprocess_json")
    @patch.object(lemail.cc, "_append_json_or_file")
    def test_batch_lead_lookup_single_call_small(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"status": "ok", "results": [{"index": 0}]}
        items = [{"index": 0, "lead_id": 1}]
        out = lemail.cc.run_batch_lead_lookup(Path("/tmp/om"), items)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(len(out["results"]), 1)


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
        self.assertEqual(mock_import.call_args.kwargs.get("source"), "trykitt")
        profiles = mock_import.call_args[0][1]
        self.assertEqual(profiles[0]["tags"], ["trykitt_attempted"])


class TestImportProfileCollection(unittest.TestCase):
    def test_should_import_not_found_without_attempts_list(self):
        from batch_runner import collect_import_profiles, should_import_result

        result = {"batch_status": "processed", "status": "not_found", "provider": "trykitt"}
        self.assertTrue(should_import_result(result))
        profiles = collect_import_profiles(
            [{"lead_id": 1, "name": "A", "domain": "acme.com"}],
            [result],
            norm.normalize_linkedin,
        )
        self.assertEqual(len(profiles), 1)
        self.assertIn("trykitt_attempted", profiles[0]["tags"])

    def test_resolve_profiles_falls_back_to_checkpoint(self):
        from batch_runner import resolve_profiles_for_import

        checkpoint = [{
            "lead_id": "5",
            "name": "Jane",
            "domain": "acme.com",
            "email": "jane@acme.com",
            "status": "found",
            "provider": "trykitt",
            "validity": "valid",
        }]
        skipped = {"batch_status": "skipped", "status": "skipped", "skip_reason": "resume_done"}
        profiles, source = resolve_profiles_for_import(
            [{"lead_id": 5, "name": "Jane", "domain": "acme.com"}],
            [skipped],
            norm.normalize_linkedin,
            checkpoint_rows=checkpoint,
        )
        self.assertEqual(source, "from_checkpoint")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["email"], "jane@acme.com")

    def test_bulk_dedup_map_reports_lookup_failure(self):
        from batch_runner import bulk_dedup_map

        with patch.object(lemail.cc, "run_batch_lead_lookup", side_effect=RuntimeError("timeout")):
            out, failed = bulk_dedup_map(
                Path("/tmp/om"),
                [{"lead_id": 1, "name": "A", "domain": "acme.com"}],
                workspace="ws",
                skill_dir=lemail._find_skill_dir(),
                provider_names=["trykitt"],
            )
        self.assertEqual(out, {})
        self.assertTrue(failed)


class TestResumeImport(unittest.TestCase):
    @patch.object(lemail.cc, "save_email_find_profiles")
    @patch.object(lemail.cc, "run_batch_lead_lookup")
    @patch("batch_runner.run_find_with_fallback")
    def test_resume_run_imports_from_checkpoint(self, mock_find, mock_lookup, mock_save):
        import batch_runner as br

        mock_lookup.return_value = {"results": [{"index": 0, "status": "not_found"}]}
        mock_save.return_value = {"mode": "apply_email_find_results", "recorded": 1, "results": [{"lead_id": 1}]}
        om = Path("/tmp/om")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "batch.json"
            inp.write_text(json.dumps([{"lead_id": 1, "name": "A", "domain": "acme.com"}]))
            out_base = str(Path(td) / "out")
            checkpoint_csv = Path(f"{out_base}.csv")
            checkpoint_csv.write_text(
                "resume_key,lead_id,name,domain,email,validity,error,provider,api_calls,status,icypeas_status,timestamp\n"
                "id:1,1,A,acme.com,a@acme.com,valid,,trykitt,1,found,,2020-01-01T00:00:00Z\n"
            )
            opts = lemail.BatchOptions(
                yes=True, skip_om=False, workspace="ws1", output_base=out_base, workers=1, delay=0,
            )
            cfg = {
                "max_people_per_run": 500,
                "trykitt_enabled": True,
                "icypeas_enabled": False,
                "trykitt_api_key": "testkey1234567890123456789012",
            }
            with patch.object(lemail, "find_outreachmagic", return_value=om), patch.object(
                br, "run_health_check", return_value=(True, [], []),
            ):
                result = br.run_batch(
                    str(inp),
                    cfg,
                    om,
                    opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=norm.normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
        mock_find.assert_not_called()
        mock_save.assert_called_once()
        profiles = mock_save.call_args[0][1]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["email"], "a@acme.com")
        self.assertEqual(result["processed"], 0)

    @patch.object(lemail.cc, "save_email_find_profiles")
    @patch.object(lemail.cc, "run_batch_lead_lookup")
    @patch("batch_runner.run_find_with_fallback")
    def test_batch_imports_found_and_not_found(self, mock_find, mock_lookup, mock_save):
        import batch_runner as br

        mock_lookup.return_value = {
            "results": [
                {"index": 0, "status": "not_found"},
                {"index": 1, "status": "not_found"},
            ],
        }
        mock_find.side_effect = [
            {"status": "found", "email": "a@acme.com", "validity": "valid", "provider": "trykitt"},
            {"status": "not_found", "provider": "trykitt"},
        ]
        mock_save.return_value = {"mode": "apply_email_find_results", "recorded": 1, "results": []}
        om = Path("/tmp/om")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "batch.json"
            inp.write_text(json.dumps([
                {"lead_id": 1, "name": "A", "domain": "acme.com"},
                {"lead_id": 2, "name": "B", "domain": "acme.com"},
            ]))
            out_base = str(Path(td) / "out")
            opts = lemail.BatchOptions(
                yes=True, skip_om=False, workspace="ws1", output_base=out_base, workers=1, delay=0,
            )
            cfg = {
                "max_people_per_run": 500,
                "trykitt_enabled": True,
                "icypeas_enabled": False,
                "trykitt_api_key": "testkey1234567890123456789012",
            }
            with patch.object(lemail, "find_outreachmagic", return_value=om), patch.object(
                br, "run_health_check", return_value=(True, [], []),
            ):
                br.run_batch(
                    str(inp), cfg, om, opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=norm.normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
        profiles = mock_save.call_args[0][1]
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["email"], "a@acme.com")
        self.assertIn("trykitt_attempted", profiles[1]["tags"])
        self.assertNotIn("email_found", profiles[1]["tags"])


class TestImportSummaryOutput(unittest.TestCase):
    def test_final_summary_always_shows_import_section(self):
        from progress import print_final_summary
        import io

        buf = io.StringIO()
        print_final_summary(
            {"found": 1, "not_found": 0, "errors": 0, "skipped_email": 2},
            10.0,
            "/tmp/out",
            import_status={"reason": "no_profiles", "recovery_hint": "import-to-om ..."},
            file=buf,
        )
        text = buf.getvalue()
        self.assertIn("IMPORT", text)
        self.assertIn("No import performed", text)
        self.assertIn("SKIPPED", text)
        self.assertIn("Already has email", text)

    def test_no_save_shows_import_skipped(self):
        from progress import print_final_summary
        import io

        buf = io.StringIO()
        print_final_summary(
            {"found": 0, "not_found": 0, "errors": 0},
            1.0,
            "",
            import_status={"reason": "no_save"},
            file=buf,
        )
        self.assertIn("no-save", buf.getvalue())


class TestFreshOmDedup(unittest.TestCase):
    def test_skip_resolved_before_api(self):
        from batch_runner import skip_resolved_before_api

        chunk = [(0, {"lead_id": 1, "name": "A", "domain": "acme.com"})]
        lookup: dict = {}
        with patch.object(lemail.cc, "run_batch_lead_lookup") as mock_lookup:
            mock_lookup.return_value = {
                "results": [{
                    "index": 0,
                    "status": "found",
                    "email": "exists@acme.com",
                    "tags": [],
                }],
            }
            api_chunk, updated, skipped = skip_resolved_before_api(
                Path("/tmp/om"),
                chunk,
                lookup,
                workspace="ws",
                skill_dir=lemail._find_skill_dir(),
                provider_names=["trykitt"],
            )
        self.assertEqual(api_chunk, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][2], "has_email")
        self.assertIn(0, updated)

    @patch.object(lemail.cc, "save_email_find_profiles")
    @patch.object(lemail.cc, "run_batch_lead_lookup")
    @patch("batch_runner.run_find_with_fallback")
    def test_fresh_om_skip_before_api_in_batch(self, mock_find, mock_lookup, mock_save):
        import batch_runner as br

        mock_lookup.side_effect = [
            {"results": [{"index": 0, "status": "not_found"}]},
            {"results": [{
                "index": 0,
                "status": "found",
                "email": "filled@acme.com",
                "tags": [],
            }]},
        ]
        mock_save.return_value = {"mode": "apply_email_find_results", "recorded": 0, "results": []}
        om = Path("/tmp/om")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "batch.json"
            inp.write_text(json.dumps([{"lead_id": 1, "name": "A", "domain": "acme.com"}]))
            opts = lemail.BatchOptions(
                yes=True, skip_om=False, workspace="ws1",
                output_base=str(Path(td) / "out"), workers=1, delay=0,
            )
            cfg = {
                "max_people_per_run": 500,
                "trykitt_enabled": True,
                "icypeas_enabled": False,
                "trykitt_api_key": "testkey1234567890123456789012",
            }
            with patch.object(lemail, "find_outreachmagic", return_value=om), patch.object(
                br, "run_health_check", return_value=(True, [], []),
            ):
                result = br.run_batch(
                    str(inp), cfg, om, opts,
                    skill_dir=lemail._find_skill_dir(),
                    normalize_linkedin_fn=norm.normalize_linkedin,
                    key_status_fn=lemail.cc.outreachmagic_agent_key_status,
                )
        mock_find.assert_not_called()
        self.assertEqual(result["stats"]["skipped_fresh_om"], 1)
        self.assertEqual(result["processed"], 0)


class TestCreditAccounting(unittest.TestCase):
    def test_find_credits_one_per_found(self):
        import credits as cr

        self.assertEqual(cr.find_credits_used(found=True), 1)
        self.assertEqual(cr.find_credits_used(found=False), 0)

    def test_icypeas_credits_only_when_email_returned(self):
        import credits as cr

        self.assertEqual(cr.icypeas_credits_for_status("DEBITED", email="a@b.com"), 1)
        self.assertEqual(cr.icypeas_credits_for_status("DEBITED_NOT_FOUND"), 0)

    def test_mv_credit_summary(self):
        import credits as cr

        plan = cr.mv_credit_summary(email_count=1691, credits_remaining=760404)
        self.assertEqual(plan["credits_required"], 1691)
        self.assertEqual(plan["credits_remaining"], 760404)
        self.assertTrue(plan["sufficient_credits"])

    @patch.object(lemail, "find_outreachmagic")
    @patch.object(lemail.cc, "run_verification_candidates")
    def test_verify_bulk_dry_run(self, mock_candidates, mock_om):
        import io

        mock_om.return_value = Path("/tmp/om")
        mock_candidates.return_value = {
            "count": 2,
            "leads": [
                {"lead_id": 1, "email": "a@b.com"},
                {"lead_id": 2, "email": "b@c.com"},
            ],
        }
        lemail.cc._AGENT_ENV_LOADED = False
        with patch.object(lemail, "load_config", return_value={"millionverifier_api_key": "mvkey"}):
            with patch.object(lemail, "_mv_provider") as mock_mv_provider:
                mv = MagicMock()
                mv.check_credits.return_value = (100.0, None)
                mock_mv_provider.return_value = mv
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    lemail.cmd_verify_bulk(workspace="ws", dry_run=True)
                mv.create_bulk.assert_not_called()
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["status"], "dry_run")
                self.assertEqual(payload["credits_required"], 2)
                self.assertEqual(payload["credits_per_email"], 1)


if __name__ == "__main__":
    unittest.main()
