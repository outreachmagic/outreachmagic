#!/usr/bin/env python3
"""Brand asset publish contract tests."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BRAND_LOGOS = (
    "logo-horizontal-dark.svg",
    "logo-horizontal-white.svg",
    "logo-icon-black.svg",
    "logo-icon-white.svg",
)


def test_skill_suite_declares_brand_public_repo():
    suite = json.loads((ROOT / "skill-suite.json").read_text(encoding="utf-8"))
    assert suite["brand"]["public_repo"] == "outreachmagic/brand"
    assert suite["brand"]["path"] == "brand"


def test_brand_logos_present():
    logos = ROOT / "brand" / "logos"
    for name in BRAND_LOGOS:
        path = logos / name
        assert path.is_file(), name
        assert path.stat().st_size > 0, name


def test_publish_brand_workflow_stages_logos():
    text = (ROOT / ".github/workflows/publish-brand.yml").read_text(encoding="utf-8")
    assert "outreachmagic/brand" in text
    assert "brand/logos" in text
    assert "PUBLISH_TOKEN" in text
