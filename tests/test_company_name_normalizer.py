"""Golden-output coverage for the company-name normalizer consolidation
(Stage C1). pipeline_dedup.normalize_company() and enrich.normalize_company_name()
used to be independent implementations; they now delegate to the single
canonical pipeline_utils.normalize_company_name(). These assertions lock down
the outputs that must NOT change across that swap (verified against the
pre-swap implementations by direct execution, not hand-derived), plus two
cases that are *expected* to change because the canonical normalizer strips a
strict superset of pipeline_dedup's original stopword list (adds
holdings/international/intl).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import enrich  # noqa: E402
import pipeline_dedup  # noqa: E402
import pipeline_utils  # noqa: E402


def test_pipeline_dedup_company_match_tier_is_stable():
    # (a, b, expected_tier) -- confirmed against the pre-swap implementation.
    pairs = [
        ("Acme Inc", "Acme Corporation", "exact"),
        ("Acme Technologies", "Acme Tech", "similar"),
        ("University of Springfield", "Springfield", "exact"),
        ("Smith & Partners LLC", "Smith Partners", "exact"),
        ("Totally Different Co", "Unrelated Group", None),
    ]
    for a, b, expected in pairs:
        assert pipeline_dedup.company_match_tier(a, b) == expected, (a, b)


def test_pipeline_dedup_holdings_international_upgrades_to_exact():
    """The one case that's supposed to change: canonical strips
    holdings/international (pipeline_dedup's original list didn't), so this
    pair moves from 'similar' (token overlap) to 'exact' (full normalized
    match) post-swap. Confirms the swap landed, not that nothing changed."""
    assert pipeline_dedup.company_match_tier("Acme Holdings", "Acme International") == "exact"


def test_enrich_companies_match_is_stable():
    # (expected, actual, result) -- confirmed against the pre-swap implementation.
    pairs = [
        ("Acme Inc", "Acme Corporation", True),
        ("Totally Different Co", "Unrelated Holdings Group", False),
        ("", "Acme", True),  # enrich's deliberate "can't verify -> True" default
        ("Acme Holdings", "Acme International", True),
        ("University of Springfield", "Springfield", True),
        ("Acme Technologies", "Acme Tech", True),
    ]
    for expected, actual, result in pairs:
        assert enrich.companies_match(expected, actual) == result, (expected, actual)


def test_canonical_normalizer_is_a_strict_superset_of_pipeline_dedups_stopwords():
    """Documents the one deliberate behavior expansion from consolidation."""
    assert pipeline_utils.normalize_company_name("Acme Holdings") == "acme"
    assert pipeline_utils.normalize_company_name("Acme International") == "acme"


def test_pipeline_dedup_normalize_company_delegates_to_canonical():
    assert pipeline_dedup.normalize_company("Acme Inc") == pipeline_utils.normalize_company_name("Acme Inc")
    assert pipeline_dedup.normalize_company("Acme Holdings") == pipeline_utils.normalize_company_name("Acme Holdings")


def test_enrich_normalize_company_name_delegates_to_canonical():
    assert enrich.normalize_company_name("Acme Inc") == pipeline_utils.normalize_company_name("Acme Inc")
