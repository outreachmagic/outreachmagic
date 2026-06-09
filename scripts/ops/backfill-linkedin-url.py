#!/usr/bin/env python3
"""Backfill leads.linkedin_url from public linkedin_url identities (skip Sales Nav hashes)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    linkedin_url_is_hash,
    promote_linkedin_url_from_identities,
)


def backfill(*, source_detail: str | None, dry_run: bool, limit: int | None) -> dict:
    conn = om.get_conn()
    query = "SELECT id FROM leads WHERE 1=1"
    params: list = []
    if source_detail:
        query += " AND original_source_detail = ?"
        params.append(source_detail)
    query += " ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()

    stats = {
        "scanned": len(rows),
        "promoted": 0,
        "already_ok": 0,
        "no_identity": 0,
        "linkedin_url_conflicts": [],
    }
    for row in rows:
        lead_id = int(row["id"])
        cur = conn.execute(
            "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        current = cur["linkedin_url"] if cur else None
        if current and not linkedin_url_is_hash(current):
            stats["already_ok"] += 1
            continue
        if dry_run:
            ident = conn.execute(
                """SELECT identity_value_normalized FROM lead_identities
                   WHERE org_id = ? AND lead_id = ? AND identity_type = 'linkedin_url'
                     AND identity_value_normalized NOT LIKE 'linkedin.com/in/acwaa%'
                   LIMIT 1""",
                (DEFAULT_ORG_ID, lead_id),
            ).fetchone()
            if ident:
                stats["promoted"] += 1
            else:
                stats["no_identity"] += 1
            continue
        conflict = promote_linkedin_url_from_identities(conn, DEFAULT_ORG_ID, lead_id)
        if conflict:
            stats["linkedin_url_conflicts"].append({"lead_id": lead_id, **conflict})
        elif conn.execute(
            "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()["linkedin_url"]:
            stats["promoted"] += 1
        else:
            stats["no_identity"] += 1
    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-detail", help="Filter by leads.original_source_detail")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    print(json.dumps(backfill(
        source_detail=args.source_detail,
        dry_run=args.dry_run,
        limit=args.limit,
    ), indent=2))


if __name__ == "__main__":
    main()
