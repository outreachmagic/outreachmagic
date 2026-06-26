import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_secrets_cloud  # noqa: E402
import om_paths  # noqa: E402


class AgentSecretsCloudTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        om_paths.set_data_root_override(self._root)
        (self._root / "skills" / "outreachmagic" / "config").mkdir(parents=True)

    def tearDown(self):
        om_paths.set_data_root_override(None)
        self._tmpdir.cleanup()

    def test_write_env_emits_pool_suffixes(self):
        path = om_paths.get_agent_secrets_path()
        secrets = {
            "SERPER_API_KEY": ["primary", "backup"],
            "TRYKITT_API_KEY": ["only"],
        }
        keys = agent_secrets_cloud.write_agent_secrets_env(path, secrets, version=3)
        text = path.read_text()
        self.assertIn("SERPER_API_KEY=primary", text)
        self.assertIn("SERPER_API_KEY__1=backup", text)
        self.assertIn("TRYKITT_API_KEY=only", text)
        self.assertIn("version: 3", text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("SERPER_API_KEY__1", keys)

    def test_parse_roundtrip(self):
        path = om_paths.get_agent_secrets_path()
        agent_secrets_cloud.write_agent_secrets_env(
            path,
            {"SERPER_API_KEY": ["a", "b"]},
            version=1,
        )
        pools = agent_secrets_cloud.parse_agent_secrets_file(path)
        self.assertEqual(pools["SERPER_API_KEY"], ["a", "b"])

    def test_agent_secrets_path_under_skill_config(self):
        expected = self._root / "skills" / "outreachmagic" / "config" / "agent_secrets.env"
        self.assertEqual(agent_secrets_cloud.agent_secrets_path(), expected)

    def test_mirror_agent_secrets_to_data_env_preserves_other_keys(self):
        data_env = self._root / ".env"
        data_env.write_text("OUTREACHMAGIC_AGENT_KEY=om_agent_test\nCUSTOM=keep\n", encoding="utf-8")
        secrets = {
            "TRYKITT_API_KEY": ["trykitt-primary"],
            "ICYPEAS_API_KEY": ["icypeas-only"],
        }
        path = agent_secrets_cloud.mirror_agent_secrets_to_data_env(secrets)
        self.assertEqual(path, data_env)
        text = data_env.read_text()
        self.assertIn("OUTREACHMAGIC_AGENT_KEY=om_agent_test", text)
        self.assertIn("CUSTOM=keep", text)
        self.assertIn("TRYKITT_API_KEY=trykitt-primary", text)
        self.assertIn("ICYPEAS_API_KEY=icypeas-only", text)

    def test_load_local_includes_backup_slots(self):
        path = om_paths.get_agent_secrets_path()
        agent_secrets_cloud.write_agent_secrets_env(
            path,
            {"SERPER_API_KEY": ["primary", "backup"]},
            version=1,
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            agent_secrets_cloud.load_local_agent_secrets_to_environ()
            self.assertEqual(os.environ.get("SERPER_API_KEY"), "primary")
            self.assertEqual(os.environ.get("SERPER_API_KEY__1"), "backup")


if __name__ == "__main__":
    unittest.main()
