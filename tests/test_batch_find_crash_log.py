"""Crash/exit capture for batch-find (trykitt debug report section 2): when
the process disappears mid-batch there was previously no exit code, no
stderr dump, no crash reason on disk at all. cmd_batch_find() now writes a
timestamped crash-log entry (unhandled exception, or SIGTERM) to a sidecar
file next to the checkpoint output before propagating/exiting."""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import email_finder  # noqa: E402
from batch_runner import BatchOptions  # noqa: E402


def test_crash_log_path_uses_output_base_when_set(tmp_path):
    opts = BatchOptions(output_base=str(tmp_path / "run1"))
    assert email_finder._crash_log_path("input.json", opts) == str(tmp_path / "run1") + ".crash.log"


def test_crash_log_path_falls_back_to_input_path(tmp_path):
    opts = BatchOptions(output_base="")
    input_path = str(tmp_path / "leads.json")
    assert email_finder._crash_log_path(input_path, opts) == input_path + ".crash.log"


def test_write_crash_log_appends_timestamped_entry(tmp_path):
    opts = BatchOptions(output_base=str(tmp_path / "run1"))
    email_finder._write_crash_log("input.json", opts, "Crashed: RuntimeError: boom")
    log_path = tmp_path / "run1.crash.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "Crashed: RuntimeError: boom" in content
    assert content.startswith("[")  # ISO-8601 timestamp prefix


def test_write_crash_log_never_raises_on_unwritable_path():
    opts = BatchOptions(output_base="/nonexistent-dir-xyz/run1")
    email_finder._write_crash_log("input.json", opts, "should not raise")  # must not raise


def test_cmd_batch_find_logs_unhandled_exception_and_reraises(tmp_path, monkeypatch, capsys):
    opts = BatchOptions(output_base=str(tmp_path / "run1"), skip_om=True)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(email_finder, "load_config", lambda: {})
    monkeypatch.setattr(email_finder, "run_batch", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        email_finder.cmd_batch_find(str(tmp_path / "input.json"), opts)

    log_path = tmp_path / "run1.crash.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "RuntimeError: simulated crash" in content
    assert "Traceback" in content


def test_cmd_batch_find_still_raises_json_error_for_value_error(tmp_path, monkeypatch, capsys):
    opts = BatchOptions(output_base=str(tmp_path / "run1"), skip_om=True)

    def _bad_input(*args, **kwargs):
        raise ValueError("bad input file")

    monkeypatch.setattr(email_finder, "load_config", lambda: {})
    monkeypatch.setattr(email_finder, "run_batch", _bad_input)

    with pytest.raises(SystemExit):
        email_finder.cmd_batch_find(str(tmp_path / "input.json"), opts)

    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "bad input file"
    # ValueError is an expected/handled input-validation error, not a crash --
    # it must not spam a crash log.
    assert not (tmp_path / "run1.crash.log").exists()
