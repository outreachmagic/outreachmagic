#!/usr/bin/env python3
"""Every enumeration of sentiments must agree with the canonical one.

A value accepted on write and absent from a read-side list does not error --
it silently disappears. `neutral` did exactly that: valid in
pipeline_lead_review, the CLI's --sentiment filters, the GHL driver and the
Sheets export, missing from reporting_queries.SENTIMENT_KEYS, so a lead sitting
at neutral was counted nowhere on the Campaigns page while the spreadsheet
export showed it.

Same check release-check runs; here too so it fails in a normal test run
rather than only at the release gate.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_sentiment_values import sentiment_mismatches  # noqa: E402


def test_every_sentiment_enumeration_agrees():
    assert sentiment_mismatches() == []


def test_neutral_is_one_of_them():
    """Pinned by name: it has been dropped once already."""
    sys.path.insert(0, str(ROOT / "skills" / "outreachmagic" / "scripts"))
    import reporting_queries
    from pipeline_lead_review import LEAD_SENTIMENT_VALUES

    assert "neutral" in LEAD_SENTIMENT_VALUES
    assert "neutral" in reporting_queries.SENTIMENT_KEYS
