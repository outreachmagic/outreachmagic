"""Two more trykitt debug-report fixes exercised through a real run_batch()
call (same mocking pattern as TestBatchFind.test_batch_single_import in
test_email_finder.py): --max clipping instead of hard-erroring on an
oversized input file, and the one-shot auto-retry when the OM auto-import
step fails."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import email_finder as lemail  # noqa: E402
import normalize as norm  # noqa: E402
from batch_runner import BatchOptions, run_batch  # noqa: E402

_CFG = {
    "max_people_per_run": 500,
    "trykitt_enabled": True,
    "icypeas_enabled": False,
    "trykitt_api_key": "testkey1234567890123456789012",
}


def _run(people, opts, tmp_path, *, save_side_effect=None, save_return=None):
    inp = tmp_path / "batch.json"
    inp.write_text(json.dumps(people))
    om = Path("/tmp/om")
    with patch.object(lemail, "find_outreachmagic", return_value=om), \
         patch("batch_runner.run_health_check", return_value=(True, [], [])), \
         patch("batch_runner.run_find_with_fallback") as mock_find, \
         patch.object(lemail.cc, "run_batch_lead_lookup") as mock_lookup, \
         patch.object(lemail.cc, "save_email_find_profiles") as mock_save:
        mock_lookup.return_value = {
            "results": [{"index": i, "status": "not_found"} for i in range(len(people))],
        }
        mock_find.return_value = {
            "status": "found", "email": "a@acme.com", "validity": "valid", "provider": "trykitt",
        }
        if save_side_effect is not None:
            mock_save.side_effect = save_side_effect
        else:
            mock_save.return_value = save_return or {
                "mode": "apply_email_find_results", "recorded": 1,
                "results": [{"lead_id": i} for i in range(len(people))],
            }
        result = run_batch(
            str(inp), _CFG, om, opts,
            skill_dir=lemail._find_skill_dir(),
            normalize_linkedin_fn=norm.normalize_linkedin,
            key_status_fn=lemail.cc.outreachmagic_agent_key_status,
        )
        return result, mock_save


def test_max_clips_instead_of_erroring(tmp_path):
    people = [{"lead_id": i, "name": f"Person {i}", "domain": "acme.com"} for i in range(5)]
    opts = BatchOptions(
        yes=True, skip_om=True, workers=1, delay=0, max_leads=3,
        output_base=str(tmp_path / "out"),
    )
    result, _ = _run(people, opts, tmp_path)
    assert "error" not in result
    assert result["stats"]["found"] == 3


def test_max_default_unaffected_when_under_limit(tmp_path):
    people = [{"lead_id": i, "name": f"Person {i}", "domain": "acme.com"} for i in range(2)]
    opts = BatchOptions(
        yes=True, skip_om=True, workers=1, delay=0, max_leads=500,
        output_base=str(tmp_path / "out"),
    )
    result, _ = _run(people, opts, tmp_path)
    assert "error" not in result
    assert result["stats"]["found"] == 2


def test_auto_import_retries_once_then_succeeds(tmp_path):
    people = [{"lead_id": 1, "name": "A", "domain": "acme.com"}]
    opts = BatchOptions(
        yes=True, skip_om=False, workspace="ws1", workers=1, delay=0,
        output_base=str(tmp_path / "out"),
    )
    ok_result = {
        "mode": "apply_email_find_results", "recorded": 1, "results": [{"lead_id": 1}],
    }
    result, mock_save = _run(
        people, opts, tmp_path,
        save_side_effect=[RuntimeError("sync returned non-JSON output"), ok_result],
    )
    assert mock_save.call_count == 2
    assert result["batch_save"]["retried"] is True
    assert result["batch_save"]["imported"] == 1
    assert "error" not in result["batch_save"]


def test_auto_import_falls_back_to_manual_hint_when_retry_also_fails(tmp_path):
    people = [{"lead_id": 1, "name": "A", "domain": "acme.com"}]
    opts = BatchOptions(
        yes=True, skip_om=False, workspace="ws1", workers=1, delay=0,
        output_base=str(tmp_path / "out"),
    )
    result, mock_save = _run(
        people, opts, tmp_path,
        save_side_effect=[RuntimeError("first failure"), RuntimeError("second failure")],
    )
    assert mock_save.call_count == 2
    assert result["batch_save"]["error"] == "second failure"
    assert result["batch_save"]["first_attempt_error"] == "first failure"
