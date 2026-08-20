"""One-off backfills that bring stored values in line with the write-time rules.

Both commands here fix data that predates a guard rather than data the current
code can still produce -- the guards live at the ingest boundary
(`resolve_lead` for placeholders, `_write_workspace_activity` for timestamps),
so nothing re-dirties what these clean.

They are plain UPDATEs on purpose. `trg_outbox_leads_update` and its siblings
record dirtiness at write time, so every row corrected here is queued for the
next push and the fix reaches the relay instead of living only on this machine.
That is what makes the cleanup survive a pull/push round trip.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db_conn import get_conn
from normalize import sentinel_sql_condition

# Only descriptive columns. `leads.name` is deliberately absent: 'unknown' is
# the marker junk_cleanup keys on to find identity-less rows, so nulling it here
# would hide those rows from the cleanup that exists to delete them.
SENTINEL_COLUMNS: dict[str, tuple[str, ...]] = {
    "leads": (
        "title", "company", "industry", "headcount",
        "location_city", "location_state", "location_country",
        "linkedin_headline",
    ),
    "companies": ("industry", "headcount", "hq_city", "hq_state", "hq_country"),
}

# Columns whose contents are compared lexicographically against
# datetime('now', ...) and exported as dates.
TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "workspace_leads": ("last_activity_at", "last_contacted_at", "current_sentiment_since"),
    "leads": ("last_contact_at",),
}

# Anything that is not already 'YYYY-MM-DD HH:MM:SS'. Checked in SQL so the scan
# never loads the 96% of rows that are already correct.
_MALFORMED_TS_SQL = (
    "({col} IS NOT NULL AND TRIM({col}) != ''"
    " AND {col} NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]')"
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def cleanup_sentinels(
    *, dry_run: bool = True, conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Replace placeholder strings ('unknown', 'N/A', …) with NULL.

    Returns per-column counts. A placeholder is not a value: it sorts and groups
    as a real one, and it wins a merge, because the merge rule only fills fields
    it considers empty.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        found: dict[str, int] = {}
        changed = 0
        for table, columns in SENTINEL_COLUMNS.items():
            present = _table_columns(conn, table)
            for column in columns:
                if column not in present:
                    continue
                cond = sentinel_sql_condition(column)
                # NULL and '' are already absent; only count what actually changes.
                where = f"{cond} AND {column} IS NOT NULL AND TRIM({column}) != ''"
                n = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE {where}").fetchone()["n"]
                if not n:
                    continue
                found[f"{table}.{column}"] = n
                if not dry_run:
                    conn.execute(f"UPDATE {table} SET {column} = NULL WHERE {where}")
                    changed += n
        if not dry_run:
            conn.commit()
        return {
            "status": "ok",
            "dry_run": dry_run,
            "by_column": found,
            "found": sum(found.values()),
            "updated": changed,
        }
    finally:
        if own:
            conn.close()


def cleanup_timestamps(
    *, dry_run: bool = True, conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Rewrite *_at columns into SQLite's own 'YYYY-MM-DD HH:MM:SS' shape.

    Every date filter in the codebase is a text comparison against
    datetime('now', ...), and 'T' (0x54) sorts above ' ' (0x20) -- so a column
    holding both '2025-04-18T13:59:23Z' and '2024-01-01 14:02:26' answers range
    queries incorrectly, quite apart from exporting two formats for one field.
    """
    from pipeline_update import normalize_relay_timestamp_for_storage

    own = conn is None
    conn = conn or get_conn()
    try:
        found: dict[str, int] = {}
        changed = 0
        unparseable = 0
        for table, columns in TIMESTAMP_COLUMNS.items():
            present = _table_columns(conn, table)
            for column in columns:
                if column not in present:
                    continue
                where = _MALFORMED_TS_SQL.format(col=column)
                rows = conn.execute(
                    f"SELECT rowid AS rid, {column} AS v FROM {table} WHERE {where}"
                ).fetchall()
                if not rows:
                    continue
                found[f"{table}.{column}"] = len(rows)
                if dry_run:
                    continue
                updates = []
                for r in rows:
                    fixed = normalize_relay_timestamp_for_storage(r["v"])
                    # Left unchanged rather than replaced with "now": bad data
                    # stays visible instead of being masked as recent activity.
                    if fixed == r["v"]:
                        unparseable += 1
                        continue
                    updates.append((fixed, r["rid"]))
                if updates:
                    conn.executemany(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?", updates)
                    changed += len(updates)
        if not dry_run:
            conn.commit()
        return {
            "status": "ok",
            "dry_run": dry_run,
            "by_column": found,
            "found": sum(found.values()),
            "updated": changed,
            "unparseable": unparseable,
        }
    finally:
        if own:
            conn.close()
