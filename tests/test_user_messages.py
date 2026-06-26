"""Customer-facing copy helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))

from user_messages import metered_usage_label  # noqa: E402


def test_metered_usage_label_free_vs_paid():
    assert metered_usage_label("free") == "Webhook events"
    assert metered_usage_label("pro") == "Webhook and sync events"
    assert metered_usage_label("agency") == "Webhook and sync events"
