#!/usr/bin/env python3
"""
Re-push local timeline events to Cloudflare relay with campaign names on each event_log.

Safe re-upload (after worker deploy with event_id upsert):
  1. Purge stale agent event_log rows in D1 (no event_id / missing campaign).
  2. Clear local event push markers (relay_ingested event:*).
  3. Run events-only sync (export now includes payload.campaign).

Requires OUTREACHMAGIC_AGENT_KEY (or cursor config agent_key).
Worker must be deployed from wbhk-worker (relay-db event_id replace + envelope).

Usage:
  export OUTREACHMAGIC_AGENT_KEY=om_agent_...
  python3 scripts/repush_events_to_relay.py --dry-run
  python3 scripts/repush_events_to_relay.py --purge-d1
  python3 scripts/repush_events_to_relay.py --purge-d1 --organization-id org_xxx

Optional D1 purge without admin API (uses wrangler from wbhk-worker repo):
  python3 scripts/repush_events_to_relay.py --purge-d1 --wrangler
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "outreachmagic"
WORKER_ROOT = REPO_ROOT.parent / "wbhk-worker"
PIPELINE = SKILL_ROOT / "scripts" / "pipeline.py"
DB = SKILL_ROOT / "databases" / "outreachmagic.db"
BATCH = SKILL_ROOT / "export" / "batch_sync_to_relay.py"
CURSOR_CFG = Path.home() / ".cursor/skills/outreachmagic/config/outreachmagic_config.json"


def agent_key() -> str:
    key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if key:
        return key
    if CURSOR_CFG.exists():
        key = (json.loads(CURSOR_CFG.read_text()) or {}).get("agent_key", "")
        if key:
            return str(key).strip()
    raise SystemExit("No OUTREACHMAGIC_AGENT_KEY and no cursor config agent_key")


def counts() -> dict:
    import sqlite3

    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM relay_ingested WHERE dedupe_key LIKE 'event:%'"
    ).fetchone()[0]
    pushable = conn.execute(
        """
        SELECT COUNT(*) FROM events e
        WHERE e.metadata_json NOT LIKE '%"source": "relay"%'
          AND e.metadata_json NOT LIKE '%"source":"relay"%'
          AND e.metadata_json NOT LIKE '%"source": "agent_sync"%'
          AND e.metadata_json NOT LIKE '%"source":"agent_sync"%'
        """
    ).fetchone()[0]
    with_campaign = conn.execute(
        """
        SELECT COUNT(*) FROM events e
        LEFT JOIN campaigns c ON c.id = e.campaign_id
        WHERE e.metadata_json NOT LIKE '%"source": "relay"%'
          AND e.metadata_json NOT LIKE '%"source":"relay"%'
          AND e.metadata_json NOT LIKE '%"source": "agent_sync"%'
          AND e.metadata_json NOT LIKE '%"source":"agent_sync"%'
          AND COALESCE(NULLIF(TRIM(c.name), ''), NULLIF(TRIM(json_extract(e.metadata_json, '$.campaign')), '')) IS NOT NULL
        """
    ).fetchone()[0]
    conn.close()
    return {
        "events_total": total,
        "relay_ingested_event": marked,
        "pushable_events": pushable,
        "pushable_with_campaign": with_campaign,
    }


def purge_d1_wrangler(organization_id: str | None) -> None:
    if not WORKER_ROOT.is_dir():
        raise SystemExit(f"wbhk-worker not found at {WORKER_ROOT}")
    sql = (
        "DELETE FROM relay_events WHERE platform = 'agent' "
        "AND json_extract(event_json, '$.action') = 'event_log'"
    )
    if organization_id:
        sql += f" AND organization_id = '{organization_id.replace(chr(39), chr(39)+chr(39))}'"
    cmd = [
        "npx",
        "wrangler",
        "d1",
        "execute",
        "outreach-magic-relay",
        "--remote",
        "--command",
        sql,
    ]
    print("D1 purge:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(WORKER_ROOT), check=True)


def purge_d1_admin(organization_id: str | None) -> None:
    import urllib.request

    admin = os.environ.get("OUTREACHMAGIC_ADMIN_TOKEN", "").strip()
    if not admin:
        raise SystemExit(
            "Set OUTREACHMAGIC_ADMIN_TOKEN for --purge-d1 (admin bearer), or use --wrangler"
        )
    body = {}
    if organization_id:
        body["organization_id"] = organization_id
    req = urllib.request.Request(
        "https://api.outreachmagic.io/admin/purge-agent-event-logs",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {admin}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        print(resp.read().decode())


def sample_export() -> dict:
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    import pipeline as om

    om.init_db()
    ex = om.export_local_changes(events_only=True)
    samples = [e for e in ex.get("entries", []) if e.get("action") == "event_log"][:2]
    return {"export_count": len(ex.get("entries", [])), "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-push local events to relay with campaigns")
    parser.add_argument("--dry-run", action="store_true", help="Show counts and sample export only")
    parser.add_argument(
        "--purge-d1",
        action="store_true",
        help="Delete agent event_log rows in D1 before re-push (avoids duplicate rows)",
    )
    parser.add_argument("--organization-id", help="Limit D1 purge to one org")
    parser.add_argument(
        "--wrangler",
        action="store_true",
        help="Purge D1 via wrangler d1 execute (no admin token)",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Only purge/clear markers; do not run batch sync",
    )
    args = parser.parse_args()

    stats = counts()
    print(json.dumps(stats, indent=2))

    if args.dry_run:
        print(
            "Dry run — would clear relay_ingested event:* markers, "
            f"re-push ~{stats['pushable_events']} events "
            f"({stats['pushable_with_campaign']} with campaign names)."
        )
        if args.purge_d1:
            print("Would also purge D1 agent event_log rows (--purge-d1).")
        return

    if args.purge_d1:
        if args.wrangler:
            purge_d1_wrangler(args.organization_id)
        else:
            purge_d1_admin(args.organization_id)

    import sqlite3

    conn = sqlite3.connect(DB)
    cleared = conn.execute("DELETE FROM relay_ingested WHERE dedupe_key LIKE 'event:%'").rowcount
    conn.commit()
    conn.close()
    print(f"Cleared {cleared} local event push markers.")

    if args.skip_sync:
        return

    agent_key()
    env = os.environ.copy()
    env["OM_SYNC_PHASE"] = "events"
    env.setdefault("OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS", "300")
    proc = subprocess.run(
        [sys.executable, str(BATCH)],
        cwd=str(SKILL_ROOT),
        env=env,
        check=False,
    )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
