#!/usr/bin/env python3
"""Brand asset presence tests."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BRAND_LOGOS = (
    "logo-horizontal-dark.svg",
    "logo-horizontal-white.svg",
    "logo-icon-black.svg",
    "logo-icon-white.svg",
)


def test_brand_logos_present():
    logos = ROOT / "brand" / "logos"
    for name in BRAND_LOGOS:
        path = logos / name
        assert path.is_file(), name
        assert path.stat().st_size > 0, name
