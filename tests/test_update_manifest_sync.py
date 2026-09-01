"""Ensure pipeline update installs every file listed in update-manifest.json."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
MANIFEST = ROOT / "skills" / "outreachmagic" / "update-manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_update  # noqa: E402
from generate_skill_manifest import generate_manifest  # noqa: E402
from skill_suite import manifest_relative_paths  # noqa: E402


class UpdateManifestSyncTests(unittest.TestCase):
    def test_update_script_files_include_every_script_module(self):
        all_py = {p.relative_to(SCRIPTS).as_posix() for p in SCRIPTS.glob("**/*.py")}
        self.assertEqual(set(om.UPDATE_SCRIPT_FILES), all_py)

    def test_manifest_lists_every_script_module(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest_scripts = {
            name for name in manifest.get("files", {})
            if name.endswith(".py")
        }
        all_py = {p.relative_to(SCRIPTS).as_posix() for p in SCRIPTS.glob("**/*.py")}
        self.assertEqual(manifest_scripts, all_py)

    def test_update_download_names_uses_manifest(self):
        manifest = {
            "files": {
                "pipeline.py": "abc",
                "pipeline_lead_review.py": "def",
                "review_cloud.py": "ghi",
                "pipeline_dedup.py": "jkl",
                "campaign_stats.py": "stu",
                "VERSION": "mno",
                "SKILL.md": "pqr",
            },
        }
        names = om.update_download_names(manifest)
        self.assertIn("pipeline_lead_review.py", names)
        self.assertIn("review_cloud.py", names)
        self.assertIn("pipeline_dedup.py", names)
        self.assertIn("campaign_stats.py", names)
        self.assertNotIn("SKILL.md", names)

    def test_committed_manifest_includes_campaign_stats(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        names = om.update_download_names(manifest)
        self.assertIn("campaign_stats.py", names)

    def test_main_channel_update_uses_remote_manifest(self):
        """Legacy installs missing new modules must still download them from main."""
        import hashlib

        stub = b"# stub\n"
        stub_hash = hashlib.sha256(stub).hexdigest()
        remote_manifest = {
            "version": "1.0.0",
            "files": {
                "campaign_stats.py": stub_hash,
                "pipeline.py": stub_hash,
                "VERSION": stub_hash,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "scripts"
            dest.mkdir()
            (dest / "pipeline.py").write_text("# legacy install\n", encoding="utf-8")
            (dest / "VERSION").write_text("1.0.0\n", encoding="utf-8")

            with patch.object(om, "skill_scripts_dir", return_value=dest):
                with patch.object(om, "backup_scripts_for_rollback"):
                    with patch.object(
                        om,
                        "resolve_update_source",
                        return_value=(None, "http://example/scripts", "http://example", "main"),
                    ):
                        with patch.object(om, "fetch_update_manifest", return_value=remote_manifest) as fetch:
                            with patch.object(om, "_fetch_url", return_value=stub):
                                with patch.object(pipeline_update, "_migrate_db_in_subprocess"):
                                    with patch.object(om, "sync_skill_md_version"):
                                        with patch.object(om, "load_config", return_value={}):
                                            with patch.object(om, "save_config"):
                                                result = om.update_skill(channel="main")

            fetch.assert_called_once()
            self.assertIn("campaign_stats.py", result["files"])
            self.assertTrue((dest / "campaign_stats.py").is_file())

    def test_manifest_generator_matches_on_disk(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest_relative_paths("outreachmagic")),
            set(manifest.get("files", {})),
        )
        generated = generate_manifest("outreachmagic")
        self.assertEqual(generated["files"], manifest["files"])


if __name__ == "__main__":
    unittest.main()


class ReferenceDocShippingTests(unittest.TestCase):
    """`update` has to carry the docs the code points people at.

    references/*.md live beside SKILL.md, not in scripts/, and
    update_download_names walks the scripts directory and filters .md out — so
    an installed reference doc was frozen at whatever the original install
    shipped, forever. The SQL timeout message names references/sql-pitfalls.md
    as the place to look, which is worse than saying nothing if the page never
    arrives.
    """

    def test_reference_docs_are_selected_from_the_manifest(self):
        import pipeline_update

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        docs = pipeline_update.manifest_reference_docs(manifest)
        self.assertIn("references/sql-pitfalls.md", docs)
        self.assertTrue(all(d.startswith("references/") for d in docs), docs)
        # And they must not also be in the scripts list, or update would try to
        # copy them out of scripts/ and fail.
        self.assertFalse(
            set(docs) & set(pipeline_update.update_download_names(manifest)))

    def test_no_manifest_means_no_reference_docs(self):
        import pipeline_update

        self.assertEqual(pipeline_update.manifest_reference_docs(None), [])
        self.assertEqual(pipeline_update.manifest_reference_docs({}), [])

    def test_every_referenced_doc_the_skill_cites_is_shipped(self):
        """A doc named in an error message or SKILL.md has to be in the manifest."""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        shipped = set(manifest["files"])
        cited = set()
        for path in (SCRIPTS.glob("*.py")):
            for match in re.finditer(r"references/([a-z0-9-]+\.md)", path.read_text()):
                cited.add(f"references/{match.group(1)}")
        missing = sorted(cited - shipped)
        self.assertFalse(missing, f"cited in code but not shipped: {missing}")
