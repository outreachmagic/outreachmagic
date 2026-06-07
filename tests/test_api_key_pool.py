import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import api_key_pool  # noqa: E402
import om_paths  # noqa: E402


class ApiKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmpdir = tempfile.TemporaryDirectory()
        om_paths.set_data_root_override(Path(self._tmpdir.name))

    def tearDown(self):
        om_paths.set_data_root_override(None)
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._saved)

    def test_api_key_pool_order(self):
        os.environ["SERPER_API_KEY"] = "primary"
        os.environ["SERPER_API_KEY__1"] = "backup1"
        os.environ["SERPER_API_KEY__2"] = ""
        self.assertEqual(api_key_pool.api_key_pool("SERPER_API_KEY"), ["primary", "backup1"])

    def test_call_with_key_pool_failover(self):
        os.environ["SERPER_API_KEY"] = "bad"
        os.environ["SERPER_API_KEY__1"] = "good"
        calls: list[str] = []

        def fn(key: str) -> str:
            calls.append(key)
            if key == "bad":
                raise urllib.error.HTTPError("url", 429, "rate", hdrs=None, fp=None)
            return "ok"

        out = api_key_pool.call_with_key_pool("SERPER_API_KEY", fn, provider="serper")
        self.assertEqual(out, "ok")
        self.assertEqual(calls, ["bad", "good"])

    def test_serper_credit_exhaustion_failover(self):
        os.environ["SERPER_API_KEY"] = "exhausted"
        os.environ["SERPER_API_KEY__1"] = "good"
        calls: list[str] = []

        def fn(key: str) -> str:
            calls.append(key)
            if key == "exhausted":
                raise ValueError('Serper HTTP 400: {"message":"Not enough credits"}')
            return "ok"

        out = api_key_pool.call_with_key_pool("SERPER_API_KEY", fn, provider="serper")
        self.assertEqual(out, "ok")
        self.assertEqual(calls, ["exhausted", "good"])

    def test_value_error_is_failover_serper_credits(self):
        exc = ValueError('Serper HTTP 400: {"message":"Not enough credits"}')
        self.assertTrue(api_key_pool.value_error_is_failover(exc))
        self.assertFalse(api_key_pool.value_error_is_failover(ValueError("Serper HTTP 400: bad query")))

    def test_result_should_failover_auth(self):
        self.assertTrue(
            api_key_pool.result_should_failover(
                {"status": "http_error", "http_status": 401},
                provider="trykitt",
            )
        )
        self.assertFalse(
            api_key_pool.result_should_failover(
                {"status": "not_found", "email": None},
                provider="trykitt",
            )
        )

    def test_record_key_usage_writes_slot_indexed_status(self):
        api_key_pool.record_key_usage(provider="serper", slot=0, success=True)
        api_key_pool.record_key_usage(
            provider="serper",
            slot=1,
            success=False,
            error="Not enough credits",
        )
        path = api_key_pool.status_file_path()
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["serper"]["0"]["status"], "ok")
        self.assertEqual(payload["serper"]["1"]["status"], "failed")
        self.assertEqual(payload["serper"]["1"]["last_error"], "Not enough credits")

    def test_build_api_keys_report_merges_config_and_status(self):
        os.environ["SERPER_API_KEY"] = "primary-key-1234567890"
        os.environ["SERPER_API_KEY__1"] = "backup-key-abcdefghij"
        api_key_pool.record_key_usage(provider="serper", slot=0, success=True)
        api_key_pool.record_key_usage(
            provider="serper",
            slot=1,
            success=False,
            error="Not enough credits",
        )
        report = api_key_pool.build_api_keys_report()
        serper = next(item for item in report["providers"] if item["provider"] == "serper")
        self.assertEqual(len(serper["keys"]), 2)
        self.assertEqual(serper["keys"][0]["status"], "ok")
        self.assertEqual(serper["keys"][0]["label"], "Primary")
        self.assertEqual(serper["keys"][1]["status"], "failed")
        self.assertEqual(serper["keys"][1]["last_error"], "Not enough credits")

    def test_build_api_keys_report_no_keys(self):
        report = api_key_pool.build_api_keys_report()
        serper = next(item for item in report["providers"] if item["provider"] == "serper")
        self.assertEqual(serper["status"], "no_keys")
        self.assertEqual(serper["keys"], [])

    def test_format_api_keys_report_text(self):
        os.environ["SERPER_API_KEY"] = "primary-key-1234567890"
        report = api_key_pool.build_api_keys_report()
        text = api_key_pool.format_api_keys_report_text(report)
        self.assertIn("Serper (lead-enrich):", text)
        self.assertIn("Primary", text)
        self.assertIn("never_used", text)


if __name__ == "__main__":
    unittest.main()
