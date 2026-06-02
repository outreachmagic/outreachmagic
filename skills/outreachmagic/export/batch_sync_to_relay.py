#!/usr/bin/env python3
"""
Push the local Popcam SQLite DB to api.outreachmagic.io in controlled batches.

How it works (not Cloudflare cron batches — local script batches):
  1. EVENTS phase (once): pipeline.py sync pushes ~62k legacy message events to POST /push.
  2. LEADS phase (loop): walk every lead by id in chunks of BATCH_SIZE (default 2500):
       a. SET cloud_pending=1 on those leads (+ their workspace_leads rows)
       b. pipeline.py sync uploads lead_core_update + lead_workspace_update snapshots
       c. Relay returns success → cloud_pending cleared back to 0 for that chunk
  Each chunk is one "batch" in batch_sync.log. ~46 batches for 114k leads.

Resume: OM_SYNC_RESUME_AFTER_ID or auto-detect from last "batch N done" in the log.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
DB = SKILL_ROOT / "databases" / "outreachmagic.db"
PIPELINE = SKILL_ROOT / "scripts" / "pipeline.py"
LOG = SKILL_ROOT / "export" / "batch_sync.log"
CURSOR_CFG = Path.home() / ".cursor/skills/outreachmagic/config/outreachmagic_config.json"
BATCH_SIZE = int(os.environ.get("OM_SYNC_LEAD_BATCH", "2500"))
PUSH_BATCH_SIZE = int(os.environ.get("OUTREACHMAGIC_SYNC_BATCH_SIZE", "400"))
SLEEP_S = float(os.environ.get("OM_SYNC_SLEEP", "2"))


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def agent_key() -> str:
    key = os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    if key:
        return key
    if CURSOR_CFG.exists():
        key = (json.loads(CURSOR_CFG.read_text()) or {}).get("agent_key", "")
        if key:
            return str(key).strip()
    raise SystemExit("No OUTREACHMAGIC_AGENT_KEY and no cursor config agent_key")


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM leads WHERE cloud_pending = 1) AS core_pending,
          (SELECT COUNT(*) FROM workspace_leads WHERE cloud_pending = 1) AS ws_pending,
          (SELECT COUNT(*) FROM events e
             WHERE 'event:' || CAST(e.id AS TEXT) NOT IN (
               SELECT dedupe_key FROM relay_ingested WHERE dedupe_key LIKE 'event:%'
             )
             AND e.metadata_json NOT LIKE '%"source": "relay"%'
             AND e.metadata_json NOT LIKE '%"source":"relay"%'
             AND e.metadata_json NOT LIKE '%"source": "agent_sync"%'
             AND e.metadata_json NOT LIKE '%"source":"agent_sync"%'
          ) AS events_pending
        """
    ).fetchone()
    return {
        "core_pending": row[0],
        "ws_pending": row[1],
        "events_pending": row[2],
    }


def progress(conn: sqlite3.Connection, last_id: int, batch_num: int) -> dict:
    """How much of the lead snapshot walk is left (by lead id cursor)."""
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    remaining = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE id > ?", (last_id,)
    ).fetchone()[0]
    done = total - remaining
    batches_total = (total + BATCH_SIZE - 1) // BATCH_SIZE
    batches_left = (remaining + BATCH_SIZE - 1) // BATCH_SIZE
    pct = round(100.0 * done / total, 1) if total else 0.0
    pending = counts(conn)
    return {
        "total_leads": total,
        "leads_synced_by_cursor": done,
        "leads_remaining": remaining,
        "pct_complete": pct,
        "batch_num": batch_num,
        "batches_total_est": batches_total,
        "batches_remaining_est": batches_left,
        "last_lead_id": last_id,
        **pending,
    }


def log_progress(conn: sqlite3.Connection, last_id: int, batch_num: int, stage: str) -> None:
    p = progress(conn, last_id, batch_num)
    log(
        f"PROGRESS [{stage}] "
        f"leads {p['leads_synced_by_cursor']:,}/{p['total_leads']:,} ({p['pct_complete']}%) | "
        f"batch {p['batch_num']}/{p['batches_total_est']} | "
        f"~{p['leads_remaining']:,} leads left (~{p['batches_remaining_est']} batches) | "
        f"uploading now: core_pending={p['core_pending']:,} ws_pending={p['ws_pending']:,} | "
        f"events_left={p['events_pending']}"
    )


def run_sync(env: dict, *, label: str = "sync") -> tuple[int, dict | None, str]:
    """Run pipeline sync; stream stdout/stderr into batch_sync.log in real time."""
    log(f"{label}: starting pipeline.py sync → POST https://api.outreachmagic.io/push")
    proc = subprocess.Popen(
        [sys.executable, str(PIPELINE), "sync", "--no-health-report"],
        cwd=str(SKILL_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    stdout_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        stdout_lines.append(line)
        stripped = line.rstrip()
        if stripped:
            log(f"{label}: {stripped}")
    proc.wait()
    stdout = "".join(stdout_lines)
    payload = _parse_sync_json(stdout)
    err = "" if proc.returncode == 0 else stdout[-2000:]
    return proc.returncode, payload, err


def _parse_sync_json(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def resume_after_id_from_log() -> int:
    raw = os.environ.get("OM_SYNC_RESUME_AFTER_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    if not LOG.exists():
        return 0
    text = LOG.read_text()
    done_batches = re.findall(r"batch (\d+) done", text)
    if not done_batches:
        return 0
    last_batch = max(int(b) for b in done_batches)
    marks = re.findall(
        rf"batch {last_batch} lead_ids (\d+)\.\.(\d+) marked",
        text,
    )
    if marks:
        return int(marks[-1][1])
    return 0


def main() -> None:
    key = agent_key()
    env = os.environ.copy()
    env["OUTREACHMAGIC_AGENT_KEY"] = key
    env["OUTREACHMAGIC_SYNC_BATCH_SIZE"] = str(PUSH_BATCH_SIZE)
    env.setdefault("OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS", "180")

    log(
        f"settings: lead_batch={BATCH_SIZE} push_batch={PUSH_BATCH_SIZE} "
        f"sync_timeout={env['OUTREACHMAGIC_SYNC_TIMEOUT_SECONDS']}s"
    )

    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")

    phase = os.environ.get("OM_SYNC_PHASE", "all")
    log(f"=== START phase={phase} batch_size={BATCH_SIZE} ===")
    log(f"db={DB}")
    log_progress(conn, 0, 0, "startup")

    if phase in ("all", "events"):
        log("--- EVENTS PHASE: push legacy email_sent/reply events (one sync) ---")
        conn.execute("UPDATE leads SET cloud_pending = 0")
        conn.execute("UPDATE workspace_leads SET cloud_pending = 0")
        conn.commit()
        c = counts(conn)
        log(f"events to push: {c['events_pending']:,}")
        code, payload, err = run_sync(env, label="events")
        log(f"events done exit={code} pushed={((payload or {}).get('agent_events_pushed'))}")
        if code != 0:
            log(f"events FAILED: {err[-2000:]}")
            sys.exit(code)
        log_progress(conn, 0, 0, "after-events")

    if phase in ("all", "leads"):
        resume_after = resume_after_id_from_log()
        last_id = resume_after
        batch_num = int(os.environ.get("OM_SYNC_START_BATCH", "0") or 0)

        if resume_after > 0:
            log(f"--- LEADS PHASE: RESUME after lead id {resume_after:,} ---")
            c = counts(conn)
            if c["core_pending"] or c["ws_pending"]:
                log(
                    f"flush: finishing interrupted upload "
                    f"(core={c['core_pending']:,} ws={c['ws_pending']:,} snapshots)"
                )
                log_progress(conn, last_id, batch_num, "before-flush")
                code, payload, err = run_sync(env, label="flush")
                pushed = (payload or {}).get("lead_snapshots_pushed", 0)
                log(f"flush done exit={code} lead_snapshots_pushed={pushed}")
                if code != 0:
                    log(f"flush FAILED: {err[-2000:]}")
                    sys.exit(code)
                log_progress(conn, last_id, batch_num, "after-flush")
        else:
            log("--- LEADS PHASE: fresh run (clears cloud_pending, then batches) ---")
            conn.execute("UPDATE leads SET cloud_pending = 0")
            conn.execute("UPDATE workspace_leads SET cloud_pending = 0")
            conn.commit()
            log_progress(conn, 0, 0, "leads-start")

        log("--- LEADS PHASE: batch loop (mark 2500 → sync → clear) ---")
        while True:
            rows = conn.execute(
                "SELECT id FROM leads WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, BATCH_SIZE),
            ).fetchall()
            if not rows:
                log("leads phase complete — no more lead ids")
                break
            ids = [r[0] for r in rows]
            last_id = ids[-1]
            batch_num += 1
            ph = ",".join("?" * len(ids))
            conn.execute(f"UPDATE leads SET cloud_pending = 1 WHERE id IN ({ph})", ids)
            conn.execute(
                f"UPDATE workspace_leads SET cloud_pending = 1 WHERE lead_id IN ({ph})",
                ids,
            )
            conn.commit()
            log(
                f"batch {batch_num}: marked {len(ids)} leads "
                f"(ids {ids[0]:,}..{ids[-1]:,}) for upload"
            )
            log_progress(conn, last_id, batch_num, f"batch-{batch_num}-marked")

            for attempt in range(1, 4):
                code, payload, err = run_sync(
                    env, label=f"batch-{batch_num}"
                )
                if code != 0:
                    log(f"batch {batch_num} attempt {attempt} FAILED: {proc_tail(err)}")
                if code == 0:
                    pushed = (payload or {}).get("lead_snapshots_pushed", "?")
                    log(f"batch {batch_num} upload ok (relay reported pushed={pushed})")
                    break
                time.sleep(30 * attempt)
            else:
                log(f"batch {batch_num} FAILED after 3 attempts — stopping")
                sys.exit(1)

            log_progress(conn, last_id, batch_num, f"batch-{batch_num}-done")
            time.sleep(SLEEP_S)

    conn.close()
    log("=== FINISHED OK ===")
    log("Next: pipeline.py sync  (push routing rules / M2N workspace to cloud)")


def proc_tail(err: str) -> str:
    return (err or "")[-500:]


if __name__ == "__main__":
    main()
