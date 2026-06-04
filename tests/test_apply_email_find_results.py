"""Tests for apply-email-find-results fast path and companion routing."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
EMAIL_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(EMAIL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EMAIL_SCRIPTS))

om = _load_module("om_pipeline_aef", SCRIPTS / "pipeline.py")
ef_cc = _load_module("ef_cc_aef", EMAIL_SCRIPTS / "companion_common.py")


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    _reset_db()
    yield


def test_apply_email_find_results_updates_email_tags_and_verification():
    lead = om.resolve_lead(
        email=None,
        name="Fast Path",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    lead_id = int(lead["id"])
    om.create_workspace("Fast WS", slug="fastws")

    summary = om.apply_email_find_results(
        [{
            "id": lead_id,
            "email": "fast@acme.com",
            "tags": ["trykitt_attempted", "email_found"],
            "list_source": "trykitt",
            "notes": "trykitt valid",
            "_verify_provider": "trykitt",
            "_verify_validity": "valid",
        }],
        workspace="fastws",
        source="trykitt",
        source_detail="email-finder/batch",
    )
    assert summary["matched"] == 1
    assert summary["enriched"] == 1
    assert summary["tagged"] == 1
    assert summary["recorded"] == 1
    assert summary["mode"] == "apply_email_find_results"

    conn = om.get_conn()
    row = conn.execute(
        "SELECT email, latest_source FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    tag = conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ? AND tag = ?",
        (lead_id, "email_found"),
    ).fetchone()
    ver = conn.execute(
        "SELECT status, source FROM lead_email_verification WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["email"] == "fast@acme.com"
    assert row["latest_source"] == "trykitt"
    assert tag is not None
    assert ver["status"] == "valid"
    assert ver["source"] == "trykitt"


def test_import_rows_all_have_lead_id():
    assert om.import_rows_all_have_lead_id([{"id": 1}, {"lead_id": 2}])
    assert not om.import_rows_all_have_lead_id([{"id": 1}, {"name": "x"}])


class TestCompanionFastPath(unittest.TestCase):
    def test_chunk_timeout_200_leads(self):
        self.assertEqual(ef_cc._chunk_timeout(200), 100)

    def test_chunk_timeout_cap(self):
        self.assertEqual(ef_cc._chunk_timeout(1000), 300)

    @patch.object(ef_cc, "_run_subprocess_json")
    @patch.object(ef_cc, "_append_json_or_file")
    def test_save_email_find_profiles_uses_fast_apply_when_ids_and_workspace(
        self, mock_append, mock_run,
    ):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {
            "matched": 2,
            "enriched": 2,
            "recorded": 1,
            "mode": "apply_email_find_results",
            "results": [],
        }
        profiles = [{"id": 1, "email": "a@acme.com"}, {"id": 2, "email": "b@acme.com"}]
        ef_cc.save_email_find_profiles(
            Path("/tmp/om"),
            profiles,
            workspace="ws1",
            source="trykitt",
        )
        cmd = mock_run.call_args[0][0]
        self.assertIn("apply-email-find-results", cmd)
        self.assertNotIn("import-profiles", cmd)

    @patch.object(ef_cc, "_run_subprocess_json")
    @patch.object(ef_cc, "_append_json_or_file")
    def test_run_import_profiles_never_uses_fast_apply(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"matched": 1, "results": []}
        profiles = [{"id": 1, "email": "a@acme.com"}]
        ef_cc.run_import_profiles(Path("/tmp/om"), profiles, workspace="ws1", source="trykitt")
        cmd = mock_run.call_args[0][0]
        self.assertIn("import-profiles", cmd)
        self.assertNotIn("apply-email-find-results", cmd)

    @patch.object(ef_cc.subprocess, "run")
    def test_subprocess_timeout_becomes_runtime_error(self, mock_run):
        mock_run.side_effect = ef_cc.subprocess.TimeoutExpired(
            cmd=["python", "pipeline.py", "import-profiles"],
            timeout=60,
        )
        with self.assertRaises(RuntimeError) as ctx:
            ef_cc._run_subprocess_json(
                ["python", "pipeline.py", "import-profiles"],
                temp_path=None,
                timeout=60,
                skill_dir=None,
            )
        self.assertIn("timed out after 60s", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
