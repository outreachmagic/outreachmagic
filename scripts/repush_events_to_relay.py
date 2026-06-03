#!/usr/bin/env python3
"""
Re-push local timeline events to Cloudflare relay after a partial/false sync.

Clears local event push markers (relay_ingested event:*) then runs sync events-only.
Does not re-push lead snapshots unless events are still pending after sync.

Usage:
  export OUTREACHMAGIC_AGENT_KEY=om_agent_...
  python3 scripts/repush_events_to_relay.py
  python3 scripts/repush_events_to_relay.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "outreachmagic"
PIPELINE = SKILL_ROOT / "scripts" / "pipeline.py"
DB = SKILL_ROOT / "databases" / "outreachmagic.db"
BATCH = SKILL_ROOT / "export" / "batch_sync_to_relay.py"


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-push local events to relay")
    parser.add_argument("--dry-run", action="store_true", help="Show counts only")
    args = parser.parse_args()

    import sqlite3

    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM relay_ingested WHERE dedupe_key LIKE 'event:%'"
    ).fetchone()[0]
    pending = conn.execute(
        """
        SELECT COUNT(*) FROM events e
        WHERE 'event:' || CAST(e.id AS TEXT) NOT IN (
          SELECT dedupe_key FROM relay_ingested WHERE dedupe_key LIKE 'event:%'
        )
        AND e.metadata_json NOT LIKE '%"source": "relay"%'
        AND e.metadata_json NOT LIKE '%"source":"relay"%'
        AND e.metadata_json NOT LIKE '%"source": "agent_sync"%'
        AND e.metadata_json NOT LIKE '%"source":"agent_sync"%'
        """
    ).fetchone()[0]
    conn.close()

    print(json.dumps({"events_total": total, "relay_ingested_event": marked, "pending_push": pending}, indent=2))

    if args.dry_run:
        print("Dry run — would clear event:* relay_ingested and run OM_SYNC_PHASE=events")
        return

    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM relay_ingested WHERE dedupe_key LIKE 'event:%'")
    conn.commit()
    conn.close()
    print(f"Cleared {marked} event push markers. Running events-only sync...")

    env = os.environ.copy()
    env["OM_SYNC_PHASE"] = "events"
    proc = subprocess.run(
        [sys.executable, str(BATCH)],
        cwd=str(SKILL_ROOT),
        env=env,
        check=False,
    )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
