"""Tests for MillionVerifier mv_attempted tagging."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EMAIL_SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
OM_SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(OM_SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import bounces  # noqa: E402


def _load_email_finder():
    spec = importlib.util.spec_from_file_location("email_finder_mv", EMAIL_SCRIPTS / "email_finder.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["email_finder_mv"] = mod
    spec.loader.exec_module(mod)
    return mod


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


class TestMvAttempted(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
        _reset_db()

    def test_config_includes_millionverifier(self):
        ef = _load_email_finder()
        with patch.dict("os.environ", {"MILLIONVERIFIER_API_KEY": "abcdefghijklmnop"}, clear=False):
            with patch.object(ef, "find_outreachmagic", return_value=None):
                import io
                from contextlib import redirect_stdout

                buf = io.StringIO()
                with redirect_stdout(buf):
                    ef.cmd_config()
                out = json.loads(buf.getvalue())
        self.assertTrue(out["millionverifier_api_key_set"])

    def test_verification_candidates_skips_mv_attempted_tag(self):
        ws = om.create_workspace("MV Test", slug="mv-ws")
        ws_id = f"ws_{ws['slug']}"
        conn = om.get_conn()
        conn.execute(
            "INSERT INTO leads (name, email, stage) VALUES ('Tagged', 'tagged@test.com', 'prospecting')"
        )
        lid = conn.execute("SELECT id FROM leads").fetchone()[0]
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lid)
        tag_id = f"wlt_{ws_id}_{lid}_mv000001"
        conn.execute(
            "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag) VALUES (?, ?, ?, ?)",
            (tag_id, ws_id, lid, "mv_attempted"),
        )
        conn.execute(
            "INSERT INTO leads (name, email, stage) VALUES ('Fresh', 'fresh@test.com', 'prospecting')"
        )
        lid2 = conn.execute("SELECT id FROM leads WHERE email = 'fresh@test.com'").fetchone()[0]
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lid2)
        conn.commit()
        conn.close()

        result = bounces.leads_needing_verification(
            ws["slug"],
            skip_mv_attempted_tag=True,
        )
        emails = {row["email"] for row in result.get("leads") or []}
        self.assertIn("fresh@test.com", emails)
        self.assertNotIn("tagged@test.com", emails)


if __name__ == "__main__":
    unittest.main()
