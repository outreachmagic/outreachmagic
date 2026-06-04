"""Tests for import-profiles --source and companion source propagation."""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
EMAIL_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"
LE_SCRIPTS = ROOT / "skills" / "lead-enrich" / "scripts"


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
om = _load_module("om_pipeline", SCRIPTS / "pipeline.py")
ef_cc = _load_module("ef_companion_common", EMAIL_SCRIPTS / "companion_common.py")
from batch_runner import build_import_profile  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _lead_source(lead_id: int) -> Optional[str]:
    conn = om.get_conn()
    row = conn.execute(
        "SELECT original_source FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    return row["original_source"] if row else None


@pytest.fixture(autouse=True)
def fresh_db():
    _reset_db()
    yield


def test_import_profiles_source_param_sets_original_source():
    summary = om.import_profiles(
        [{"email": "src@acme.com", "name": "Source Test", "company": "Acme"}],
        source="sales_navigator",
        source_detail="Test list",
    )
    assert summary["created"] == 1
    lead_id = summary["results"][0]["id"]
    assert _lead_source(lead_id) == "sales_navigator"


def test_import_profiles_default_source_is_csv_import():
    summary = om.import_profiles(
        [{"email": "def@acme.com", "name": "Default Test", "company": "Acme"}],
        source_detail="detail only",
    )
    lead_id = summary["results"][0]["id"]
    assert _lead_source(lead_id) == "csv_import"


def test_import_profiles_row_list_source_overrides_cli_default():
    summary = om.import_profiles(
        [{
            "email": "row@acme.com",
            "name": "Row Override",
            "company": "Acme",
            "list_source": "icypeas",
        }],
        source="trykitt",
        source_detail="batch",
    )
    lead_id = summary["results"][0]["id"]
    assert _lead_source(lead_id) == "icypeas"


def test_import_profiles_cli_source_flag(tmp_path):
    payload = json.dumps([{
        "email": "cli@acme.com",
        "name": "CLI Source",
        "company": "Acme",
    }])
    env = {**os.environ, "OUTREACHMAGIC_DATA_ROOT": str(tmp_path)}
    init = subprocess.run(
        [sys.executable, str(SCRIPTS / "pipeline.py"), "init"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert init.returncode == 0, init.stderr
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "pipeline.py"),
            "import-profiles",
            "--json",
            payload,
            "--source",
            "lead_enrich",
            "--source-detail",
            "cli-test",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    lead_id = out["results"][0]["id"]
    db_path = tmp_path / "skills" / "outreachmagic" / "databases" / "outreachmagic.db"
    conn = __import__("sqlite3").connect(db_path)
    row = conn.execute(
        "SELECT original_source FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "lead_enrich"


class TestEmailFinderSource(unittest.TestCase):
    @patch.object(ef_cc, "_run_subprocess_json")
    @patch.object(ef_cc, "_append_json_or_file")
    def test_run_import_profiles_passes_source_flag(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"processed": 1, "matched": 0, "enriched": 0, "created": 1, "results": []}
        ef_cc.run_import_profiles(
            Path("/tmp/om"),
            [{"name": "A", "email": "a@acme.com"}],
            source="icypeas",
            source_detail="email-finder/icypeas",
        )
        cmd = mock_run.call_args[0][0]
        self.assertIn("--source", cmd)
        self.assertEqual(cmd[cmd.index("--source") + 1], "icypeas")

    def test_batch_import_results_passes_provider(self):
        email_finder = _load_module("ef_email_finder", EMAIL_SCRIPTS / "email_finder.py")
        with patch.object(email_finder.cc, "run_import_profiles") as mock_import:
            mock_import.return_value = {"results": [{"id": 1}]}
            email_finder.batch_import_results(
                Path("/tmp/om"),
                [{"name": "Jane", "email": "j@acme.com"}],
                source="trykitt",
                source_detail="email-finder/trykitt",
            )
            mock_import.assert_called_once()
            self.assertEqual(mock_import.call_args.kwargs.get("source"), "trykitt")

    def test_save_find_result_passes_provider(self):
        email_finder = _load_module("ef_email_finder_save", EMAIL_SCRIPTS / "email_finder.py")
        with patch.object(email_finder.cc, "run_import_profiles") as mock_import, patch.object(
            email_finder.cc, "run_verify_email_batch",
        ):
            mock_import.return_value = {"results": [{"id": 42}]}
            email_finder.save_find_result(
                Path("/tmp/om"),
                full_name="Jane",
                company="Acme",
                domain="acme.com",
                linkedin="",
                find_result={"email": "j@acme.com", "provider": "icypeas", "validity": "valid"},
            )
            mock_import.assert_called_once()
            self.assertEqual(mock_import.call_args.kwargs.get("source"), "icypeas")

    def test_build_import_profile_sets_list_source(self):
        profile = build_import_profile(
            full_name="Jane",
            company="Acme",
            domain="acme.com",
            linkedin="",
            find_result={"email": "j@acme.com", "provider": "trykitt", "validity": "valid"},
            normalize_linkedin_fn=lambda x: x,
        )
        self.assertEqual(profile["list_source"], "trykitt")


class TestLeadEnrichSource(unittest.TestCase):
    def test_lead_enrich_companion_passes_source(self):
        le_cc = _load_module("le_companion_common_test", LE_SCRIPTS / "companion_common.py")
        with patch.object(le_cc, "_run_subprocess_json") as mock_run, patch.object(
            le_cc, "_append_json_or_file",
        ) as mock_append:
            mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
            mock_run.return_value = {"processed": 1, "matched": 0, "enriched": 0, "created": 0, "results": []}
            le_cc.run_import_profiles(
                Path("/tmp/om"),
                [{"name": "A", "linkedin": "linkedin.com/in/a"}],
                source="lead_enrich",
                source_detail="lead-enrich",
            )
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[cmd.index("--source") + 1], "lead_enrich")

    def test_build_import_command_includes_source(self):
        enrich = _load_module("le_enrich_cmd", LE_SCRIPTS / "enrich.py")
        cmd = enrich._build_import_command(
            {"name": "Jane", "linkedin": "linkedin.com/in/jane"},
            "ws1",
            "",
        )
        self.assertIn("--source lead_enrich", cmd)


if __name__ == "__main__":
    unittest.main()
