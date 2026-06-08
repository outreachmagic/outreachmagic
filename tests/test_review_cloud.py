"""Tests for hosted review API client."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import review_cloud  # noqa: E402


class ReviewCloudExportTests(unittest.TestCase):
    @patch("review_cloud._request_json")
    def test_lead_review_export_sends_headers_and_rows(self, mock_request):
        mock_request.return_value = {"sheet_id": "abc", "url": "https://example.com"}
        review_cloud.export_review(
            "https://app.outreachmagic.io",
            "token",
            template="lead-review",
            title="Popcam Review",
            detail="full",
            headers=["lead_id", "name", "email"],
            rows=[[1, "Teresa", "teresa@purdueglobal.edu"]],
            workspace="popcam",
            share_email="owner@example.com",
        )
        mock_request.assert_called_once()
        body = mock_request.call_args.kwargs["body"]
        self.assertEqual(body["template"], "lead-review")
        self.assertEqual(body["detail"], "full")
        self.assertEqual(body["workspace"], "popcam")
        self.assertEqual(body["headers"], ["lead_id", "name", "email"])
        self.assertEqual(body["rows"], [[1, "Teresa", "teresa@purdueglobal.edu"]])
        self.assertEqual(body["share_email"], "owner@example.com")
        self.assertNotIn("candidates", body)

    @patch("review_cloud._request_json")
    def test_dedup_review_export_sends_candidates(self, mock_request):
        mock_request.return_value = {"sheet_id": "abc"}
        candidates = [{"keep_id": 1, "merge_id": 2}]
        review_cloud.export_review(
            "https://app.outreachmagic.io",
            "token",
            template="dedup-review",
            title="Dedup",
            candidates=candidates,
        )
        body = mock_request.call_args.kwargs["body"]
        self.assertEqual(body["candidates"], candidates)
        self.assertNotIn("headers", body)
        self.assertNotIn("rows", body)


if __name__ == "__main__":
    unittest.main()
