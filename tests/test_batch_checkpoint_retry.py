"""Tests for batch-find checkpoint retry semantics."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EF_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"
sys.path.insert(0, str(EF_SCRIPTS))

from batch_runner import (  # noqa: E402
    IncrementalWriter,
    checkpoint_row_is_complete,
    count_checkpoint_errors,
)


def test_checkpoint_row_is_complete():
    assert checkpoint_row_is_complete({"status": "not_found"})
    assert checkpoint_row_is_complete({"status": "found", "email": "a@b.com"})
    assert not checkpoint_row_is_complete({"status": "error", "error": "dns"})
    assert not checkpoint_row_is_complete({"status": "auth_error"})


def test_retry_errors_requeues_failed_rows():
    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "batch-results")
        writer = IncrementalWriter(base)
        writer.append(
            {
                "resume_key": "k1",
                "lead_id": "1",
                "name": "A",
                "domain": "acme.com",
                "email": "",
                "validity": "",
                "error": "dns fail",
                "provider": "trykitt",
                "api_calls": 1,
                "status": "error",
                "icypeas_status": "",
                "timestamp": "2026-06-11T00:00:00Z",
            },
            "k1",
        )
        writer.finalize()

        resume = IncrementalWriter(base, retry_errors=False)
        assert "k1" in resume.done_keys
        assert "k1" in resume.error_keys

        retry = IncrementalWriter(base, retry_errors=True)
        assert "k1" not in retry.done_keys
        assert "k1" in retry.error_keys


def test_count_checkpoint_errors():
    rows = [
        {"status": "error"},
        {"status": "not_found"},
    ]
    assert count_checkpoint_errors(rows) == 1


def test_finalize_rewrites_json():
    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "out")
        writer = IncrementalWriter(base)
        writer.append(
            {
                "resume_key": "k1",
                "lead_id": "",
                "name": "A",
                "domain": "acme.com",
                "email": "",
                "validity": "",
                "error": "x",
                "provider": "",
                "api_calls": 0,
                "status": "error",
                "icypeas_status": "",
                "timestamp": "t",
            },
            "k1",
        )
        writer.append(
            {
                "resume_key": "k1",
                "lead_id": "",
                "name": "A",
                "domain": "acme.com",
                "email": "a@acme.com",
                "validity": "valid",
                "error": "",
                "provider": "trykitt",
                "api_calls": 1,
                "status": "found",
                "icypeas_status": "",
                "timestamp": "t2",
            },
            "k1",
        )
        writer.finalize()
        data = json.loads(Path(f"{base}.json").read_text())
        assert len(data) == 1
        assert data[0]["email"] == "a@acme.com"
