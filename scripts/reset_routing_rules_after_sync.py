#!/usr/bin/env python3
"""
Wait for batch_sync_to_relay to finish, then replace all routing rules with 3 contains rules.

Does not stop or interfere with batch sync — polls until the job is done, then:
  1. DELETE every campaign map in Neon (via routing API)
  2. POST 3 rule_contains rules (popcam, leadgenph, m2n)
  3. Clear local campaign_workspace_map and pull cloud bundle into SQLite

Usage:
  python3 scripts/reset_routing_rules_after_sync.py           # wait for batch sync, then reset
  python3 scripts/reset_routing_rules_after_sync.py --no-wait # reset immediately
  python3 scripts/reset_routing_rules_after_sync.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

import pipeline as om  # noqa: E402
import routing_cloud  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402

BATCH_LOG = Path(__file__).resolve().parents[1] / "skills" / "outreachmagic" / "export" / "batch_sync.log"
CURSOR_CFG = Path.home() / ".cursor/skills/outreachmagic/config/outreachmagic_config.json"
POLL_SECONDS = int(os.environ.get("OM_ROUTING_RESET_POLL", "30"))

CANONICAL_RULES = (
    # (workspace_slug, match_strategy, campaign_name, priority)
    ("popcam", "rule_contains", "popcam", 10),
    ("leadgenph", "rule_contains", "leadgenph", 20),
    ("m2n", "rule_contains", "m2n", 30),
)


def agent_key() -> str:
    key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if key:
        return key
    if CURSOR_CFG.exists():
        key = (json.loads(CURSOR_CFG.read_text()) or {}).get("agent_key", "")
        if key:
            return str(key).strip()
    raise SystemExit("No OUTREACHMAGIC_AGENT_KEY and no cursor config agent_key")


def batch_sync_running() -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "batch_sync_to_relay"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def batch_sync_finished() -> bool:
    if not BATCH_LOG.exists():
        return False
    text = BATCH_LOG.read_text(encoding="utf-8", errors="replace")
    return "=== FINISHED OK ===" in text or "leads phase complete — no more lead ids" in text


def wait_for_batch_sync() -> None:
    print(f"Waiting for batch sync to finish (poll every {POLL_SECONDS}s)...", flush=True)
    while True:
        running = batch_sync_running()
        finished = batch_sync_finished()
        if finished and not running:
            print("Batch sync complete.", flush=True)
            return
        if running:
            tail = ""
            if BATCH_LOG.exists():
                lines = BATCH_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                tail = lines[-1] if lines else ""
            print(f"  batch sync still running… {tail}", flush=True)
        elif finished:
            print("Batch sync log shows complete (process not running).", flush=True)
            return
        else:
            print("  no batch sync process detected; proceeding in 60s unless log updates…", flush=True)
            time.sleep(60)
            if not batch_sync_running():
                return
        time.sleep(POLL_SECONDS)


def reset_routing_rules(*, dry_run: bool = False) -> dict:
    tok = agent_key()
    api_base = routing_cloud.get_api_base(om.load_config)
    bundle = routing_cloud.fetch_routing_bundle(api_base, tok)
    maps = list(bundle.get("campaignMaps") or [])

    summary = {
        "deleted_cloud_maps": 0,
        "created_cloud_maps": 0,
        "rules": list(CANONICAL_RULES),
        "dry_run": dry_run,
    }

    print(f"Cloud has {len(maps)} active routing rule(s).", flush=True)
    if dry_run:
        print("Dry run — would delete all cloud maps and create 3 contains rules.", flush=True)
        return summary

    for item in maps:
        map_id = item.get("id")
        if not map_id:
            continue
        routing_cloud.delete_campaign_map(api_base, tok, map_id)
        summary["deleted_cloud_maps"] += 1
        print(f"  deleted {map_id}", flush=True)

    for ws_slug, strategy, cname, priority in CANONICAL_RULES:
        routing_cloud.push_campaign_map(
            api_base,
            tok,
            source_platform="*",
            workspace_slug=ws_slug,
            campaign_name=cname,
            match_strategy=strategy,
            priority=priority,
        )
        summary["created_cloud_maps"] += 1
        print(f"  created {strategy} {cname!r} → {ws_slug}", flush=True)

    conn = om.get_conn()
    try:
        conn.execute("DELETE FROM campaign_workspace_map WHERE org_id = ?", (DEFAULT_ORG_ID,))
        conn.commit()
        routing_cloud.sync_routing_from_cloud(
            conn,
            api_base=api_base,
            token=tok,
            org_id=DEFAULT_ORG_ID,
            load_config_fn=om.load_config,
            save_config_fn=om.save_config,
            quiet=False,
        )
    finally:
        conn.close()

    final = routing_cloud.fetch_routing_bundle(api_base, tok)
    summary["final_cloud_map_count"] = len(final.get("campaignMaps") or [])
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset routing rules after batch sync completes.")
    parser.add_argument("--no-wait", action="store_true", help="Skip waiting for batch_sync_to_relay")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    args = parser.parse_args()

    if not args.no_wait:
        wait_for_batch_sync()

    result = reset_routing_rules(dry_run=args.dry_run)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
