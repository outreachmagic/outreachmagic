#!/usr/bin/env python3
"""Every list of sentiment values must agree with the canonical one.

A sentiment accepted on write and missing from a read-side enumeration does not
error -- it silently disappears. `neutral` did exactly that: valid in
pipeline_lead_review, the CLI's --sentiment filters, the GHL driver and the
Sheets export, absent from reporting_queries.SENTIMENT_KEYS, and therefore
counted nowhere on the Campaigns page while the spreadsheet showed it.

The canonical set is pipeline_lead_review.LEAD_SENTIMENT_VALUES.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def sentiment_mismatches() -> list[str]:
    import campaign_stats
    import pipeline_cli  # noqa: F401  - imported for the parser definitions
    import reporting_queries
    from crm_drivers.ghl import GhlDriver
    from pipeline_lead_review import LEAD_SENTIMENT_VALUES

    canonical = set(LEAD_SENTIMENT_VALUES)
    problems = []

    def compare(label, values):
        missing = canonical - set(values)
        extra = set(values) - canonical
        if missing:
            problems.append(f"{label} is missing {sorted(missing)}")
        if extra:
            problems.append(f"{label} has unknown value(s) {sorted(extra)}")

    compare("reporting_queries.SENTIMENT_KEYS", reporting_queries.SENTIMENT_KEYS)
    compare("crm_drivers.ghl.GhlDriver.SENTIMENT_VALUES", GhlDriver.SENTIMENT_VALUES)

    # campaign_stats spells its order inline for the Sheets export header.
    source = (SCRIPTS / "campaign_stats.py").read_text(encoding="utf-8")
    for marker in ("sentiment_order = [", "rs.sentiment IN ("):
        if marker not in source:
            problems.append(f"campaign_stats.py no longer contains {marker!r}")
            continue
        chunk = source.split(marker, 1)[1].split("]" if "[" in marker else ")", 1)[0]
        listed = {piece.strip().strip("'\"") for piece in chunk.split(",")}
        compare(f"campaign_stats.py {marker.strip('( =[')}", {v for v in listed if v})

    del campaign_stats
    return problems


def main() -> int:
    problems = sentiment_mismatches()
    if problems:
        print("FAIL: sentiment enumerations disagree with "
              "pipeline_lead_review.LEAD_SENTIMENT_VALUES", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("  canonical values agree: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
