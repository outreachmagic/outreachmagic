"""Tests for batch-find checkpoint retry semantics."""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EF_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"
sys.path.insert(0, str(EF_SCRIPTS))

from batch_runner import (  # noqa: E402
    BATCH_CSV_COLUMNS,
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


def _seed_csv(path: str, rows: list[dict[str, str]], *, header: str | None = None) -> None:
    """Write a CSV with BATCH_CSV_COLUMNS (or a custom header) for test fixtures."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if header:
            writer.writerow([header])
        else:
            writer.writerow(BATCH_CSV_COLUMNS)
        for row in rows:
            writer.writerow([row.get(c, "") for c in BATCH_CSV_COLUMNS])


def test_load_existing_corrupted_header_preserves_data():
    """When the CSV header is corrupted (e.g. by an external editor), data rows
    should still be loaded instead of renaming the file and losing them."""
    rows = [
        {"resume_key": "k1", "lead_id": "1", "name": "Alice", "domain": "acme.com",
         "email": "a@acme.com", "validity": "valid", "error": "",
         "provider": "trykitt", "api_calls": "1", "status": "found",
         "icypeas_status": "", "timestamp": "t1"},
        {"resume_key": "k2", "lead_id": "2", "name": "Bob", "domain": "bobco.com",
         "email": "", "validity": "", "error": "",
         "provider": "trykitt", "api_calls": "1", "status": "not_found",
         "icypeas_status": "", "timestamp": "t2"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "corrupted")
        csv_path = f"{base}.csv"

        # Simulate an editor that writes a BOM or alters the first column header
        _seed_csv(csv_path, rows, header="\ufeffresume_key,lead_id,name")

        writer = IncrementalWriter(base)
        assert len(writer.buffer) == 2
        assert writer.buffer[0]["resume_key"] == "k1"
        assert writer.buffer[0]["email"] == "a@acme.com"
        assert writer.buffer[1]["resume_key"] == "k2"
        assert writer.buffer[1]["status"] == "not_found"
        assert "k1" in writer.done_keys
        assert "k2" in writer.done_keys
        # Verify the original file was NOT renamed
        assert os.path.exists(csv_path)


def test_load_existing_missing_header_column():
    """When a different column replaces resume_key, data should still load
    without trashing the file."""
    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "mismatch")
        csv_path = f"{base}.csv"

        _seed_csv(csv_path, [
            {"resume_key": "k1", "lead_id": "1", "name": "Alice",
             "domain": "acme.com", "email": "a@acme.com",
             "validity": "", "error": "", "provider": "trykitt",
             "api_calls": "1", "status": "found", "icypeas_status": "",
             "timestamp": "t1"},
        ], header="id,name,email,status")

        writer = IncrementalWriter(base)
        assert len(writer.buffer) == 1
        assert writer.buffer[0]["resume_key"] == "k1"
        assert writer.buffer[0]["email"] == "a@acme.com"
        assert os.path.exists(csv_path)
