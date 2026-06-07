import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "skills" / "outreachmagic" / "scripts" / "pipeline.py"
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import om_paths  # noqa: E402


class ApiKeysCliTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self._tmpdir = tempfile.TemporaryDirectory()
        om_paths.set_data_root_override(Path(self._tmpdir.name))

    def tearDown(self):
        om_paths.set_data_root_override(None)
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._saved)

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        return subprocess.run(
            [sys.executable, str(PIPELINE), "api-keys", *extra],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(SCRIPTS),
            check=False,
        )

    def test_api_keys_json_no_keys(self):
        proc = self._run("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("providers", payload)
        serper = next(item for item in payload["providers"] if item["provider"] == "serper")
        self.assertEqual(serper["status"], "no_keys")

    def test_api_keys_text_with_configured_key(self):
        os.environ["SERPER_API_KEY"] = "primary-key-1234567890"
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Serper (lead-enrich):", proc.stdout)
        self.assertIn("Primary", proc.stdout)


if __name__ == "__main__":
    unittest.main()
