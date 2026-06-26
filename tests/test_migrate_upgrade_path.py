#!/usr/bin/env python3
"""Upgrade-path migration tests (SQLite lock regression)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402


def test_backfill_null_campaign_reuses_open_connection():
    """Regression: backfill must not open a second SQLite connection during migrate_db."""
    om.init_db()
    conn = om.get_conn()
    conn.execute("INSERT INTO leads (name, email) VALUES ('Legacy', 'legacy@example.com')")
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_reply', 'inbound', ?, NULL)""",
        (lead_id, json.dumps({"platform": "plusvibe", "relay_id": 1})),
    )
    conn.commit()

    cfg = om.load_config()
    cfg.pop("null_campaign_backfill_at", None)
    om.save_config(cfg)

    result = om.backfill_null_campaign_quarantine(quiet=True, conn=conn)
    conn.commit()
    assert result["found"] >= 1
    assert result["quarantined"] >= 1

    # Second pass on the same connection must not raise database is locked.
    again = om.backfill_null_campaign_quarantine(quiet=True, conn=conn)
    conn.commit()
    assert again["found"] >= 1
    conn.close()
