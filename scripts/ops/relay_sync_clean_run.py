#!/usr/bin/env python3
"""
Kill stuck sync, purge D1 agent event_logs, clear local push markers, re-run events sync.

Watch progress:
  tail -f ~/Developer/outreachmagic-skill/outreachmagic/logs/batch_sync.log

Usage:
  python3 scripts/ops/relay_sync_clean_run.py
  python3 scripts/ops/relay_sync_clean_run.py --dry-run
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "outreachmagic"
DB = SKILL / "databases" / "outreachmagic.db"
LOG = SKILL / "export" / "batch_sync.log"
WORKER = REPO.parent / "wbhk-worker"
CURSOR_CFG = Path.home() / ".cursor/skills/outreachmagic/config/outreachmagic_config.json"


def agent_key() -> str:
    key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if key:
        return key
    if CURSOR_CFG.exists():
        key = (json.loads(CURSOR_CFG.read_text()) or {}).get("agent_key", "")
        if key:
            return str(key).strip()
    raise SystemExit("Set OUTREACHMAGIC_AGENT_KEY or configure cursor agent_key")


def kill_sync_procs() -> int:
    me = os.getpid()
    out = subprocess.run(
        ["pgrep", "-f", "pipeline.py sync"],
        capture_output=True,
        text=True,
    )
    pids = [int(x) for x in out.stdout.split() if x.strip().isdigit() and int(x) != me]
    out2 = subprocess.run(
        ["pgrep", "-f", "batch_sync_to_relay"],
        capture_output=True,
        text=True,
    )
    pids.extend(int(x) for x in out2.stdout.split() if x.strip().isdigit() and int(x) != me)
    pids = list(dict.fromkeys(pids))
    n = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            n += 1
        except ProcessLookupError:
            pass
    return n


def purge_d1() -> None:
    if not WORKER.is_dir():
        raise SystemExit(f"wbhk-worker not found at {WORKER}")
    subprocess.run(
        [
            "npx",
            "wrangler",
            "d1",
            "execute",
            "outreach-magic-relay",
            "--remote",
            "--command",
            "DELETE FROM relay_events WHERE platform = 'agent' "
            "AND json_extract(event_json, '$.action') = 'event_log';",
        ],
        cwd=str(WORKER),
        check=True,
    )


def clear_local_markers() -> int:
    conn = sqlite3.connect(DB)
    n = conn.execute("DELETE FROM relay_ingested WHERE dedupe_key LIKE 'event:%'").rowcount
    conn.commit()
    conn.close()
    return n


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
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
    marked = conn.execute(
        "SELECT COUNT(*) FROM relay_ingested WHERE dedupe_key LIKE 'event:%'"
    ).fetchone()[0]
    conn.close()

    print(json.dumps({"events_pending_push": pending, "relay_ingested_event": marked}, indent=2))

    if args.dry_run:
        print("Dry run — would kill sync, purge D1 event_logs, clear markers, run events sync")
        return

    killed = kill_sync_procs()
    print(f"Killed {killed} sync process(es).")
    purge_d1()
    print("Purged D1 agent event_log rows.")
    cleared = clear_local_markers()
    print(f"Cleared {cleared} local event push markers.")

    key = agent_key()
    env = os.environ.copy()
    env["OUTREACHMAGIC_AGENT_KEY"] = key
    env["OM_SYNC_PHASE"] = "events"
    env["OM_SYNC_LOG"] = str(LOG)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS", "300")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- clean run started ---\n")

    print(f"Starting events sync — tail -f {LOG}")
    proc = subprocess.run(
        [sys.executable, "-u", str(SKILL / "scripts" / "pipeline.py"), "sync", "--no-health-report"],
        cwd=str(SKILL),
        env=env,
    )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
