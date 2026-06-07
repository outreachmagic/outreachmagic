import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_secrets_cloud  # noqa: E402


class AgentSecretsCloudTests(unittest.TestCase):
    def test_write_env_emits_pool_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_secrets.env"
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
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_secrets.env"
            agent_secrets_cloud.write_agent_secrets_env(
                path,
                {"SERPER_API_KEY": ["a", "b"]},
                version=1,
            )
            pools = agent_secrets_cloud.parse_agent_secrets_file(path)
            self.assertEqual(pools["SERPER_API_KEY"], ["a", "b"])

    def test_agent_secrets_path_org_scoped(self):
        root = Path("/tmp/data")
        p = agent_secrets_cloud.agent_secrets_path(root, "org_abc")
        self.assertEqual(p, root / "orgs" / "org_abc" / "agent_secrets.env")


if __name__ == "__main__":
    unittest.main()
