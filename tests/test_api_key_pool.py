import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import api_key_pool  # noqa: E402


class ApiKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
