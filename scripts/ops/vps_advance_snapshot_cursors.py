#!/usr/bin/env python3
"""Advance snapshot cursors from max relay:* ids in relay_ingested (run on VPS)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path.home() / "hermes/instances/magic/data/skills/outreachmagic"
DB = SKILL / "databases/outreachmagic.db"
CFG = SKILL / "config/outreachmagic_config.json"

BASE = {
    "workspace": 1_000_000_000,
    "core": 2_000_000_000,
    "company": 3_000_000_000,
}
KEYS = {
    "core": "last_snapshot_core_after_id",
    "workspace": "last_snapshot_workspace_after_id",
    "company": "last_snapshot_company_after_id",
}


def main() -> None:
    cfg = json.loads(CFG.read_text())
    conn = sqlite3.connect(str(DB), timeout=120)
    derived: dict[str, int] = {}
    print("=== relay_ingested snapshot maxima ===")
    for kind, base in BASE.items():
        hi = base + 1_000_000_000
        row = conn.execute(
            """SELECT MAX(CAST(substr(dedupe_key, 7) AS INTEGER)) FROM relay_ingested
               WHERE CAST(substr(dedupe_key, 7) AS INTEGER) >= ? AND CAST(substr(dedupe_key, 7) AS INTEGER) < ?""",
            (base, hi),
        ).fetchone()
        max_rid = int(row[0] or 0)
        snap_id = max_rid - base if max_rid >= base else 0
        cnt = conn.execute(
            """SELECT COUNT(*) FROM relay_ingested
               WHERE CAST(substr(dedupe_key, 7) AS INTEGER) >= ? AND CAST(substr(dedupe_key, 7) AS INTEGER) < ?""",
            (base, hi),
        ).fetchone()[0]
        derived[kind] = snap_id
        print(f"  {kind}: count={cnt} max_snapshot_after_id={snap_id}")
    conn.close()

    bak = CFG.with_name(
        CFG.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    bak.write_text(json.dumps(cfg, indent=2) + "\n")
    updated = False
    for kind, key in KEYS.items():
        old = int(cfg.get(key) or 0)
        new = int(derived.get(kind) or 0)
        if new > old:
            print(f"  {key}: {old} -> {new}")
            cfg[key] = new
            updated = True
    if updated:
        CFG.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"config updated (backup {bak})")
    else:
        print("no changes needed")


if __name__ == "__main__":
    main()
