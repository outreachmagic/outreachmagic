#!/usr/bin/env python3
"""
Post-import fixes for legacy Popcam batch imports (no re-import required).

1. source  — Re-apply list_source -> *_source, import_name -> *_source_detail from CSVs
2. sales-nav — Add linkedin_sales_nav_id identities for ACwAA-style /in/ slugs

Usage:
  # After batches 001-007 finished (dry-run first):
  python3 scripts/ops/legacy_import_post_fix.py source \\
    --batch-dir ~/Downloads/popcam_incremental_batches \\
    --to-batch 7 --dry-run

  python3 scripts/ops/legacy_import_post_fix.py sales-nav --dry-run
  python3 scripts/ops/legacy_import_post_fix.py all --batch-dir ... --to-batch 7
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()
_cursor_scripts = HOME / ".cursor" / "skills" / "outreachmagic" / "scripts"
_hermes_scripts = HOME / ".hermes" / "skills" / "outreachmagic" / "scripts"

SCRIPTS = (
    ROOT / "skills" / "outreachmagic" / "scripts"
    if (ROOT / "skills" / "outreachmagic" / "scripts" / "pipeline.py").exists()
    else (_cursor_scripts if (_cursor_scripts / "pipeline.py").exists() else _hermes_scripts)
)
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    extract_sales_nav_id_from_linkedin_url,
    upsert_identity_alias,
)

IMPORT_BATCH = "legacy-may-2026"
SOURCE = "legacy"
SOURCE_DETAIL = "legacy_export_may_2026"


def _strip(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def row_source_fields(row: dict) -> tuple[str, str]:
    return (
        _strip(row, "list_source") or SOURCE,
        _strip(row, "import_name") or SOURCE_DETAIL,
    )


def _batch_files(batch_dir: Path, from_batch: int, to_batch: int) -> list[Path]:
    files = sorted(batch_dir.glob("popcam_incremental_batch_*.csv"))
    out: list[Path] = []
    for f in files:
        stem = f.stem  # popcam_incremental_batch_007
        try:
            num = int(stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if from_batch <= num <= to_batch:
            out.append(f)
    return out


def backfill_source_from_csvs(
    *,
    batch_dir: Path,
    from_batch: int,
    to_batch: int,
    dry_run: bool,
) -> dict:
    summary = {
        "files": 0,
        "rows": 0,
        "matched": 0,
        "updated": 0,
        "skipped_no_match": 0,
        "samples": [],
    }
    files = _batch_files(batch_dir, from_batch, to_batch)
    if not files:
        summary["error"] = f"no batch files in {batch_dir} for range {from_batch}-{to_batch}"
        return summary

    conn = None if dry_run else om.get_conn()

    for path in files:
        summary["files"] += 1
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            summary["rows"] += 1
            lead_source, lead_source_detail = row_source_fields(row)
            profile = om.normalize_profile_row(row)
            extra = om._extract_extra_import_fields(row)
            idents = om.build_import_identities(
                profile,
                extra,
                import_batch=IMPORT_BATCH,
                company_domain=extra.get("company_domain"),
            )
            if not idents:
                summary["skipped_no_match"] += 1
                continue
            match = om.resolve_lead(
                name=profile.get("name") or "Unknown",
                identities=idents,
                dry_run=True,
            )
            if match.get("status") != "matched" or not match.get("id"):
                summary["skipped_no_match"] += 1
                continue
            lead_id = int(match["id"])
            summary["matched"] += 1
            if dry_run:
                if len(summary["samples"]) < 5:
                    summary["samples"].append(
                        {
                            "lead_id": lead_id,
                            "source": lead_source,
                            "source_detail": lead_source_detail,
                            "file": path.name,
                        }
                    )
                continue
            conn.execute(
                """UPDATE leads
                   SET original_source = ?,
                       original_source_detail = ?,
                       latest_source = ?,
                       latest_source_detail = ?,
                       latest_source_platform = 'csv',
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (lead_source, lead_source_detail, lead_source, lead_source_detail, lead_id),
            )
            summary["updated"] += 1

    if conn is not None:
        conn.commit()
        conn.close()
    return summary


def backfill_sales_nav_ids(*, dry_run: bool, limit: int | None) -> dict:
    summary = {
        "candidates": 0,
        "would_add": 0,
        "added": 0,
        "already_had": 0,
        "conflicts": 0,
        "samples": [],
    }
    conn = om.get_conn()
    rows = conn.execute(
        """SELECT id AS lead_id, linkedin_url AS value, 'leads' AS src
           FROM leads
           WHERE linkedin_url IS NOT NULL
             AND lower(linkedin_url) LIKE 'linkedin.com/in/acwaa%'
           UNION
           SELECT lead_id, identity_value_normalized, 'identity'
           FROM lead_identities
           WHERE identity_type = 'linkedin_url'
             AND lower(identity_value_normalized) LIKE 'linkedin.com/in/acwaa%'"""
    ).fetchall()
    seen_leads: set[int] = set()
    candidates: list[tuple[int, str]] = []
    for r in rows:
        lid = int(r["lead_id"])
        if lid in seen_leads:
            continue
        seen_leads.add(lid)
        candidates.append((lid, r["value"]))

    if limit is not None:
        candidates = candidates[:limit]

    summary["candidates"] = len(candidates)

    for lead_id, linkedin_value in candidates:
        sales_id = extract_sales_nav_id_from_linkedin_url(linkedin_value)
        if not sales_id:
            continue
        existing = conn.execute(
            """SELECT lead_id FROM lead_identities
               WHERE org_id = ? AND identity_type = 'linkedin_sales_nav_id'
                 AND identity_value_normalized = ?""",
            (DEFAULT_ORG_ID, sales_id),
        ).fetchone()
        if existing and int(existing["lead_id"]) == lead_id:
            summary["already_had"] += 1
            continue
        if existing and int(existing["lead_id"]) != lead_id:
            summary["conflicts"] += 1
            continue
        if dry_run:
            summary["would_add"] += 1
            if len(summary["samples"]) < 5:
                summary["samples"].append(
                    {"lead_id": lead_id, "linkedin_url": linkedin_value, "sales_nav_id": sales_id}
                )
            continue
        try:
            upsert_identity_alias(
                conn, DEFAULT_ORG_ID, lead_id, "linkedin_sales_nav_id", sales_id,
                source="legacy_post_fix",
            )
            summary["added"] += 1
        except ValueError:
            summary["conflicts"] += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-import legacy fixes (no re-import)")
    parser.add_argument(
        "fix",
        choices=("source", "sales-nav", "all"),
        help="Which fix to run",
    )
    parser.add_argument(
        "--batch-dir",
        type=Path,
        default=Path.home() / "Downloads" / "popcam_incremental_batches",
        help="Directory with popcam_incremental_batch_*.csv files",
    )
    parser.add_argument("--from-batch", type=int, default=1)
    parser.add_argument("--to-batch", type=int, default=7, help="Backfill source up to this batch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Limit sales-nav candidates (testing)")
    args = parser.parse_args()

    om.init_db()
    out: dict = {"dry_run": args.dry_run}

    if args.fix in ("source", "all"):
        out["source"] = backfill_source_from_csvs(
            batch_dir=args.batch_dir.expanduser(),
            from_batch=args.from_batch,
            to_batch=args.to_batch,
            dry_run=args.dry_run,
        )
    if args.fix in ("sales-nav", "all"):
        out["sales_nav"] = backfill_sales_nav_ids(dry_run=args.dry_run, limit=args.limit)

    print(json.dumps(out, indent=2))
    if not args.dry_run:
        print(
            "\nDone. Run sync when ready:\n"
            f"  python3 {SCRIPTS / 'pipeline.py'} sync",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
