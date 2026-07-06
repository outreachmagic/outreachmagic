"""Tests for auto-chunking lead-review sheets exports over 1000 rows
(verify-bulk-save-bugs.md feature request)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline_cli  # noqa: E402


def _row(n):
    return [n, f"Lead {n}"]


class ChunkedLeadReviewExportTests(unittest.TestCase):
    @patch("pipeline_cli.review_cloud.export_review")
    def test_small_export_is_a_single_call(self, mock_export):
        mock_export.return_value = {"sheet_id": "s1", "url": "https://example.com/s1"}
        rows = [_row(i) for i in range(5)]
        result = pipeline_cli._export_lead_review_chunked(
            "https://app.outreachmagic.io", "tok",
            template="lead-review", title="t", share_email=None, public_link=False,
            sheet_id=None, parent_sheet_id=None, tab_title=None,
            detail="standard", headers=["id", "name"], rows=rows,
            workspace="ws", columns=None, freeze_header=True,
        )
        mock_export.assert_called_once()
        self.assertEqual(result["sheet_id"], "s1")
        self.assertNotIn("chunked", result)

    @patch("pipeline_cli.review_cloud.export_review")
    def test_large_export_splits_into_multiple_tabs(self, mock_export):
        mock_export.return_value = {"sheet_id": "s1", "url": "https://example.com/s1"}
        rows = [_row(i) for i in range(2500)]
        result = pipeline_cli._export_lead_review_chunked(
            "https://app.outreachmagic.io", "tok",
            template="lead-review", title="t", share_email="a@b.com", public_link=False,
            sheet_id=None, parent_sheet_id=None, tab_title=None,
            detail="standard", headers=["id", "name"], rows=rows,
            workspace="ws", columns=None, freeze_header=True,
        )
        self.assertEqual(mock_export.call_count, 3)
        self.assertTrue(result["chunked"])
        self.assertEqual(result["rows"], 2500)
        self.assertEqual(len(result["tabs"]), 3)

        first_call = mock_export.call_args_list[0].kwargs
        self.assertIsNone(first_call["parent_sheet_id"])
        self.assertEqual(first_call["share_email"], "a@b.com")
        self.assertEqual(len(first_call["rows"]), 1000)

        second_call = mock_export.call_args_list[1].kwargs
        self.assertEqual(second_call["parent_sheet_id"], "s1")
        self.assertIsNone(second_call["share_email"])
        self.assertEqual(second_call["tab_title"], "Page 2")
        self.assertEqual(len(second_call["rows"]), 1000)

        third_call = mock_export.call_args_list[2].kwargs
        self.assertEqual(third_call["parent_sheet_id"], "s1")
        self.assertEqual(third_call["tab_title"], "Page 3")
        self.assertEqual(len(third_call["rows"]), 500)

    @patch("pipeline_cli.review_cloud.export_review")
    def test_explicit_parent_sheet_id_bypasses_chunking(self, mock_export):
        """A caller who already targets a specific sheet/tab keeps today's single-call behavior."""
        mock_export.return_value = {"sheet_id": "existing", "url": "https://example.com/existing"}
        rows = [_row(i) for i in range(2500)]
        pipeline_cli._export_lead_review_chunked(
            "https://app.outreachmagic.io", "tok",
            template="lead-review", title="t", share_email=None, public_link=False,
            sheet_id=None, parent_sheet_id="existing", tab_title="Custom Tab",
            detail="standard", headers=["id", "name"], rows=rows,
            workspace="ws", columns=None, freeze_header=True,
        )
        mock_export.assert_called_once()
        self.assertEqual(mock_export.call_args.kwargs["rows"], rows)


if __name__ == "__main__":
    unittest.main()
