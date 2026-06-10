#!/usr/bin/env python3
"""Replay PlusVibe fixture events into local SQLite (dark-factory helper)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

EVENTS_NAME = "relay_events.json"


def main() -> int:
    default_root = Path(__file__).resolve().parent / "data-root"
    data_root = Path(sys.argv[1] if len(sys.argv) > 1 else default_root).resolve()
    os.environ["OUTREACHMAGIC_DATA_ROOT"] = str(data_root)
    from om_paths import set_data_root_override  # noqa: E402

    set_data_root_override(data_root)

    import relay_ingest as ri  # noqa: E402

    events_path = data_root / EVENTS_NAME
    if not events_path.is_file():
        print(f"Missing {events_path}", file=sys.stderr)
        return 1
    events = json.loads(events_path.read_text(encoding="utf-8"))
    ingested = 0
    skipped = 0
    for event in events:
        lid = ri.ingest_relay_event(event, force_workspace_id="ws_pv-factory", quiet=True)
        if lid is None:
            skipped += 1
        else:
            ingested += 1

    import pipeline as om  # noqa: E402

    conn = om.get_conn()
    reply_count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'email_reply'",
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()

    print(
        json.dumps(
            {
                "status": "ok",
                "ingested": ingested,
                "skipped": skipped,
                "email_reply_count": reply_count,
                "total_events": total,
            },
            indent=2,
        )
    )
    return 0 if reply_count == 1 and total == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
