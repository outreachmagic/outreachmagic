#!/usr/bin/env python3
"""
One-time legacy import: Popcam May 2026 export (388 leads).

Imports profiles via import-profiles, then materializes activity summary fields
(last_contacted, email/linkedin sent counts, replies) for cross-platform sync.

Usage:
  python3 scripts/legacy_import_may2026.py \\
    --file "/path/to/export.csv" \\
    --workspace popcam \\
    --sender-profile "https://linkedin.com/in/YOUR_PROFILE" \\
    --dry-run

  python3 scripts/legacy_import_may2026.py --file export.csv --workspace popcam --sender-profile "..." 
  python3 skills/outreachmagic/scripts/pipeline.py sync
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402

IMPORT_BATCH = "legacy-may-2026"
SOURCE_DETAIL = "legacy_export_may_2026"


def _int_field(row: dict, key: str) -> int:
    raw = (row.get(key) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def _strip(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def apply_activity_for_row(
    row: dict,
    *,
    workspace_id: str,
    dry_run: bool,
) -> dict:
    email = _strip(row, "email").lower()
    if not email:
        return {"status": "skipped", "reason": "no_email"}

    lead = om.find_lead(email=email)
    if not lead:
        return {"status": "skipped", "reason": "lead_not_found", "email": email}

    lead_id = lead["id"]
    last_contacted = _strip(row, "last_contacted") or None
    email_sent = _int_field(row, "email_sent")
    linkedin_sent = _int_field(row, "linkedin_sent")
    total_replies = _int_field(row, "total_replies")

    if not any([last_contacted, email_sent, linkedin_sent, total_replies]):
        return {"status": "skipped", "reason": "no_activity", "email": email, "lead_id": lead_id}

    if dry_run:
        return {
            "status": "dry_run",
            "email": email,
            "lead_id": lead_id,
            "last_contacted_at": last_contacted,
            "email_sent_count": email_sent,
            "linkedin_sent_count": linkedin_sent,
            "total_replies_count": total_replies,
        }

    summary = om.set_lead_activity_summary(
        lead_id,
        workspace_id,
        last_contacted_at=last_contacted,
        email_sent_count=email_sent,
        linkedin_sent_count=linkedin_sent,
        total_replies_count=total_replies,
        merge=False,
        mark_cloud_pending=True,
    )
    return {"status": "updated", "email": email, "lead_id": lead_id, "activity": summary}


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy Popcam May 2026 CSV import")
    parser.add_argument("--file", required=True, help="Path to CSV export")
    parser.add_argument("--workspace", required=True, help="Workspace slug (e.g. popcam)")
    parser.add_argument(
        "--sender-profile",
        help="LinkedIn sender URL for is_connected_linkedin columns",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument(
        "--skip-profiles",
        action="store_true",
        help="Skip import-profiles (activity only)",
    )
    args = parser.parse_args()

    csv_path = Path(args.file).expanduser()
    if not csv_path.is_file():
        print(json.dumps({"error": f"file not found: {csv_path}"}))
        return 1

    om.init_db()
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, args.workspace)
    conn.close()
    if not ws:
        print(json.dumps({"error": f"workspace not found: {args.workspace}"}))
        return 1
    workspace_id = ws["id"]

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    summary: dict = {
        "file": str(csv_path),
        "workspace": args.workspace,
        "rows": len(rows),
        "dry_run": args.dry_run,
        "import_batch": IMPORT_BATCH,
    }

    if not args.skip_profiles:
        if args.dry_run:
            summary["import_profiles"] = {"dry_run": True, "would_process": len(rows)}
        else:
            imp = om.import_profiles(
                rows,
                workspace=args.workspace,
                sender_profile=args.sender_profile,
                source_detail=SOURCE_DETAIL,
                import_batch_id=IMPORT_BATCH,
            )
            summary["import_profiles"] = {
                "processed": imp.get("processed"),
                "created": imp.get("created"),
                "matched": imp.get("matched"),
                "enriched": imp.get("enriched"),
                "errors": len(imp.get("errors") or []),
            }

    activity_results = []
    updated = skipped = 0
    for row in rows:
        result = apply_activity_for_row(row, workspace_id=workspace_id, dry_run=args.dry_run)
        activity_results.append(result)
        if result.get("status") in ("updated", "dry_run"):
            updated += 1
        elif result.get("status") == "skipped":
            skipped += 1

    if not args.dry_run and not args.skip_profiles:
        ver_batch = []
        for row in rows:
            email = _strip(row, "email").lower()
            status = _strip(row, "email_verify_result") or "valid"
            lead = om.find_lead(email=email)
            if lead:
                ver_batch.append({"lead_id": lead["id"], "status": status, "source": "legacy_import"})
        if ver_batch:
            summary["verify_email"] = om.verify_email_batch(ver_batch)

    summary["activity"] = {
        "updated": updated,
        "skipped": skipped,
    }
    if args.dry_run:
        summary["activity_samples"] = [r for r in activity_results if r.get("status") == "dry_run"][:5]

    print(json.dumps(summary, indent=2))
    if not args.dry_run:
        print(
            f"\nDone. Run sync to push to relay:\n"
            f"  python3 {SCRIPTS / 'pipeline.py'} sync",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
