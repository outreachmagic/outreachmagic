"""Regression test for the update_skill() stale-module migration bug.

update_skill() overwrites this install's scripts on disk, then must migrate
the DB using the code that was *just written*, not whatever was cached in
sys.modules when this process started. Before the fix, the DB migration
ran in-process via a plain `from pipeline_migration import init_db; init_db()`
call, which silently reused the already-imported (pre-update) module -- see
`_migrate_db_in_subprocess` in pipeline_update.py for the full story.

This test proves the fix (spawning a fresh interpreter for the migration)
actually works by simulating the exact split that caused the bug: the test
process already has the *real* `pipeline_migration` module imported (with
no knowledge of a table that only exists in a "new version" of the code),
then asks update_skill() to install a patched copy of the scripts that adds
a new table to migrate_db(). Only a genuinely fresh interpreter reading the
new code off disk would ever create that table.
"""

import shutil
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"

sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_migration  # noqa: E402 -- forces the "old" module into sys.modules
from om_paths import get_db_path  # noqa: E402

MARKER_TABLE = "_regression_marker_new_migration_code"


class UpdateSkillSubprocessMigrationTests(unittest.TestCase):
    def test_migration_uses_freshly_copied_code_not_stale_cached_module(self):
        # Sanity check: the module already loaded in *this* process is the
        # real, unpatched source -- it has never heard of MARKER_TABLE.
        self.assertNotIn(MARKER_TABLE, Path(pipeline_migration.__file__).read_text())

        old_dest = Path(self.enterContext_tmp("old")) / "scripts"
        new_src = Path(self.enterContext_tmp("new")) / "scripts"
        shutil.copytree(SCRIPTS, old_dest)
        shutil.copytree(SCRIPTS, new_src)

        # Patch the "new version" to add a table migrate_db() doesn't
        # otherwise create -- standing in for a real schema change.
        migration_src = new_src / "pipeline_migration.py"
        patched = migration_src.read_text().replace(
            "def migrate_db(conn=None):\n"
            '    """Apply incremental schema changes and backfill derived data."""\n'
            "    own_conn = conn is None\n"
            "    if own_conn:\n"
            "        conn = get_conn()\n",
            "def migrate_db(conn=None):\n"
            '    """Apply incremental schema changes and backfill derived data."""\n'
            "    own_conn = conn is None\n"
            "    if own_conn:\n"
            "        conn = get_conn()\n"
            f"    conn.execute(\"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (id INTEGER)\")\n",
            1,
        )
        self.assertIn(MARKER_TABLE, patched, "test's own patch of migrate_db() didn't apply")
        migration_src.write_text(patched)

        with patch.object(om, "skill_scripts_dir", return_value=old_dest):
            with patch.object(om, "backup_scripts_for_rollback"):
                with patch.object(
                    om,
                    "resolve_update_source",
                    return_value=(new_src, "", str(new_src), "test"),
                ):
                    with patch.object(om, "sync_skill_md_version"):
                        with patch.object(om, "load_config", return_value={}):
                            with patch.object(om, "save_config"):
                                result = om.update_skill(channel="main")

        self.assertEqual(result["status"], "updated")

        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (MARKER_TABLE,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(
            row,
            "migrate_db() ran without the newly-copied code -- the migration "
            "subprocess must not have picked up the freshly-written scripts",
        )

    def enterContext_tmp(self, name: str) -> str:
        import tempfile

        tmp = tempfile.mkdtemp(prefix=f"om-update-test-{name}-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp


if __name__ == "__main__":
    unittest.main()
