"""Stage 9: quarantine and delete the weak-identity junk leads.

~10,884 rows originated from the pre-Stage-1 weak-identity path: name-only
imports that fell through `build_import_identities` to `name_company` /
`import_key`, which are in NON_PERSISTED_IDENTITY_TYPES. No `lead_identities`
row was ever written, so `resolve_lead` could never match them, so every next
sync re-created them. They carry `name='Unknown'`, `email IS NULL`, no
`linkedin_url`, no sales-nav id, no events, no personalization -- zero
recoverable information except `original_source_detail` (list name / import
name), which we preserve in the quarantine table.

They are already invisible to the relay (`entity_key_from_prefetch` returns
`""` for them, and the push loop skips empty keys), so they were never pushed.
That is why we drop their Stage 5 tombstones locally rather than sending them:
`buildSnapshotDeleteStatement` on the relay would look up a row that never
existed and report a batch of failures.

Stage 1's identity guard (`c4b3231`) must be live first, or this cleans up
10,884 rows and immediately regrows them.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db_conn import get_conn


# The six child tables that would make a row *not* junk. Any lead with even one
# row in any of these has recoverable history and stays. The predicate below
# LEFT JOINs each and requires COUNT()=0 -- so contamination is impossible by
# construction, not just by inspection.
_CHILD_TABLES = (
    "lead_identities",
    "events",
    "workspace_lead_events",
    "lead_personalization",
    "bounce_events",
    "crm_entity_map",
)


# The junk predicate. Broken out because dry-run, delete, and the "should be
# zero" contamination check must all agree on the same row set exactly.
_JUNK_WHERE = """
    l.linkedin_url IS NULL
    AND l.linkedin_sales_nav_id IS NULL
    AND l.email IS NULL
    AND LOWER(TRIM(l.name)) = 'unknown'
"""


def _junk_ids_sql() -> str:
    """SELECT l.id FROM leads matching every junk condition and having zero
    rows in any of the six child tables. LEFT JOIN + COUNT()=0 in HAVING is the
    contamination-proof form.
    """
    joins = "\n".join(
        f"LEFT JOIN {t} ON {t}.lead_id = l.id" for t in _CHILD_TABLES
    )
    counts = " + ".join(f"COUNT(DISTINCT {t}.rowid)" for t in _CHILD_TABLES)
    return f"""
        SELECT l.id AS id, l.uid AS uid, l.original_source_detail AS osd
          FROM leads l
          {joins}
         WHERE {_JUNK_WHERE}
         GROUP BY l.id
        HAVING ({counts}) = 0
    """


def _contamination_count_sql() -> str:
    """Rows matching the junk *header* conditions (name/email/linkedin) that
    have any child row. Must be zero in the selected id set; if not, the
    predicate above would over-select and the run refuses.
    """
    joins = "\n".join(
        f"LEFT JOIN {t} ON {t}.lead_id = l.id" for t in _CHILD_TABLES
    )
    counts = " + ".join(f"COUNT(DISTINCT {t}.rowid)" for t in _CHILD_TABLES)
    return f"""
        SELECT COUNT(*) AS n FROM (
            SELECT l.id
              FROM leads l
              {joins}
             WHERE {_JUNK_WHERE}
             GROUP BY l.id
            HAVING ({counts}) > 0
        )
    """


def _report_distribution(conn: sqlite3.Connection) -> dict:
    """Distribution of the selected rows: top source_detail values + monthly
    bucket. Read-only; safe on --dry-run.
    """
    junk = _junk_ids_sql()
    top_sources = conn.execute(
        f"""SELECT COALESCE(osd, '(null)') AS source_detail, COUNT(*) AS n
              FROM ({junk})
             GROUP BY osd
             ORDER BY n DESC
             LIMIT 20"""
    ).fetchall()
    by_month = conn.execute(
        f"""SELECT substr(l.created_at, 1, 7) AS month, COUNT(*) AS n
              FROM leads l
              JOIN ({junk}) j ON j.id = l.id
             GROUP BY month
             ORDER BY month"""
    ).fetchall()
    return {
        "top_sources": [
            {"source_detail": r["source_detail"], "count": r["n"]}
            for r in top_sources
        ],
        "by_month": [
            {"month": r["month"], "count": r["n"]} for r in by_month
        ],
    }


# ---------------------------------------------------------------------------
# Empty-identity leads (Stage 9b). A second, distinct junk population: leads
# whose ONLY lead_identities rows are the system-assigned `uid` / `external_id`
# (a content hash and a source id) with no real contact identity, no email, no
# LinkedIn, name 'unknown'/blank, and zero rows in any history table. These are
# name-only Sales-Navigator / CSV imports (e.g. "popcam | career services").
#
# Unlike the Stage-9 junk above, these DO have a valid relay entity_key (their
# uid), so they were pushed. Deleting them therefore must *keep* the Stage-5
# delete tombstone so the next push removes them from the relay too — otherwise
# the next pull regrows them. That single difference (push vs drop tombstones)
# is why this is a separate path rather than a loosened predicate.
_EMPTY_CHILD_TABLES = (
    "events",
    "workspace_lead_events",
    "lead_personalization",
    "bounce_events",
    "crm_entity_map",
)

_EMPTY_WHERE = """
    (l.name IS NULL OR TRIM(l.name) = '' OR LOWER(TRIM(l.name)) = 'unknown')
    AND (l.email IS NULL OR TRIM(l.email) = '')
    AND l.linkedin_url IS NULL
    AND l.linkedin_sales_nav_id IS NULL
"""


def _empty_ids_sql(workspace_id: Optional[str] = None) -> tuple[str, list]:
    """SELECT the empty-identity lead ids. `li` is LEFT JOINed only on *real*
    identity types, so uid/external_id rows don't count as recoverable history;
    HAVING then requires zero rows across the five history tables and zero real
    identities. Optionally scoped to a workspace's members."""
    child = _EMPTY_CHILD_TABLES
    joins = "\n".join(f"LEFT JOIN {t} ON {t}.lead_id = l.id" for t in child)
    counts = " + ".join(f"COUNT(DISTINCT {t}.rowid)" for t in child)
    ws_join, params = "", []
    if workspace_id:
        ws_join = "JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?"
        params.append(workspace_id)
    sql = f"""
        SELECT l.id AS id, l.uid AS uid, l.original_source_detail AS osd
          FROM leads l
          {ws_join}
          {joins}
          LEFT JOIN lead_identities li
                 ON li.lead_id = l.id
                AND li.identity_type NOT IN ('uid', 'external_id')
         WHERE {_EMPTY_WHERE}
         GROUP BY l.id
        HAVING ({counts} + COUNT(DISTINCT li.rowid)) = 0
    """
    return sql, params


def cleanup_empty_leads(
    conn: Optional[sqlite3.Connection] = None,
    *,
    workspace_id: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    """Quarantine + delete empty-identity leads, KEEPING delete tombstones so the
    relay is cleaned up on the next push (see population note above)."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        ids_sql, ids_params = _empty_ids_sql(workspace_id)
        selected = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({ids_sql})", ids_params
        ).fetchone()["n"]
        top_sources = conn.execute(
            f"""SELECT COALESCE(osd, '(null)') AS source_detail, COUNT(*) AS n
                  FROM ({ids_sql}) GROUP BY osd ORDER BY n DESC LIMIT 20""",
            ids_params,
        ).fetchall()
        result: dict = {
            "dry_run": dry_run,
            "selected": selected,
            "workspace_id": workspace_id,
            "distribution": {
                "top_sources": [
                    {"source_detail": r["source_detail"], "count": r["n"]}
                    for r in top_sources
                ],
            },
            "quarantined": 0,
            "deleted": 0,
            "tombstones_kept": 0,
        }
        if dry_run:
            return result
        if not confirm:
            raise RuntimeError(
                "cleanup_empty_leads is destructive and pushes deletes to the "
                "relay; pass confirm=True (dashboard confirm) after reviewing "
                "the dry-run count."
            )

        rows = conn.execute(ids_sql, ids_params).fetchall()
        ids = [(r["id"], r["uid"], r["osd"]) for r in rows]
        if not ids:
            return result
        conn.executemany(
            "INSERT INTO leads_junk_quarantine (lead_id, uid, original_source_detail) "
            "VALUES (?, ?, ?)",
            ids,
        )
        result["quarantined"] = len(ids)

        # DELETE fires the Stage-5 BEFORE DELETE trigger, filing a 'delete'
        # outbox row keyed on the lead's (valid) uid. We KEEP those tombstones —
        # the next push removes the lead from the relay so a pull can't regrow it.
        lead_ids = [row[0] for row in ids]
        conn.execute("PRAGMA foreign_keys = ON")
        chunk = 500
        for i in range(0, len(lead_ids), chunk):
            batch = lead_ids[i : i + chunk]
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                f"DELETE FROM leads WHERE id IN ({placeholders})", batch
            )
            result["deleted"] += cur.rowcount
        result["tombstones_kept"] = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE entity_type = 'lead_core' AND op = 'delete'"
        ).fetchone()["n"]
        conn.commit()
        return result
    finally:
        if own_conn:
            conn.close()


def cleanup_junk_leads(
    conn: Optional[sqlite3.Connection] = None,
    *,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    """Quarantine -> delete -> drop tombstones. See module docstring for why.

    `dry_run` reports counts + distribution without writing anything. `confirm`
    (the CLI's --yes) is required to actually delete; without it, a dry_run=False
    call is refused.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        junk_sql = _junk_ids_sql()
        selected = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({junk_sql})"
        ).fetchone()["n"]
        distribution = _report_distribution(conn)

        result: dict = {
            "dry_run": dry_run,
            "selected": selected,
            "distribution": distribution,
            "quarantined": 0,
            "deleted": 0,
            "tombstones_dropped": 0,
        }

        if dry_run:
            return result

        if not confirm:
            raise RuntimeError(
                "cleanup_junk_leads is destructive; pass confirm=True (CLI: --yes) "
                "after reviewing the dry-run output."
            )

        # Refuse if the predicate would delete anything with recoverable history.
        # Belt-and-suspenders: the HAVING clause already excludes them, but if
        # anyone ever loosens the predicate this check catches the regression
        # before rows are gone.
        contamination = conn.execute(_contamination_count_sql()).fetchone()["n"]
        if contamination:
            raise RuntimeError(
                f"aborting: {contamination} junk-looking leads have child rows "
                "and would be excluded from delete but flagged by the header "
                "predicate. The two must agree exactly."
            )

        ids = [
            (r["id"], r["uid"], r["osd"])
            for r in conn.execute(junk_sql).fetchall()
        ]
        if not ids:
            return result

        # Materialise into the quarantine table first, before the delete cascades.
        conn.executemany(
            "INSERT INTO leads_junk_quarantine (lead_id, uid, original_source_detail) "
            "VALUES (?, ?, ?)",
            ids,
        )
        result["quarantined"] = len(ids)

        # DELETE fires the Stage 5 BEFORE DELETE trigger on leads, which files a
        # 'delete' outbox row keyed on uid. Since these rows were never pushed
        # (empty entity_key), the tombstone would fail at the relay -- drop
        # them client-side. We batch the DELETE into small chunks so
        # `executemany` doesn't build one 10k-parameter statement.
        lead_ids = [row[0] for row in ids]
        conn.execute("PRAGMA foreign_keys = ON")
        chunk = 500
        for i in range(0, len(lead_ids), chunk):
            batch = lead_ids[i : i + chunk]
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                f"DELETE FROM leads WHERE id IN ({placeholders})", batch
            )
            result["deleted"] += cur.rowcount
            cur2 = conn.execute(
                f"DELETE FROM outbox WHERE entity_type = 'lead_core' "
                f"AND op = 'delete' AND entity_id IN ({placeholders})",
                [str(x) for x in batch],
            )
            result["tombstones_dropped"] += cur2.rowcount

        conn.commit()
        return result
    finally:
        if own_conn:
            conn.close()
