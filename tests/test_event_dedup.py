#!/usr/bin/env python3
"""Tests for event_dedup.py -- historical duplicate email_reply cleanup."""

import json
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
import event_dedup  # noqa: E402
from db_conn import get_conn  # noqa: E402


def _insert_event(conn, *, lead_id, event_type="email_reply", subject=None,
                   body_preview="", metadata=None, created_at="2026-06-10 12:00:00"):
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, channel, subject, body_preview, metadata_json, created_at)
           VALUES (?, ?, 'inbound', 'email', ?, ?, ?, ?)""",
        (lead_id, event_type, subject, body_preview, json.dumps(metadata or {}), created_at),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _fresh_db():
    om.init_db()
    conn = get_conn()
    conn.execute("INSERT INTO leads (id, name, email) VALUES (1, 'Dup Lead', 'dup@example.com')")
    conn.commit()
    return conn


def test_find_duplicate_reply_events_groups_by_lead_and_message_id():
    conn = _fresh_db()
    sparse_id = _insert_event(
        conn, lead_id=1, subject="Re: hello", body_preview="Sure",
        metadata={"message_id": "shared-1", "body": "Sure"},
        created_at="2026-06-10 12:00:00",
    )
    rich_id = _insert_event(
        conn, lead_id=1, subject="Re: hello", body_preview="Sure",
        metadata={"message_id": "shared-1", "body": "Sure", "lead_status_sentiment": "positive"},
        created_at="2026-06-10 12:00:02",
    )
    conn.commit()

    groups = event_dedup.find_duplicate_reply_events(conn)
    conn.close()
    assert len(groups) == 1
    group = groups[0]
    assert group["lead_id"] == 1
    assert group["message_id"] == "shared-1"
    assert group["keep_id"] == rich_id
    assert group["duplicate_ids"] == [sparse_id]


def test_dedupe_reply_events_dry_run_makes_no_changes():
    conn = _fresh_db()
    _insert_event(conn, lead_id=1, metadata={"message_id": "shared-2"})
    _insert_event(conn, lead_id=1, metadata={"message_id": "shared-2"})
    conn.commit()

    result = event_dedup.dedupe_reply_events(conn, commit=False)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert result["groups"] == 1
    assert result["events_deleted"] == 1
    assert result["committed"] is False
    assert count == 2, "dry-run must not delete anything"


def test_dedupe_reply_events_commit_deletes_duplicate_and_merges_body():
    conn = _fresh_db()
    sparse_id = _insert_event(
        conn, lead_id=1, subject=None, body_preview="",
        metadata={"message_id": "shared-3", "body": "Full body text"},
    )
    rich_id = _insert_event(
        conn, lead_id=1, subject="Re: hello", body_preview="",
        metadata={"message_id": "shared-3", "lead_status_sentiment": "positive"},
        created_at="2026-06-10 12:00:02",
    )
    conn.commit()

    result = event_dedup.dedupe_reply_events(conn, commit=True)
    remaining = conn.execute("SELECT id FROM events").fetchall()
    kept = conn.execute(
        "SELECT subject, metadata_json FROM events WHERE id = ?", (rich_id,),
    ).fetchone()
    conn.close()

    assert result["events_deleted"] == 1
    assert [r[0] for r in remaining] == [rich_id]
    assert sparse_id not in [r[0] for r in remaining]
    assert kept["subject"] == "Re: hello"
    kept_meta = json.loads(kept["metadata_json"])
    assert kept_meta["body"] == "Full body text"
    assert kept_meta["lead_status_sentiment"] == "positive"


def test_no_duplicates_returns_empty():
    conn = _fresh_db()
    _insert_event(conn, lead_id=1, metadata={"message_id": "unique-1"})
    conn.commit()

    groups = event_dedup.find_duplicate_reply_events(conn)
    conn.close()
    assert groups == []
