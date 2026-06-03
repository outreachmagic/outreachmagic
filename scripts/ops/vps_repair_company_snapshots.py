#!/usr/bin/env python3
"""Re-ingest company snapshots that failed with FK errors (unmarked in relay_ingested)."""
from __future__ import annotations

import sys
from pathlib import Path

SKILL = Path.home() / "hermes/instances/magic/data/skills/outreachmagic"
sys.path.insert(0, str(SKILL / "scripts"))

import pipeline as om  # noqa: E402


def main() -> None:
    key = om.get_agent_key()
    if not key:
        raise SystemExit("no agent key")
    cfg = om.load_config()
    start = int(cfg.get("last_snapshot_company_after_id") or 0)
    # Replay from first company cursor that had errors during recovery.
    after_id = 259
    local = om.get_or_create_client_id()
    imported = errors = dupes = filtered = 0
    pages = 0
    while True:
        r = om.pull_events_org(
            key,
            snapshot_after_id=after_id or None,
            snapshot_kind="company",
            snapshots_only=True,
            limit=om.RELAY_PULL_COMPANY_MAX,
            timeout=180,
        )
        if r.get("error"):
            raise SystemExit(r.get("message", "pull failed"))
        events = r.get("events") or []
        if not events:
            break
        pages += 1
        batch = om._ingest_relay_page(events, quiet=True)
        imported += batch["imported"]
        errors += batch["skipped_errors"]
        dupes += batch["skipped_duplicates"]
        filtered += batch["skipped_filtered"]
        after_id = int(r.get("max_snapshot_id") or after_id)
        print(
            f"page {pages}: n={len(events)} after_id={after_id} "
            f"+{batch['imported']} dup={batch['skipped_duplicates']} "
            f"filtered={batch['skipped_filtered']} err={batch['skipped_errors']}"
        )
        if not r.get("has_more_snapshots"):
            break
        if after_id >= start:
            break
    if after_id:
        om.set_snapshot_cursor(after_id, "company")
    print(
        f"done pages={pages} imported={imported} dupes={dupes} "
        f"filtered={filtered} errors={errors} cursor={after_id}"
    )


if __name__ == "__main__":
    main()
