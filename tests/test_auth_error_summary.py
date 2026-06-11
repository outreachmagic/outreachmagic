"""Auth errors must be surfaced separately from generic errors in batch summary."""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "email-finder" / "scripts"))

from progress import print_final_summary  # noqa: E402


def test_final_summary_shows_auth_errors_line():
    buf = io.StringIO()
    stats = {"found": 4, "not_found": 0, "errors": 0, "auth_errors": 2, "credits_used": 4}
    print_final_summary(stats, 12.5, "/tmp/out", file=buf)
    out = buf.getvalue()
    assert "Auth errors:" in out
    assert "2" in out
