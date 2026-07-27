"""
Database initialization, schema migration, and backfill functions.

Extracted from pipeline.py's "Database Operations" section.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from typing import Optional

import bounces
from bounces import backfill_bounce_events_from_events
from db_conn import get_conn
from om_paths import get_db_path
from schema import SCHEMA_SQL
from schema_views import ensure_read_views
from workspace_routing import (
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    assign_campaign_map,
    ensure_default_org_workspace,
    ensure_organization,
    get_org_routing_config,
    normalize_campaign_name,
    upsert_workspace_lead,
)


OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS outbox (
    entity_type    TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    op             TEXT NOT NULL,              -- 'upsert' | 'delete'
    entity_key     TEXT,                       -- captured at trigger time for deletes only
    workspace_slug TEXT,
    dirty_at       TEXT NOT NULL DEFAULT (datetime('now')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    PRIMARY KEY (entity_type, entity_id, op)
);
CREATE INDEX IF NOT EXISTS idx_outbox_dirty ON outbox(dirty_at);

-- What we believe the relay currently holds. Lets the push loop drop an echo
-- (a local write that merely re-applied what we just pulled) by comparing
-- content hashes, rather than by suppressing writes during a pull -- the latter
-- latches on a crash and silently un-tracks every subsequent local write.
CREATE TABLE IF NOT EXISTS sync_shadow (
    entity_type    TEXT NOT NULL,
    entity_key     TEXT NOT NULL,
    workspace_slug TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL,
    relay_seq      INTEGER,
    synced_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, entity_key, workspace_slug)
);
"""


def _outbox_upsert_stmt(entity_type: str, id_expr: str) -> str:
    return (
        "INSERT INTO outbox (entity_type, entity_id, op, dirty_at) "
        f"VALUES ('{entity_type}', {id_expr}, 'upsert', datetime('now')) "
        "ON CONFLICT (entity_type, entity_id, op) DO UPDATE SET "
        "dirty_at = datetime('now'), attempts = 0, last_error = NULL;"
    )


def build_outbox_triggers() -> list[str]:
    """Generate the outbox triggers from sync_contract.SYNC_MAP.

    Three rules, and the third is the one that bites if you skip it:

    1. Any INSERT/UPDATE on a mapped table marks its owning entity dirty.
    2. A DELETE on an *owner* table (leads, companies, ...) is a tombstone --
       the entity itself is gone, so we record op='delete' and capture the
       entity_key while the row still exists (hence BEFORE DELETE). We also drop
       any queued 'upsert' for it: pushing a snapshot for a deleted row is a
       guaranteed relay-side error.
    3. A DELETE on a *child* table (a tag removed, a verification row cleared)
       is an ordinary content change to a still-living parent -> 'upsert'. But
       when the parent is itself being deleted, SQLite's ON DELETE CASCADE
       deletes the children too, and those child triggers would re-queue an
       upsert for the very entity we just tombstoned. Every child DELETE trigger
       is therefore guarded on the parent still existing.
    """
    from sync_contract import OWNER_TABLES, SYNC_MAP, entity_id_expr

    # Parent-existence guard per entity_type, expressed against the child's OLD row.
    parent_guard = {
        "lead_core": "EXISTS (SELECT 1 FROM leads WHERE id = OLD.lead_id)",
        "lead_workspace": (
            "EXISTS (SELECT 1 FROM workspace_leads "
            "WHERE lead_id = OLD.lead_id AND workspace_id = OLD.workspace_id)"
        ),
        "company": "EXISTS (SELECT 1 FROM companies WHERE id = OLD.company_id)",
    }
    # The immutable key to file a tombstone under, read from the owner's OLD row.
    tombstone_key = {
        "leads": ("OLD.uid", "NULL"),
        "companies": ("OLD.uid", "NULL"),
        "workspace_leads": (
            "(SELECT uid FROM leads WHERE id = OLD.lead_id)",
            "(SELECT slug FROM workspaces WHERE id = OLD.workspace_id)",
        ),
        "sender_accounts": ("CAST(OLD.id AS TEXT)", "NULL"),
        "sender_domains": ("OLD.domain", "NULL"),
    }

    out: list[str] = []
    for table, (etype, _) in SYNC_MAP.items():
        new_expr = entity_id_expr(table, "NEW")
        old_expr = entity_id_expr(table, "OLD")

        for verb, row_expr in (("insert", new_expr), ("update", new_expr)):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_outbox_{table}_{verb} "
                f"AFTER {verb.upper()} ON {table} BEGIN "
                f"{_outbox_upsert_stmt(etype, row_expr)} END"
            )

        if table in OWNER_TABLES:
            key_expr, slug_expr = tombstone_key[table]
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_outbox_{table}_delete "
                f"BEFORE DELETE ON {table} BEGIN "
                f"DELETE FROM outbox WHERE entity_type = '{etype}' "
                f"AND entity_id = {old_expr} AND op = 'upsert'; "
                "INSERT INTO outbox (entity_type, entity_id, op, entity_key, workspace_slug, dirty_at) "
                f"VALUES ('{etype}', {old_expr}, 'delete', {key_expr}, {slug_expr}, datetime('now')) "
                "ON CONFLICT (entity_type, entity_id, op) DO UPDATE SET "
                "dirty_at = datetime('now'), attempts = 0, last_error = NULL; END"
            )
        else:
            guard = parent_guard[etype]
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_outbox_{table}_delete "
                f"AFTER DELETE ON {table} WHEN {guard} BEGIN "
                f"{_outbox_upsert_stmt(etype, old_expr)} END"
            )

    # A lead_workspace tombstone reads its entity_key as
    # `(SELECT uid FROM leads WHERE id = OLD.lead_id)`. That works when a
    # workspace_leads row is deleted on its own -- and returns NULL when the
    # delete arrives by ON DELETE CASCADE from `leads`, because the parent row
    # is already gone by the time the child's BEFORE DELETE trigger runs. A
    # tombstone with no entity_key cannot be pushed, so it sat in the outbox
    # forever while the relay kept the workspace snapshot: deleting 10,458 empty
    # leads produced exactly that many undeliverable rows.
    #
    # Pre-file them from the parent, where OLD.uid is still readable. The
    # cascade's own trigger then hits ON CONFLICT and only bumps dirty_at --
    # it never touches entity_key -- so the good key survives.
    out.append(
        "CREATE TRIGGER IF NOT EXISTS trg_outbox_leads_delete_workspace_tombstones "
        "BEFORE DELETE ON leads BEGIN "
        "INSERT INTO outbox (entity_type, entity_id, op, entity_key, workspace_slug, dirty_at) "
        "SELECT 'lead_workspace', wl.lead_id || ':' || wl.workspace_id, 'delete', "
        "       OLD.uid, (SELECT slug FROM workspaces WHERE id = wl.workspace_id), "
        "       datetime('now') "
        "  FROM workspace_leads wl WHERE wl.lead_id = OLD.id "
        "ON CONFLICT (entity_type, entity_id, op) DO UPDATE SET "
        "  entity_key = excluded.entity_key, "
        "  workspace_slug = excluded.workspace_slug, "
        "  dirty_at = datetime('now'), attempts = 0, last_error = NULL; END"
    )
    return out


def _stage_rank_case(column: str) -> str:
    """CASE expression ranking stages, byte-for-byte the ordering furthest_stage()
    uses (PIPELINE_STAGES index), so the derived cache and the Python helper can
    never disagree."""
    from constants import PIPELINE_STAGES

    whens = " ".join(
        f"WHEN '{stage}' THEN {i}" for i, stage in enumerate(PIPELINE_STAGES)
    )
    return f"CASE {column} {whens} ELSE 0 END"


def ensure_derived_lead_stage(conn: sqlite3.Connection) -> None:
    """leads.stage stops being a fact and becomes a cache.

    Pipeline stage is per-workspace (workspace_leads.status): the same lead can be
    `replied` in one workspace and `prospecting` in another, so an org-wide stage
    is ill-defined by construction. But formatters.py, the stats breakdown,
    copy-insights and merge all read leads.stage, and rewriting every org-wide
    report to be workspace-scoped is a much bigger change than this one.

    So: workspace_leads.status is the single source of truth and the only thing on
    the wire; leads.stage is derived from it (furthest stage across the lead's
    workspaces) and maintained here. Reports keep working, the duplicate authority
    is gone, and nothing can write a stage to leads that the workspaces disagree
    with.
    """
    rank = _stage_rank_case("wl.status")
    derive = f"""
        UPDATE leads SET stage = COALESCE((
            SELECT wl.status FROM workspace_leads wl
            WHERE wl.lead_id = {{lead_ref}}
            ORDER BY {rank} DESC LIMIT 1
        ), stage)
        WHERE id = {{lead_ref}}
    """
    for verb, ref in (("INSERT", "NEW.lead_id"), ("UPDATE", "NEW.lead_id")):
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_leads_stage_from_ws_{verb.lower()}
            AFTER {verb} ON workspace_leads
            BEGIN
                {derive.format(lead_ref=ref)};
            END
        """)
    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS trg_leads_stage_from_ws_delete
        AFTER DELETE ON workspace_leads
        WHEN EXISTS (SELECT 1 FROM leads WHERE id = OLD.lead_id)
        BEGIN
            {derive.format(lead_ref="OLD.lead_id")};
        END
    """)


def backfill_derived_lead_stage(conn: sqlite3.Connection) -> int:
    """One-time reconciliation of the cache against its source of truth."""
    rank = _stage_rank_case("wl.status")
    cur = conn.execute(f"""
        UPDATE leads SET stage = (
            SELECT wl.status FROM workspace_leads wl
            WHERE wl.lead_id = leads.id
            ORDER BY {rank} DESC LIMIT 1
        )
        WHERE EXISTS (SELECT 1 FROM workspace_leads wl WHERE wl.lead_id = leads.id)
          AND stage IS NOT (
            SELECT wl.status FROM workspace_leads wl
            WHERE wl.lead_id = leads.id
            ORDER BY {rank} DESC LIMIT 1
          )
    """)
    return cur.rowcount


def _drop_stale_outbox_triggers(conn: sqlite3.Connection) -> None:
    """Drop every trg_outbox_* trigger so ensure_outbox() rebuilds them fresh.

    CREATE TRIGGER IF NOT EXISTS (what ensure_outbox uses) never updates an
    existing trigger's body -- if entity_id_expr()/SYNC_MAP changed shape
    since a trigger was first installed on some install out there, the old
    body sticks around forever, silently. That already happened for real:
    an old install had trg_outbox_sender_domains_* referencing NEW.id/OLD.id
    from before sender_domains was keyed by domain instead of a surrogate id
    -- SQLite doesn't validate a trigger body's column references at CREATE
    time, only when the trigger actually fires (or, as discovered here, when
    an unrelated ALTER TABLE RENAME forces it to recompile every trigger in
    the schema to check for name references). Any DB with that latent bug
    would take down the very first ALTER TABLE anyone ever ran against it --
    which is exactly what Stage 7's rename below is. Rebuilding from
    SYNC_MAP on every migrate_db() call is cheap (a few dozen tiny
    statements) and means this class of drift can't accumulate again.
    """
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_outbox_%'"
        ).fetchall()
    ]
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def ensure_outbox(conn: sqlite3.Connection) -> None:
    """Create the outbox/shadow tables and (re)install their triggers."""
    conn.executescript(OUTBOX_SQL)
    for stmt in build_outbox_triggers():
        conn.execute(stmt)


# The one-time cutover seed. The triggers only see writes made *after* they are
# installed, so at cutover the outbox is empty while the relay may still be
# missing changes the old cursor never managed to push. Everything is marked
# dirty once; the drain's content-hash check then drops whatever the relay
# already holds, so this costs payload-build CPU rather than 300k pushes --
# provided sync_shadow has been seeded by a pull first.
#
# updated_at is deliberately not consulted: 40.7% of it is older than its own
# created_at, so it cannot be used to narrow this down. Ignore it, don't repair it.
_OUTBOX_BACKFILL_SOURCES = (
    ("lead_core", "SELECT CAST(id AS TEXT) AS entity_id FROM leads"),
    (
        "lead_workspace",
        "SELECT CAST(lead_id AS TEXT) || ':' || workspace_id AS entity_id "
        "FROM workspace_leads",
    ),
    ("company", "SELECT CAST(id AS TEXT) AS entity_id FROM companies"),
    ("sender_account", "SELECT CAST(id AS TEXT) AS entity_id FROM sender_accounts"),
    ("sender_domain", "SELECT domain AS entity_id FROM sender_domains"),
)


def backfill_outbox(
    conn: Optional[sqlite3.Connection] = None, *, dry_run: bool = False
) -> dict:
    """Mark every synced entity dirty once, for the cutover to the outbox."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    result: dict = {"dry_run": dry_run, "queued": {}, "total": 0}
    try:
        for entity_type, select_sql in _OUTBOX_BACKFILL_SOURCES:
            n = conn.execute(
                f"SELECT COUNT(*) AS n FROM ({select_sql})"
            ).fetchone()["n"]
            result["queued"][entity_type] = n
            result["total"] += n
            if dry_run:
                continue
            # `WHERE TRUE` is load-bearing: without it SQLite cannot tell the
            # ON CONFLICT clause from a join constraint on the SELECT.
            conn.execute(
                f"""INSERT INTO outbox (entity_type, entity_id, op, dirty_at)
                    SELECT ?, entity_id, 'upsert', datetime('now')
                    FROM ({select_sql})
                    WHERE TRUE
                    ON CONFLICT (entity_type, entity_id, op) DO NOTHING""",
                (entity_type,),
            )
        if not dry_run:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return result


def init_db():
    db = get_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    from pipeline import _chmod_best_effort  # stays in pipeline.py (Config section)

    _chmod_best_effort(db.parent, 0o700)
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    migrate_db(conn)
    conn.close()
    if db.exists():
        _chmod_best_effort(db, 0o600)
    return True


_LEAD_PROVIDER_ATTEMPTS_REQUIRED_COLUMNS = frozenset({
    "id", "lead_id", "provider", "domain", "attempted_at", "completed_at",
    "status", "result_email", "result_validity", "batch_id", "metadata_json",
})


def _repair_lead_provider_attempts_schema(conn: sqlite3.Connection) -> None:
    """Rebuild lead_provider_attempts if it exists but with the wrong columns.

    CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table under
    that name, regardless of its actual columns -- if something outside this
    migration (e.g. a manual/raw SQL statement against the DB, which this
    project's own rules prohibit) created a malformed version of this table
    first, the CREATE TABLE IF NOT EXISTS above would silently leave it
    broken. Plain ALTER TABLE ADD COLUMN can't fix this on its own: SQLite
    doesn't allow adding a PRIMARY KEY/AUTOINCREMENT column or a UNIQUE
    constraint after the fact, and a malformed table could have the wrong
    PRIMARY KEY entirely (e.g. composite (lead_id, provider) instead of a
    real autoincrement id) -- so this does a rename + recreate + best-effort
    data copy instead.

    Stage 7 retires this name to a read-only VIEW over
    lead_provider_observations (see _migrate_provider_observations below) --
    once that has happened there is no table left here to repair, and
    PRAGMA table_info would report the view's columns, not a real table's.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'provider_observations_unification'"
    ).fetchone():
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lead_provider_attempts)").fetchall()}
    if not cols or _LEAD_PROVIDER_ATTEMPTS_REQUIRED_COLUMNS.issubset(cols):
        return  # table doesn't exist yet, or already has the right columns
    conn.execute("ALTER TABLE lead_provider_attempts RENAME TO lead_provider_attempts_malformed_backup")
    conn.execute("""
        CREATE TABLE lead_provider_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            provider        TEXT NOT NULL,
            domain          TEXT,
            attempted_at    TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at    TEXT,
            status          TEXT NOT NULL,
            result_email    TEXT,
            result_validity TEXT,
            batch_id        INTEGER REFERENCES provider_batch_jobs(id) ON DELETE SET NULL,
            metadata_json   TEXT,
            UNIQUE (lead_id, provider)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lpa_lookup ON lead_provider_attempts(lead_id, provider, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lpa_provider ON lead_provider_attempts(provider, status, attempted_at)"
    )
    if {"lead_id", "provider"}.issubset(cols):
        domain_expr = "domain" if "domain" in cols else "NULL"
        attempted_at_expr = "COALESCE(attempted_at, datetime('now'))" if "attempted_at" in cols else "datetime('now')"
        status_expr = "COALESCE(status, 'unknown')" if "status" in cols else "'unknown'"
        result_email_expr = "result_email" if "result_email" in cols else "NULL"
        result_validity_expr = "result_validity" if "result_validity" in cols else "NULL"
        batch_id_expr = "batch_id" if "batch_id" in cols else "NULL"
        metadata_json_expr = "metadata_json" if "metadata_json" in cols else "NULL"
        conn.execute(f"""
            INSERT OR IGNORE INTO lead_provider_attempts
                (lead_id, provider, domain, attempted_at, status, result_email, result_validity, batch_id, metadata_json)
            SELECT lead_id, provider, {domain_expr}, {attempted_at_expr}, {status_expr},
                   {result_email_expr}, {result_validity_expr}, {batch_id_expr}, {metadata_json_expr}
            FROM lead_provider_attempts_malformed_backup
        """)
    conn.execute("DROP TABLE lead_provider_attempts_malformed_backup")


def _collapse_domain_found_tags(conn: sqlite3.Connection) -> None:
    """Replace per-domain domain_found_<domain> tags with one domain_discovered.

    The old form minted a tag per unique domain -- 288 of 340 tags in one real
    workspace, 218 used by exactly one lead -- burying the handful of tags that
    describe an actual segment. It was also a denormalized copy of
    companies.domain that nothing kept in step, so tags naming values that were
    later corrected (gmail.com., health.usnews.com) persisted as a permanent
    record of the wrong answer.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'domain_found_tag_collapse'"
    ).fetchone():
        return
    rows = conn.execute(
        # substr() rather than LIKE: '_' is a single-char wildcard in LIKE, so
        # the pattern would also match tags this migration has no business
        # touching, and escaping it portably is fiddlier than an exact prefix.
        """SELECT id, workspace_id, lead_id FROM workspace_lead_tags
           WHERE substr(tag, 1, 13) = 'domain_found_'"""
    ).fetchall()
    for row in rows:
        # INSERT OR IGNORE first: several old tags can collapse onto the same
        # (workspace, lead) pair, and the table is uniquely keyed on the tag.
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
               VALUES (?, ?, ?, 'domain_discovered')""",
            (f"wlt_{row['workspace_id']}_{row['lead_id']}_domdisc",
             row["workspace_id"], row["lead_id"]),
        )
        conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["id"],))
    conn.execute("INSERT INTO migration_flags (name) VALUES ('domain_found_tag_collapse')")


def _reconcile_stale_domain_discovered_tags(conn: sqlite3.Connection) -> None:
    """Retag domain_discovered where no domain actually backs it up.

    domain_discovered means "a domain was found and attached for this
    company" -- but the tag-collapse migration above (domain_found_tag_
    collapse) rewrote it purely by STRING, from whatever domain_found_<domain>
    tags predate it, without checking whether the current company record still
    supports that claim. Those legacy tags came from the original, ungated
    version of find-domains: no confidence floor at all, so it happily tagged
    a company from a directory-site or wrong-address match with the same
    domain_discovered later runs use to mean "trustworthy". A live production
    example: RangeWater Residential's every domain_lookup observation, then
    and now, was confidence 0.35 -- always below the 0.40 attach floor,
    correctly tagged domain_low_confidence by every run of today's code -- but
    also carried a fossil domain_discovered from before that floor existed.

    A company genuinely mid-review (a real domain found, but parked behind a
    pending merge because another company row already owns that identity) is
    NOT touched: it has a company_identities row even though companies.domain
    is empty, which is how it is told apart from a tag with nothing behind it
    at all. Measured on one production workspace this way: 240 companies
    matched "domain_discovered, no companies.domain" and 203 of them (85%)
    were the legitimate pending-merge case; this migration only ever acts on
    the other 15%.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'domain_discovered_tag_reconcile'"
    ).fetchone():
        return
    rows = conn.execute(
        """SELECT t.id AS tag_id, t.workspace_id, t.lead_id, l.company_id
           FROM workspace_lead_tags t
           JOIN leads l ON l.id = t.lead_id
           JOIN companies c ON c.id = l.company_id
           WHERE t.tag = 'domain_discovered'
             AND (c.domain IS NULL OR TRIM(c.domain) = '')
             AND NOT EXISTS (
                 SELECT 1 FROM company_identities ci
                 WHERE ci.company_id = c.id AND ci.identity_type = 'domain'
             )"""
    ).fetchall()
    for row in rows:
        # Hardcoded rather than imported from domain_discovery.
        # MIN_ATTACH_CONFIDENCE: this is reclassifying HISTORICAL observations
        # written under whatever floor was in effect at the time (0.40 for
        # every run this session), not re-scoring under whatever the constant
        # happens to be on a future install where this migration first runs.
        obs = conn.execute(
            """SELECT o.metadata_json FROM lead_provider_observations o
               JOIN leads l2 ON l2.id = o.lead_id
               WHERE l2.company_id = ? AND o.kind = 'domain_lookup'
               ORDER BY o.observed_at DESC LIMIT 1""",
            (row["company_id"],),
        ).fetchone()
        replacement = None
        if obs and obs["metadata_json"]:
            try:
                confidence = json.loads(obs["metadata_json"]).get("confidence")
            except (ValueError, TypeError):
                confidence = None
            if isinstance(confidence, (int, float)) and confidence < 0.40:
                replacement = "domain_low_confidence"
        conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["tag_id"],))
        if replacement:
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (f"wlt_{row['workspace_id']}_{row['lead_id']}_domlow",
                 row["workspace_id"], row["lead_id"], replacement),
            )
    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('domain_discovered_tag_reconcile')"
    )


def _migrate_provider_observations(conn: sqlite3.Connection) -> None:
    """Stage 7: fold lead_email_verification + lead_provider_attempts into one
    append-only lead_provider_observations log, then retire both old names to
    read-only VIEWs projecting "latest row per provider" (see
    provider_observations.COMPAT_VIEWS_SQL) so every existing reader --
    _lev_sources_for_lead, get_provider_attempts_map, has_attempted,
    _compute_verification_status, the CLI -- keeps working unchanged.

    Ordering: must run after the uid backfill above (obs_uid keys on the
    stable lead_uid, not the local autoincrement id, so a full pull replaying
    this history onto a wiped DB doesn't duplicate it) and before
    ensure_outbox (so lead_provider_observations gets an outbox trigger from
    the same migration that creates it -- installing the table without the
    trigger would recreate the exact "provider attempt bumps no parent
    timestamp" bug this whole effort exists to fix).

    Runs the table-create every time (cheap, idempotent); the rename+backfill
    only once, guarded by a migration flag, since it moves data out of tables
    that no longer exist on the second run.
    """
    from provider_observations import (
        COMPAT_VIEWS_SQL,
        KIND_PLATFORM_BOUNCE,
        ORIGIN_ATTEMPT,
        ORIGIN_VERIFICATION,
        TABLE_SQL,
        compute_obs_uid,
        kind_for_provider_domain,
    )

    conn.executescript(TABLE_SQL)

    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'provider_observations_unification'"
    ).fetchone():
        return

    lead_uids = {r["id"]: r["uid"] for r in conn.execute("SELECT id, uid FROM leads").fetchall()}

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lead_email_verification'"
    ).fetchone():
        for row in conn.execute("SELECT * FROM lead_email_verification").fetchall():
            lead_uid = lead_uids.get(row["lead_id"])
            if lead_uid is None:
                # Orphaned child row: lead_id no longer exists in leads (the
                # known apply_bulk_pull_pragmas FK-off bulk-pull artifact from
                # the Stage -1 audit). Dead data with no live parent to attach
                # to -- inserting it would violate lead_provider_observations'
                # FK, and there is nothing left in the app that could ever
                # join it to a real lead anyway. Drop it here rather than
                # carry it forward as a permanently orphaned observation.
                continue
            source = row["source"] or ""
            kind = KIND_PLATFORM_BOUNCE if source == "platform_bounce" else "email_verification"
            obs_uid = compute_obs_uid(
                row["org_id"], lead_uid, source, kind, ORIGIN_VERIFICATION, row["verified_at"],
                email=row["email"], status=row["status"], sub_status=row["sub_status"],
                source_detail=row["source_detail"], bounce_message=row["bounce_message"],
                free_email=row["free_email"], mx_found=row["mx_found"],
                smtp_provider=row["smtp_provider"],
            )
            conn.execute(
                """INSERT OR IGNORE INTO lead_provider_observations (
                       obs_uid, org_id, lead_id, kind, origin, provider, email, status,
                       sub_status, source_detail, bounce_message, free_email, mx_found,
                       smtp_provider, observed_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    obs_uid, row["org_id"], row["lead_id"], kind, ORIGIN_VERIFICATION, source,
                    row["email"], row["status"], row["sub_status"], row["source_detail"],
                    row["bounce_message"], row["free_email"], row["mx_found"],
                    row["smtp_provider"], row["verified_at"], row["created_at"],
                ),
            )
        for verb in ("insert", "update", "delete"):
            conn.execute(f"DROP TRIGGER IF EXISTS trg_outbox_lead_email_verification_{verb}")
        conn.execute("ALTER TABLE lead_email_verification RENAME TO lead_email_verification_legacy")

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lead_provider_attempts'"
    ).fetchone():
        from pipeline_provider_attempts import PROVIDER_DOMAINS

        for row in conn.execute("SELECT * FROM lead_provider_attempts").fetchall():
            lead_uid = lead_uids.get(row["lead_id"])
            if lead_uid is None:
                # Orphaned child row -- see the matching guard above.
                continue
            domain = row["domain"] or PROVIDER_DOMAINS.get(row["provider"])
            kind = kind_for_provider_domain(domain)
            obs_uid = compute_obs_uid(
                DEFAULT_ORG_ID, lead_uid, row["provider"], kind, ORIGIN_ATTEMPT, row["attempted_at"],
                status=row["status"], domain=domain, result_email=row["result_email"],
                result_validity=row["result_validity"], completed_at=row["completed_at"],
            )
            conn.execute(
                """INSERT OR IGNORE INTO lead_provider_observations (
                       obs_uid, org_id, lead_id, kind, origin, provider, domain, status,
                       result_email, result_validity, batch_id, metadata_json,
                       observed_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    obs_uid, DEFAULT_ORG_ID, row["lead_id"], kind, ORIGIN_ATTEMPT, row["provider"],
                    domain, row["status"], row["result_email"], row["result_validity"],
                    row["batch_id"], row["metadata_json"], row["attempted_at"], row["completed_at"],
                ),
            )
        for verb in ("insert", "update", "delete"):
            conn.execute(f"DROP TRIGGER IF EXISTS trg_outbox_lead_provider_attempts_{verb}")
        conn.execute("ALTER TABLE lead_provider_attempts RENAME TO lead_provider_attempts_legacy")

    conn.executescript(COMPAT_VIEWS_SQL)
    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('provider_observations_unification')"
    )


def _backfill_sender_account_activity(conn: sqlite3.Connection) -> None:
    """Seed last_outbound_at / last_inbound_at from existing event history.

    One pass over events, once -- log_event maintains the columns from then on.
    Guarded by a migration flag because migrate_db runs on every init_db and this
    scans the whole events table.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'sender_activity_backfill'"
    ).fetchone():
        return
    conn.execute("""
        WITH agg AS (
            SELECT lower(sender) AS s,
                   MAX(CASE WHEN direction = 'outbound' THEN created_at END) AS ob,
                   MAX(CASE WHEN direction = 'inbound'  THEN created_at END) AS ib
              FROM events
             WHERE sender IS NOT NULL AND trim(sender) != ''
             GROUP BY lower(sender)
        )
        UPDATE sender_accounts SET
            last_outbound_at = (SELECT ob FROM agg WHERE agg.s = lower(sender_accounts.email)),
            last_inbound_at  = (SELECT ib FROM agg WHERE agg.s = lower(sender_accounts.email))
         WHERE lower(email) IN (SELECT s FROM agg)
    """)
    conn.execute("INSERT INTO migration_flags (name) VALUES ('sender_activity_backfill')")


def _backfill_current_sentiment_since(conn: sqlite3.Connection) -> None:
    """Seed current_sentiment_since from event history for legacy rows.

    For each workspace_lead, current_sentiment_since is the start of the
    *current* contiguous run of its materialized current_status_sentiment: the
    earliest sentiment event that is (a) the same sentiment as the lead's
    current one and (b) later than the most recent event of a *different*
    sentiment. A flip away and back therefore anchors on the latest re-entry.

    Guarded on "any row still needs it" (sentiment set, since NULL) rather than
    a permanent flag: ingest stamps the column going forward, so after the first
    pass this is a LIMIT-1 no-op. event_at compares lexicographically as ISO
    text, and '' sorts before any timestamp, so a lead that never changed
    sentiment (no boundary row) takes MIN over its whole history.

    THE GUARD AND THE UPDATE MUST SELECT THE SAME ROWS, or this never converges.
    They used to disagree: the guard asked "is any row missing an anchor?" while
    the UPDATE could only supply one from a matching sentiment *event*. A lead
    whose sentiment was set directly (a manual stage change, not a webhook) has
    no such event, so the subquery returned NULL, the UPDATE wrote NULL over
    NULL, and the row still qualified on the next pass -- forever.

    276 rows were in that state. Because SQLite fires AFTER UPDATE even when no
    value changes, every one of them re-stamped its outbox row on EVERY
    pipeline.py invocation: `dirty_at` reset to now and `attempts` reset to 0.
    The outbox therefore never reached zero, every sync rebuilt ~276 workspace
    and ~241 core payloads only to discard them as echoes, and an expensive
    correlated subquery over the whole sentiment set ran on every CLI command.

    Restricting both the guard and the UPDATE to rows that HAVE an anchor makes
    it converge in one pass. Rows with no anchorable event keep NULL, which is
    honest -- and if a sentiment event ever does arrive for one, ingest stamps
    the column at that point (relay_ingest), so nothing is lost by not retrying.
    """
    def anchor(row: str) -> str:
        """The anchor subquery, correlated to `row` (the outer workspace_leads)."""
        return f"""
        SELECT MIN(wle.event_at)
        FROM workspace_lead_events wle JOIN events e ON e.id = wle.event_id
        WHERE wle.workspace_id = {row}.workspace_id
          AND wle.lead_id = {row}.lead_id
          AND lower(json_extract(e.metadata_json, '$.lead_status_sentiment'))
              = lower({row}.current_status_sentiment)
          AND wle.event_at > COALESCE((
              SELECT MAX(wle2.event_at)
              FROM workspace_lead_events wle2 JOIN events e2 ON e2.id = wle2.event_id
              WHERE wle2.workspace_id = {row}.workspace_id
                AND wle2.lead_id = {row}.lead_id
                AND json_extract(e2.metadata_json, '$.lead_status_sentiment') IS NOT NULL
                AND lower(json_extract(e2.metadata_json, '$.lead_status_sentiment'))
                    <> lower({row}.current_status_sentiment)
          ), '')
        """

    def qualifies(row: str) -> str:
        return f"""
            {row}.current_status_sentiment IS NOT NULL
        AND trim({row}.current_status_sentiment) != ''
        AND {row}.current_sentiment_since IS NULL
        AND ({anchor(row)}) IS NOT NULL
        """

    # Cheap pre-check on workspace_leads alone, before anything touches the
    # event tables. On a fresh database there are no rows at all, and this must
    # return before the anchor subquery is even parsed -- during init_db the
    # event schema is not necessarily in its final shape yet.
    if not conn.execute(
        "SELECT 1 FROM workspace_leads "
        "WHERE current_status_sentiment IS NOT NULL "
        "  AND trim(current_status_sentiment) != '' "
        "  AND current_sentiment_since IS NULL LIMIT 1"
    ).fetchone():
        return
    if not conn.execute(
        f"SELECT 1 FROM workspace_leads wl WHERE {qualifies('wl')} LIMIT 1"
    ).fetchone():
        return
    conn.execute(f"""
        UPDATE workspace_leads
           SET current_sentiment_since = ({anchor('workspace_leads')})
         WHERE {qualifies('workspace_leads')}
    """)


def _repair_sales_nav_id_casing(conn: sqlite3.Connection) -> None:
    """Unify 'ACwAA...' and 'acwaa...' -- they're the same person, and the local
    store kept them as two.

    37,536 of 54,154 identities had been lowercased somewhere upstream while
    16,618 retained mixed case. The UNIQUE (org_id, identity_type,
    identity_value_normalized) constraint compares BINARY, so the two forms
    coexisted as separate rows, split ~28k people into two leads apiece, and
    passed straight through the identity-guard tests.

    Model going forward: identity_value_normalized is case-folded (match key,
    enforced at write in upsert_all_identities / upsert_identity_alias);
    leads.linkedin_sales_nav_id is the canonical mixed case (display + outbound
    alias). This migration merges the split pairs, promotes the best available
    case onto the survivor's leads column, then dedupes and case-folds the
    identity rows. Guarded because it's O(n) over lead_identities.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'sales_nav_id_casing_repair'"
    ).fetchone():
        return

    from pipeline import _pick_merge_keep_id, merge_leads

    # Groups of leads that own the same sales-nav id under different casing --
    # exactly the split we're here to reunite. Merge folds each into a survivor.
    duplicate_groups = conn.execute(
        """SELECT LOWER(identity_value_normalized) AS key,
                  GROUP_CONCAT(DISTINCT lead_id) AS lead_ids
             FROM lead_identities
            WHERE identity_type = 'linkedin_sales_nav_id'
            GROUP BY org_id, LOWER(identity_value_normalized)
           HAVING COUNT(DISTINCT lead_id) > 1"""
    ).fetchall()

    for group in duplicate_groups:
        ids = sorted({int(x) for x in (group["lead_ids"] or "").split(",") if x})
        if len(ids) < 2:
            continue
        # Pick the best mixed-case display value from the whole group BEFORE the
        # merge -- once the losers are deleted, their case is gone. Any value
        # that differs from its own lower() form is mixed-case; take the first
        # one we find (Sales Nav ids are one canonical spelling, so any mixed-
        # case form we've seen is the right one).
        canonical = None
        for cur_id in ids:
            row = conn.execute(
                "SELECT linkedin_sales_nav_id FROM leads WHERE id = ?", (cur_id,),
            ).fetchone()
            val = (row["linkedin_sales_nav_id"] if row else None) or ""
            if val and val != val.lower():
                canonical = val
                break

        survivor = ids[0]
        for other in ids[1:]:
            keep_id, merge_id = _pick_merge_keep_id(conn, survivor, other)
            merge_leads(keep_id, merge_id, reason="sales_nav_id_case_dupe", conn=conn)
            survivor = keep_id

        # Stamp the mixed-case value on the survivor so a survivor that was
        # picked for having more events (but happened to be lowercase) doesn't
        # discard the canonical form its merged-in twin had.
        if canonical:
            conn.execute(
                """UPDATE leads
                      SET linkedin_sales_nav_id = ?, updated_at = datetime('now')
                    WHERE id = ?""",
                (canonical, survivor),
            )

    # After merges, one lead can own both a lowercase and a mixed-case identity
    # row (the merge moved lead_id but didn't collapse identity_value_normalized).
    # Delete the redundant rows keeping the earliest -- that row's created_at is
    # the honest "first seen" timestamp for the identity.
    conn.execute(
        """DELETE FROM lead_identities
            WHERE identity_type = 'linkedin_sales_nav_id'
              AND id NOT IN (
                  SELECT MIN(id) FROM lead_identities
                   WHERE identity_type = 'linkedin_sales_nav_id'
                   GROUP BY org_id, lead_id, LOWER(identity_value_normalized)
              )"""
    )

    conn.execute(
        """UPDATE lead_identities
              SET identity_value_normalized = LOWER(identity_value_normalized)
            WHERE identity_type = 'linkedin_sales_nav_id'
              AND identity_value_normalized != LOWER(identity_value_normalized)"""
    )

    # leads.linkedin_sales_nav_id is left alone: the merge step above already
    # stamped the best available case on each survivor, and rows that never had
    # a duplicate keep whatever they had. Runtime upgrade (see
    # _upgrade_lead_sales_nav_id_case) picks up any better casing that arrives
    # later -- including from the next full D1 pull, which carries the original
    # mixed-case values.

    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('sales_nav_id_casing_repair')"
    )


# Values that describe the *transport* by which a lead reached us, not *where
# the lead came from*. Historically every relay-ingested lead had its
# original_source overwritten with "relay_sync" and its original_source_platform
# with "relay" -- so ~85% of the ~150k leads read as if their provenance were
# "relay", which is a useless answer to "where did this person come from". The
# guard below aborts any INSERT/UPDATE that would put one of these back into a
# provenance column.
_TRANSPORT_STRINGS = ("agent_sync", "relay_sync", "relay")


def _add_lead_record_type(conn: sqlite3.Connection) -> dict:
    """`leads.record_type`: is this row a person, or a stand-in for a company?

    Google Maps / Apify scrapes are lists of businesses, not people. Importing
    them as leads with name = company_name gives you a "contact" that is really
    an account: no email, no LinkedIn, nobody to send to. That is a legitimate
    stage -- you import the list, then research actual contacts at each company
    -- but the difference has to be recorded somewhere every query can see it.

    A native column, NOT a `personalized_record_type` field, because:
      * personalization is a user namespace; a client can legitimately create a
        field called record_type and collide with structural meaning,
      * import turns any unrecognised CSV column into personalization, so a
        stray header could silently reclassify leads (this is exactly how
        original_source ended up shadowed),
      * it has to ride the relay snapshot for the dashboard and CRM to filter
        on it, and
      * every send/enrich eligibility check would otherwise need a join to
        lead_personalization.

    Vocabulary is validated in code rather than by CHECK, so it can grow without
    a table rebuild. The index is partial: ~zero cost for the 99% 'contact' case.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "record_type" not in cols:
        conn.execute(
            "ALTER TABLE leads ADD COLUMN record_type TEXT NOT NULL DEFAULT 'contact'")
    if "superseded_at" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN superseded_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_record_type ON leads(record_type) "
        "WHERE record_type <> 'contact'"
    )

    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'lead_record_type_fold'"
    ).fetchone():
        return {"skipped": True}

    # Fold the personalization field that was standing in for this column.
    folded = conn.execute(
        """UPDATE leads SET record_type = (
               SELECT lower(TRIM(p.field_value)) FROM lead_personalization p
                WHERE p.lead_id = leads.id AND p.field_name = 'record_type'
                  AND lower(TRIM(p.field_value)) IN ('contact', 'company_placeholder')
           )
           WHERE EXISTS (
               SELECT 1 FROM lead_personalization p
                WHERE p.lead_id = leads.id AND p.field_name = 'record_type'
                  AND lower(TRIM(p.field_value)) IN ('contact', 'company_placeholder')
           )"""
    ).rowcount
    dropped = conn.execute(
        "DELETE FROM lead_personalization WHERE field_name = 'record_type'").rowcount

    # Catch the rest of the same import: a lead whose name IS its company, with
    # no email, no LinkedIn and no history, is a company stub whether or not
    # anyone remembered to tag it.
    #
    # Candidates come from SQL, but the decision runs through the SAME Python
    # predicate the importer uses. A second copy of the rule in SQL is how the
    # two drift, and the drift is not symmetric: an over-eager rule here hides
    # real people from sending, enrichment and CRM sync without saying so.
    from pipeline import detect_company_placeholder

    candidates = conn.execute(
        """SELECT id, name, company FROM leads
            WHERE record_type = 'contact'
              AND company IS NOT NULL AND TRIM(company) != ''
              AND TRIM(lower(name)) = TRIM(lower(company))
              AND COALESCE(TRIM(email), '') = ''
              AND COALESCE(TRIM(linkedin_url), '') = ''
              AND COALESCE(TRIM(linkedin_sales_nav_id), '') = ''
              AND COALESCE(TRIM(title), '') = ''
              AND NOT EXISTS (SELECT 1 FROM events e WHERE e.lead_id = leads.id)"""
    ).fetchall()
    hits = [
        (r["id"],) for r in candidates
        if detect_company_placeholder({"name": r["name"], "company": r["company"]}, {})
    ]
    if hits:
        conn.executemany(
            "UPDATE leads SET record_type = 'company_placeholder' WHERE id = ?", hits)
    detected = len(hits)

    conn.execute("INSERT INTO migration_flags (name) VALUES ('lead_record_type_fold')")
    stats = {"folded": folded, "shadow_rows_dropped": dropped, "auto_detected": detected}
    if folded or detected:
        print(
            f"[outreachmagic] record_type: {folded:,} folded from personalization, "
            f"{detected:,} auto-detected as company placeholders",
            file=sys.stderr,
        )
    return stats


def _add_company_identity_purpose(conn: sqlite3.Connection) -> None:
    """`company_identities.purpose` — what a prospect's domain is FOR.

    Purpose lived on `sender_domains` (a company_id + purpose pair added in an
    earlier stage), which put two unrelated things behind one word: your own
    cold-email sending infrastructure, and a prospect company's set of known
    domains. The company pane rendered both as "this company's domains", so the
    same heading meant two different tables.

    Purpose belongs on the alias set email finding actually walks. Nothing in
    production ever used the sender_domains company link (every row is
    company_id IS NULL), so this needs no data migration -- the columns stay on
    sender_domains, they just stop being surfaced as a company's domains.

    Vocabulary is validated in code, not by CHECK, so it can grow without a
    table rebuild -- same discipline as leads.record_type. Backfill marks the
    company's own `companies.domain` as primary; everything else stays NULL
    ("unclassified") rather than being guessed into a category.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(company_identities)").fetchall()}
    if "purpose" not in cols:
        conn.execute("ALTER TABLE company_identities ADD COLUMN purpose TEXT")
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'company_identity_purpose_backfill'"
    ).fetchone():
        return
    conn.execute(
        """UPDATE company_identities SET purpose = 'primary'
            WHERE identity_type = 'domain' AND purpose IS NULL
              AND EXISTS (SELECT 1 FROM companies c
                           WHERE c.id = company_identities.company_id
                             AND LOWER(c.domain) = LOWER(company_identities.identity_value_normalized))"""
    )
    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('company_identity_purpose_backfill')")


def _repair_keyless_workspace_tombstones(conn: sqlite3.Connection) -> int:
    """Recover lead_workspace tombstones whose entity_key came out NULL.

    Cascade-deleted workspace_leads rows filed a tombstone with no entity_key
    (see trg_outbox_leads_delete_workspace_tombstones for why). Unpushable, so
    they accumulate in the outbox forever while the relay keeps serving the
    snapshot they were meant to retract.

    leads_junk_quarantine kept (lead_id, uid) for exactly this sort of
    after-the-fact recovery. Rows we still cannot key are dropped: an
    un-keyable tombstone can never be delivered, and leaving it queued only
    makes 'pending' permanently wrong.
    """
    keyless = conn.execute(
        "SELECT COUNT(*) n FROM outbox "
        "WHERE entity_type = 'lead_workspace' AND op = 'delete' "
        "  AND (entity_key IS NULL OR entity_key = '')"
    ).fetchone()["n"]
    if not keyless:
        return 0

    conn.execute(
        """UPDATE outbox SET entity_key = (
               SELECT q.uid FROM leads_junk_quarantine q
                WHERE q.lead_id = CAST(substr(outbox.entity_id, 1,
                        instr(outbox.entity_id, ':') - 1) AS INTEGER)
                  AND q.uid IS NOT NULL AND q.uid != ''
                ORDER BY q.id DESC LIMIT 1
           )
           WHERE entity_type = 'lead_workspace' AND op = 'delete'
             AND (entity_key IS NULL OR entity_key = '')
             AND instr(entity_id, ':') > 1"""
    )
    dropped = conn.execute(
        "DELETE FROM outbox WHERE entity_type = 'lead_workspace' AND op = 'delete' "
        "  AND (entity_key IS NULL OR entity_key = '')"
    ).rowcount
    recovered = keyless - dropped
    if recovered or dropped:
        print(
            f"[outreachmagic] repaired {recovered:,} workspace tombstone(s); "
            f"dropped {dropped:,} that could not be keyed",
            file=sys.stderr,
        )
    return recovered


# Placeholder provenance written by the importer when the CSV said nothing
# specific. A real value from the row beats it; a genuine user-chosen source
# does not.
_GENERIC_SOURCES = ("csv_import", "")


def _fold_shadow_source_personalization(conn: sqlite3.Connection) -> dict:
    """Fold `personalized_original_source` & co. back onto the real columns.

    Until CANONICAL_SOURCE_IMPORT_FIELDS existed, a CSV column literally headed
    `original_source` was not recognised as provenance: it fell through to the
    personalization loop and became a `lead_personalization` row, while the real
    leads.original_source kept the importer's `csv_import` placeholder. 1,285
    leads were in that state in production -- their true origin (google_maps)
    stored in the shadow, the wrong value in the column every report groups by.

    Conservative on purpose, because provenance is not re-derivable:
      * fill any real column that is empty;
      * replace original_source only when it still holds the importer's generic
        placeholder and the shadow is specific;
      * NEVER overwrite a specific latest_source -- "most recent touch" may well
        have been set by a later, legitimate import;
      * drop a shadow row only once its value is actually on the lead.

    Flag-guarded, so rows we deliberately declined to overwrite are not
    re-examined on every startup (see _backfill_current_sentiment_since for what
    a non-converging migration costs).
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'shadow_source_personalization_fold'"
    ).fetchone():
        return {"skipped": True}

    generic = ",".join("?" for _ in _GENERIC_SOURCES)
    transport = ",".join("?" for _ in _TRANSPORT_STRINGS)
    stats: dict = {}

    def shadow(field: str) -> str:
        return f"""
            SELECT p.field_value FROM lead_personalization p
             WHERE p.lead_id = leads.id AND p.field_name = '{field}'
               AND p.field_value IS NOT NULL AND TRIM(p.field_value) != ''
               AND p.field_value NOT IN ({transport})
        """

    # 1. original_source_detail: fill only where empty.
    cur = conn.execute(
        f"""UPDATE leads SET original_source_detail = ({shadow('original_source_detail')})
             WHERE (original_source_detail IS NULL OR TRIM(original_source_detail) = '')
               AND ({shadow('original_source_detail')}) IS NOT NULL""",
        (*_TRANSPORT_STRINGS, *_TRANSPORT_STRINGS),
    )
    stats["original_source_detail_filled"] = cur.rowcount

    # 2. latest_source / latest_source_detail move as a PAIR. Filling the detail
    # from this import's shadow while the source column still names a different,
    # later import produces a row that reads "latest touch: list-B, described by:
    # list-A" -- worse than the NULL it replaced.
    pair_ok = f"""
        (latest_source IS NULL OR TRIM(latest_source) = ''
         OR latest_source = ({shadow('latest_source')}))
    """
    for col in ("latest_source", "latest_source_detail"):
        cur = conn.execute(
            f"""UPDATE leads SET {col} = ({shadow(col)})
                 WHERE ({col} IS NULL OR TRIM({col}) = '')
                   AND ({shadow(col)}) IS NOT NULL
                   AND {pair_ok}""",
            (*_TRANSPORT_STRINGS, *_TRANSPORT_STRINGS, *_TRANSPORT_STRINGS),
        )
        stats[f"{col}_filled"] = cur.rowcount

    # 2. original_source: fill when empty, and displace the generic placeholder.
    cur = conn.execute(
        f"""UPDATE leads SET original_source = ({shadow('original_source')})
             WHERE COALESCE(TRIM(original_source), '') IN ({generic})
               AND ({shadow('original_source')}) IS NOT NULL""",
        (*_TRANSPORT_STRINGS, *_GENERIC_SOURCES, *_TRANSPORT_STRINGS),
    )
    stats["original_source_set"] = cur.rowcount

    # 3. Retire only the shadows whose value now lives on the lead.
    cur = conn.execute(
        """DELETE FROM lead_personalization
            WHERE field_name IN ('original_source', 'original_source_detail',
                                 'latest_source', 'latest_source_detail')
              AND EXISTS (
                  SELECT 1 FROM leads l
                   WHERE l.id = lead_personalization.lead_id
                     AND CASE lead_personalization.field_name
                           WHEN 'original_source'        THEN l.original_source
                           WHEN 'original_source_detail' THEN l.original_source_detail
                           WHEN 'latest_source'          THEN l.latest_source
                           WHEN 'latest_source_detail'   THEN l.latest_source_detail
                         END = lead_personalization.field_value
              )"""
    )
    stats["shadow_rows_retired"] = cur.rowcount
    stats["shadow_rows_kept"] = conn.execute(
        """SELECT COUNT(*) n FROM lead_personalization
            WHERE field_name IN ('original_source', 'original_source_detail',
                                 'latest_source', 'latest_source_detail')"""
    ).fetchone()["n"]

    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('shadow_source_personalization_fold')"
    )
    return stats


def _repair_provenance_transport_strings(conn: sqlite3.Connection) -> None:
    """One-time backfill: NULL out provenance columns that hold transport strings.

    An unknown provenance is honest; a wrong one silently misleads every report
    that groups by original_source. Only the four *_source / *_source_platform
    columns on leads are touched -- source_detail carries the real hint
    (campaign name, list name), so it stays untouched even if the source itself
    was garbage.
    """
    if conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'provenance_transport_backfill'"
    ).fetchone():
        return

    placeholders = ",".join("?" for _ in _TRANSPORT_STRINGS)
    for col in (
        "original_source",
        "latest_source",
        "original_source_platform",
        "latest_source_platform",
    ):
        conn.execute(
            f"""UPDATE leads
                   SET {col} = NULL,
                       updated_at = datetime('now')
                 WHERE {col} IN ({placeholders})""",
            _TRANSPORT_STRINGS,
        )
    conn.execute(
        "INSERT INTO migration_flags (name) VALUES ('provenance_transport_backfill')"
    )


def _install_provenance_transport_guard(conn: sqlite3.Connection) -> None:
    """RAISE(ABORT) if any code path tries to write a transport string back into
    a provenance column. The backfill above only cleans what's already there;
    without this guard, the next relay ingest that (re)introduces "relay_sync"
    would silently re-dirty the columns and the report drift would come back.
    """
    values_sql = ", ".join(f"'{v}'" for v in _TRANSPORT_STRINGS)
    for col in (
        "original_source",
        "latest_source",
        "original_source_platform",
        "latest_source_platform",
    ):
        # BEFORE INSERT and BEFORE UPDATE, one trigger each per column, guarded
        # so a NULL or an unchanged value never fires -- the abort only sees
        # actual attempts to install a transport string as provenance.
        for op, new_ref in (("INSERT", "NEW"), ("UPDATE OF " + col, "NEW")):
            trigger_name = f"trg_leads_{col}_transport_guard_{op.split()[0].lower()}"
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            conn.execute(f"""
                CREATE TRIGGER {trigger_name}
                BEFORE {op} ON leads
                FOR EACH ROW
                WHEN {new_ref}.{col} IN ({values_sql})
                BEGIN
                    SELECT RAISE(ABORT,
                        'transport string in leads.{col} -- agent_sync/relay_sync/relay are transport, not provenance; pass the inbound platform instead');
                END
            """)


def _add_campaigns_workspace_id(conn: sqlite3.Connection) -> None:
    """Add workspace_id FK to campaigns and backfill from the name prefix.

    Campaign names use the convention '{workspace_slug} | {campaign_name}'.
    This migration is idempotent: it re-runs the backfill on each call so that
    campaigns added after the first migration run also get their workspace_id set.
    Campaigns without a matching prefix are left as NULL.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "workspace_id" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL")

    ws_rows = conn.execute(
        "SELECT id, slug FROM workspaces WHERE slug != 'default'"
    ).fetchall()
    slug_to_id = {row["slug"]: row["id"] for row in ws_rows}
    if not slug_to_id:
        return

    campaigns = conn.execute(
        "SELECT id, name FROM campaigns WHERE workspace_id IS NULL"
    ).fetchall()
    for camp in campaigns:
        name = camp["name"] or ""
        if " | " not in name:
            continue
        prefix = name.split(" | ", 1)[0].strip().lower()
        ws_id = slug_to_id.get(prefix)
        if ws_id:
            conn.execute(
                "UPDATE campaigns SET workspace_id = ? WHERE id = ?",
                (ws_id, camp["id"]),
            )


def migrate_db(conn=None):
    """Apply incremental schema changes and backfill derived data."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            domain TEXT,
            industry TEXT,
            headcount TEXT,
            headcount_numeric INTEGER,
            hq_city TEXT,
            hq_state TEXT,
            hq_country TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS lead_merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keep_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            merge_id INTEGER NOT NULL,
            reason TEXT,
            merged_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS migration_flags (
            name TEXT PRIMARY KEY,
            done_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        -- Stage 9 pre-image of leads deleted by cleanup_junk_leads. Only the
        -- original_source_detail is worth preserving (list / import name);
        -- everything else on the row was null or 'Unknown'.
        CREATE TABLE IF NOT EXISTS leads_junk_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            uid TEXT,
            original_source_detail TEXT,
            quarantined_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            cloud_synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, slug)
        );
        CREATE TABLE IF NOT EXISTS lead_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            identity_type TEXT NOT NULL,
            identity_value_normalized TEXT NOT NULL,
            source TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, identity_type, identity_value_normalized)
        );
        -- Tiered company identities, mirroring lead_identities. 'domain' rows
        -- are one-to-many per company (a real company can send mail from a
        -- different domain than its website, or from several per-branch
        -- domains) -- the UNIQUE constraint keeps one exact domain string
        -- mapped to exactly one company while letting a company own several
        -- such rows. role/verified_mx/source are nullable and opportunistic:
        -- they rank candidate domains for email-finding (rank_company_domains)
        -- but are never required or enforced. identity_type STRONG tier
        -- ('domain', 'linkedin_company_id') is safe to auto-match on; MEDIUM
        -- ('linkedin_company_url') gets tracked but conflicts are logged to
        -- company_merge_candidates rather than auto-resolved, since the same
        -- domain can map to more than one LinkedIn company page; WEAK
        -- ('name_normalized') never auto-attaches anything on its own.
        CREATE TABLE IF NOT EXISTS company_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            identity_type TEXT NOT NULL,
            identity_value_normalized TEXT NOT NULL,
            role TEXT,
            verified_mx INTEGER,
            source TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, identity_type, identity_value_normalized)
        );
        CREATE INDEX IF NOT EXISTS idx_company_identities_company_type ON company_identities(company_id, identity_type);
        -- Human-review queue for ambiguous company matches (name-only fallback
        -- hits, LinkedIn-URL/domain conflicts, backfill audit findings). Shape
        -- mirrors unmapped_campaign_queue: pending -> resolved/dismissed, full
        -- context kept in payload_json. Never auto-resolved -- see
        -- ensure_company()'s name-only fallback and merge_companies().
        CREATE TABLE IF NOT EXISTS company_merge_candidates (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            candidate_company_id INTEGER,
            existing_company_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payload_json TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_company_merge_candidates_status ON company_merge_candidates(status);
        CREATE TABLE IF NOT EXISTS company_merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keep_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            merge_id INTEGER NOT NULL,
            reason TEXT,
            merge_entity_key TEXT,
            relay_delete_pushed INTEGER NOT NULL DEFAULT 0,
            merged_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS workspace_leads (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'prospecting',
            owner_user_id TEXT,
            stage_entered_at TEXT,
            last_activity_at TEXT,
            current_status_label TEXT,
            current_status_sentiment TEXT,
            current_sentiment_since TEXT,
            contact_priority INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id)
        );
        -- Must stay in step with schema.py: no payload_json here, content lives
        -- in `events` and is reached via event_id.
        CREATE TABLE IF NOT EXISTS workspace_lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL,
            event_id INTEGER,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS campaign_workspace_map (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            campaign_platform_id TEXT,
            campaign_name_normalized TEXT,
            workspace_id TEXT NOT NULL,
            match_strategy TEXT NOT NULL DEFAULT 'id_exact',
            priority INTEGER NOT NULL DEFAULT 100,
            is_active INTEGER NOT NULL DEFAULT 1,
            cloud_synced INTEGER NOT NULL DEFAULT 0,
            map_source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS unmapped_campaign_queue (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            campaign_platform_id TEXT,
            campaign_name_raw TEXT,
            campaign_name_normalized TEXT,
            external_event_id TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payload_json TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            assigned_workspace TEXT
        );
        CREATE TABLE IF NOT EXISTS lead_merge_jobs (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            keep_lead_id INTEGER NOT NULL,
            merge_lead_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            reason TEXT,
            audit_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS workspace_lead_tags (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_wlt_workspace_tag ON workspace_lead_tags(workspace_id, tag);
        CREATE INDEX IF NOT EXISTS idx_wlt_lead ON workspace_lead_tags(lead_id);
        CREATE INDEX IF NOT EXISTS idx_wlt_tag_ws_lead ON workspace_lead_tags(tag, workspace_id, lead_id);
        CREATE TABLE IF NOT EXISTS workspace_lead_linkedin_status (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            sender_profile TEXT NOT NULL,
            is_connected INTEGER NOT NULL DEFAULT 0,
            is_request_pending INTEGER NOT NULL DEFAULT 0,
            connected_at TEXT,
            request_sent_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id, sender_profile)
        );
        CREATE INDEX IF NOT EXISTS idx_li_status_workspace ON workspace_lead_linkedin_status(workspace_id, sender_profile);
        CREATE INDEX IF NOT EXISTS idx_li_status_lead ON workspace_lead_linkedin_status(lead_id);
        -- lead_email_verification used to be created here. Stage 7 folded it into
        -- lead_provider_observations and retired this name to a read-only VIEW
        -- (see _migrate_provider_observations below) -- creating it as a table
        -- here would collide with that VIEW on every DB that has migrated.
        CREATE TABLE IF NOT EXISTS bounce_events (
            id                  TEXT PRIMARY KEY,
            org_id              TEXT NOT NULL,
            lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            first_event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
            latest_event_id     INTEGER REFERENCES events(id) ON DELETE SET NULL,
            platform            TEXT NOT NULL,
            sender_email        TEXT NOT NULL,
            lead_email          TEXT NOT NULL,
            bounce_type         TEXT NOT NULL DEFAULT 'unknown',
            bounce_message      TEXT,
            smtp_code           TEXT,
            recipient_mx        TEXT,
            sender_mx           TEXT,
            campaign_id         INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
            campaign_name       TEXT,
            workspace_id        TEXT,
            relay_id            TEXT,
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (lead_id, sender_email)
        );
        CREATE INDEX IF NOT EXISTS idx_bounce_events_lead ON bounce_events(lead_id);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_platform ON bounce_events(platform, bounce_type);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_sender ON bounce_events(sender_email);
        CREATE INDEX IF NOT EXISTS idx_bounce_events_seen ON bounce_events(last_seen_at DESC);
    """)
    for col, col_type in [
        ("industry", "TEXT"), ("headcount", "TEXT"), ("email_domain", "TEXT"),
        ("company_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """UPDATE leads SET email_domain = lower(substr(email, instr(email, '@') + 1))
           WHERE email LIKE '%@%' AND (email_domain IS NULL OR email_domain = '')"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_email_domain ON leads(email_domain)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_identities_lead_type ON lead_identities(lead_id, identity_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_identities_type ON lead_identities(identity_type, lead_id)"
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_lead_identities_type_value_lower
           ON lead_identities(org_id, identity_type, LOWER(identity_value_normalized))"""
    )
    try:
        conn.execute(
            "ALTER TABLE events ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_lead_created ON events(lead_id, created_at DESC)"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_name ON campaigns(name)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(email) WHERE email IS NOT NULL"
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique
           ON leads(linkedin_url) WHERE linkedin_url IS NOT NULL"""
    )
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN linkedin_sales_nav_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_sales_nav_id_unique
           ON leads(linkedin_sales_nav_id) WHERE linkedin_sales_nav_id IS NOT NULL"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company_name ON leads(company)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_companies_name_lower ON companies(lower(name))"
    )
    # workspace_leads' other indexes all lead with workspace_id; lead_id-only
    # lookups (relay pull's stage-downgrade guard, merge_leads, activity_sync)
    # were falling back to a full table scan that grows with total lead count.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_leads_lead_id ON workspace_leads(lead_id)"
    )
    from pipeline import backfill_campaigns_from_events, backfill_plusvibe_status_metadata

    backfill_campaigns_from_events(conn)
    backfill_plusvibe_status_metadata(conn)
    for col, col_type in [
        ("workspace_routing_mode", "TEXT NOT NULL DEFAULT 'single'"),
        ("default_workspace_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE organizations ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    # Must run before backfill_workspace_routing(): that call writes map_source
    # via assign_campaign_map(), so the column has to exist first or a fresh
    # single-mode migration aborts with OperationalError.
    try:
        conn.execute(
            "ALTER TABLE campaign_workspace_map ADD COLUMN map_source TEXT NOT NULL DEFAULT 'manual'"
        )
    except sqlite3.OperationalError:
        pass
    # Same reason as map_source above: backfill_workspace_routing() calls
    # upsert_workspace_lead(), whose INSERT now names current_sentiment_since, so
    # the column must exist first on an already-created workspace_leads. The
    # ALTER is repeated (idempotently) in the column loop further down.
    try:
        conn.execute("ALTER TABLE workspace_leads ADD COLUMN current_sentiment_since TEXT")
    except sqlite3.OperationalError:
        pass
    backfill_workspace_routing(conn)
    for tbl in ("workspaces", "campaign_workspace_map"):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN cloud_synced INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("merge_entity_key", "TEXT"),
        ("relay_delete_pushed", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE lead_merges ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("assigned_workspace", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE unmapped_campaign_queue ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE lead_personalization ADD COLUMN field_date TEXT")
    except sqlite3.OperationalError:
        pass
    for col, col_type in [
        ("original_source", "TEXT"),
        ("original_source_detail", "TEXT"),
        ("original_source_platform", "TEXT"),
        ("original_source_at", "TEXT"),
        ("latest_source", "TEXT"),
        ("latest_source_detail", "TEXT"),
        ("latest_source_platform", "TEXT"),
        ("latest_source_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("current_status_label", "TEXT"),
        ("current_status_sentiment", "TEXT"),
        ("contact_priority", "INTEGER"),
        # Timestamp the lead ENTERED its current contiguous sentiment run. A
        # flip away and back resets it to the latest entry. Materialized so the
        # campaigns view can GROUP BY date(current_sentiment_since) instead of
        # rescanning the event stream; maintained at ingest + carried in the
        # workspace snapshot. Backfilled below from the event history.
        ("current_sentiment_since", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE workspace_leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    _backfill_current_sentiment_since(conn)
    for col, col_type in [
        ("headcount_numeric", "INTEGER"),
        ("location_city", "TEXT"),
        ("location_state", "TEXT"),
        ("location_country", "TEXT"),
        ("email_verification_status", "TEXT"),
        ("email_verified_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("linkedin_headline", "TEXT"),
        ("linkedin_bio", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in [
        ("headcount_numeric", "INTEGER"),
        ("hq_city", "TEXT"),
        ("hq_state", "TEXT"),
        ("hq_country", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    # Backfill once when companies is empty (avoid re-scanning all leads on every pull).
    if conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 0:
        from pipeline import backfill_companies_from_leads

        backfill_companies_from_leads(conn)
    for col, col_type in [
        ("latest_sender", "TEXT"),
        ("latest_sender_platform", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE events ADD COLUMN sender TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE events ADD COLUMN relay_id INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_relay ON events(relay_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relay_ingested_lead_id ON relay_ingested(lead_id)")
    conn.execute(
        """UPDATE events SET relay_id = json_extract(metadata_json, '$.relay_id')
           WHERE relay_id IS NULL AND json_extract(metadata_json, '$.relay_id') IS NOT NULL"""
    )
    try:
        conn.execute("ALTER TABLE workspace_leads ADD COLUMN latest_sender TEXT")
    except sqlite3.OperationalError:
        pass
    for col, col_type in [
        ("email_sent_count", "INTEGER NOT NULL DEFAULT 0"),
        ("linkedin_sent_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_replies_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_contacted_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE workspace_leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """UPDATE workspace_leads
           SET last_contacted_at = last_activity_at
           WHERE last_contacted_at IS NULL AND last_activity_at IS NOT NULL"""
    )
    repair_malformed_tags(conn)
    if not conn.execute(
        "SELECT 1 FROM migration_flags WHERE name = 'bounce_events_backfill'"
    ).fetchone():
        backfill_bounce_events_from_events(conn)
        conn.execute(
            "INSERT INTO migration_flags (name) VALUES ('bounce_events_backfill')"
        )
    _add_campaigns_workspace_id(conn)
    from pipeline import maybe_backfill_null_campaign_quarantine

    maybe_backfill_null_campaign_quarantine(quiet=True, conn=conn)
    ensure_read_views(conn)

    # CRM sync tables (Phase 0)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS crm_workspace_config (
            workspace_id         TEXT NOT NULL,
            platform             TEXT NOT NULL,
            api_key              TEXT NOT NULL,
            location_id          TEXT,
            pipeline_id          TEXT,
            stage_mapping        TEXT NOT NULL DEFAULT '{}',
            contact_field_mapping TEXT,
            overwrite_existing   INTEGER NOT NULL DEFAULT 0,
            contact_owner_id     TEXT,
            enabled              INTEGER NOT NULL DEFAULT 1,
            updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, platform),
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS crm_entity_map (
            workspace_id         TEXT NOT NULL,
            lead_id              INTEGER NOT NULL,
            platform             TEXT NOT NULL,
            crm_contact_id       TEXT,
            crm_deal_id          TEXT,
            crm_company_id       TEXT,
            crm_owner_id         TEXT,
            last_synced_at       TEXT,
            last_event_id_synced TEXT,
            last_sync_status     TEXT NOT NULL DEFAULT 'pending',
            sync_error           TEXT,
            sync_hash            TEXT,
            created_at           TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, lead_id, platform),
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        );
        CREATE TRIGGER IF NOT EXISTS trg_crm_entity_map_bump_workspace_leads_insert
        AFTER INSERT ON crm_entity_map
        BEGIN
            UPDATE workspace_leads
            SET updated_at = datetime('now')
            WHERE lead_id = NEW.lead_id AND workspace_id = NEW.workspace_id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_crm_entity_map_bump_workspace_leads_update
        AFTER UPDATE ON crm_entity_map
        BEGIN
            UPDATE workspace_leads
            SET updated_at = datetime('now')
            WHERE lead_id = NEW.lead_id AND workspace_id = NEW.workspace_id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_crm_entity_map_bump_leads_insert
        AFTER INSERT ON crm_entity_map
        BEGIN
            UPDATE leads
            SET updated_at = datetime('now')
            WHERE id = NEW.lead_id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_crm_entity_map_bump_leads_update
        AFTER UPDATE ON crm_entity_map
        BEGIN
            UPDATE leads
            SET updated_at = datetime('now')
            WHERE id = NEW.lead_id;
        END;
        CREATE TABLE IF NOT EXISTS crm_sync_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id         TEXT NOT NULL,
            platform             TEXT NOT NULL,
            started_at           TEXT NOT NULL,
            completed_at         TEXT,
            leads_checked        INTEGER NOT NULL DEFAULT 0,
            contacts_created     INTEGER NOT NULL DEFAULT 0,
            contacts_updated     INTEGER NOT NULL DEFAULT 0,
            opportunities_created INTEGER NOT NULL DEFAULT 0,
            opportunities_updated INTEGER NOT NULL DEFAULT 0,
            events_pushed        INTEGER NOT NULL DEFAULT 0,
            skipped              INTEGER NOT NULL DEFAULT 0,
            errors               INTEGER NOT NULL DEFAULT 0,
            error_details        TEXT,
            status               TEXT NOT NULL DEFAULT 'in_progress',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_crm_sync_log_ws ON crm_sync_log(workspace_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS lead_emails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            email           TEXT NOT NULL,
            is_primary      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_lead_emails_lead ON lead_emails(lead_id);
        CREATE INDEX IF NOT EXISTS idx_lead_emails_email ON lead_emails(email);
        -- Phone numbers for leads AND companies in one polymorphic table.
        --
        -- Not personalization fields: personalization is a single value per
        -- (entity, name), so it cannot hold a mobile and a switchboard number
        -- at once, cannot normalize, and cannot dedup. Worse, it is a user
        -- namespace -- a client whose CSV has its own `phone` column would
        -- collide with the field CRM sync maps, which is exactly the failure
        -- mode `record_type` was made native to avoid.
        --
        -- owner_type is TEXT rather than two nullable FK columns so the same
        -- add/promote/remove verbs serve both entities. The cost is no FK, so
        -- deletes are swept by the triggers below rather than ON DELETE CASCADE.
        CREATE TABLE IF NOT EXISTS phone_numbers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_type  TEXT NOT NULL,           -- 'lead' | 'company'
            owner_id    INTEGER NOT NULL,
            phone_e164  TEXT NOT NULL,           -- normalized; the dedup key
            phone_raw   TEXT,                    -- exactly as sourced
            label       TEXT NOT NULL DEFAULT 'other',
            source      TEXT,                    -- provider slug
            is_primary  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (owner_type, owner_id, phone_e164)
        );
        CREATE INDEX IF NOT EXISTS idx_phone_numbers_owner ON phone_numbers(owner_type, owner_id);
        CREATE INDEX IF NOT EXISTS idx_phone_numbers_e164 ON phone_numbers(phone_e164);
        CREATE TRIGGER IF NOT EXISTS trg_phone_numbers_lead_delete
        AFTER DELETE ON leads BEGIN
            DELETE FROM phone_numbers WHERE owner_type = 'lead' AND owner_id = OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_phone_numbers_company_delete
        AFTER DELETE ON companies BEGIN
            DELETE FROM phone_numbers WHERE owner_type = 'company' AND owner_id = OLD.id;
        END;
        CREATE TABLE IF NOT EXISTS provider_batch_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            provider        TEXT NOT NULL,
            kind            TEXT NOT NULL,
            job_id          TEXT NOT NULL,
            workspace_id    TEXT,
            item_count      INTEGER NOT NULL DEFAULT 0,
            item_set_hash   TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'submitted',
            submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at    TEXT,
            metadata_json   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_provider_batch_jobs_hash ON provider_batch_jobs(provider, item_set_hash);
        CREATE INDEX IF NOT EXISTS idx_provider_batch_jobs_job_id ON provider_batch_jobs(provider, job_id);
        -- lead_provider_attempts used to be created here. Stage 7 folded it into
        -- lead_provider_observations and retired this name to a read-only VIEW
        -- (see _migrate_provider_observations below) -- creating it as a table
        -- here would collide with that VIEW on every DB that has migrated.
        CREATE TABLE IF NOT EXISTS sender_accounts (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id                  TEXT NOT NULL DEFAULT 'default',
            email                   TEXT NOT NULL,
            channel                 TEXT NOT NULL DEFAULT 'email',
            external_id             TEXT,
            provider                TEXT,
            first_name              TEXT,
            last_name               TEXT,
            daily_limit             INTEGER,
            warmup_status           TEXT,
            warmup_enabled_date     TEXT,
            status                  TEXT,
            spf_status              TEXT,
            dkim_status             TEXT,
            dmarc_status            TEXT,
            warmup_max_daily_limit  INTEGER,
            overall_health_score    INTEGER,
            google_health_score     INTEGER,
            microsoft_health_score  INTEGER,
            other_health_score      INTEGER,
            ooo_rr                  REAL,
            ooo_rr_14               REAL,
            ooo_rr_30               REAL,
            ooo_rr_90               REAL,
            bounce_rate             REAL,
            miss_warmup_rate        REAL,
            tags_json               TEXT DEFAULT '[]',
            source_created_at       TEXT,
            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, email)
        );
        CREATE INDEX IF NOT EXISTS idx_sender_accounts_updated ON sender_accounts(updated_at);
        CREATE INDEX IF NOT EXISTS idx_sender_accounts_org_email ON sender_accounts(org_id, email);
        CREATE TABLE IF NOT EXISTS workspace_sender_accounts (
            workspace_id        TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            sender_account_id   INTEGER NOT NULL REFERENCES sender_accounts(id) ON DELETE CASCADE,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (workspace_id, sender_account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_wsa_sender ON workspace_sender_accounts(sender_account_id);
    """)
    _repair_lead_provider_attempts_schema(conn)
    try:
        conn.execute("ALTER TABLE crm_entity_map ADD COLUMN crm_note_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE crm_workspace_config ADD COLUMN contact_owner_id TEXT")
    except sqlite3.OperationalError:
        pass
    for _col, _type in (
        ("linkedin_url", "TEXT"),
        ("linkedin_sales_nav_id", "TEXT"),
        ("email_domain", "TEXT"),
        # Last time this sender sent, and last time anything came back to it.
        # Maintained by log_event; see touch_sender_account_activity.
        ("last_outbound_at", "TEXT"),
        ("last_inbound_at", "TEXT"),
        # is_active: 0 == decommissioned mailbox. Provider exports only carry
        # live accounts, so a dropped mailbox is never told to go away -- this
        # lets cost/count/DNSBL queries exclude it while sync still ships the
        # 0 so the cloud can tell "decommissioned" from "never registered".
        # Default 1 is correct for every currently-live row (no backfill).
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ):
        try:
            conn.execute(f"ALTER TABLE sender_accounts ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass
    # Domain-level activity is a rollup over these, not a second copy of them --
    # see sender_domain_activity(). idx_sender_accounts_email_domain (below) makes
    # the GROUP BY cheap, so sender_domains stays free of duplicated state.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_sender_accounts_activity
           ON sender_accounts(last_outbound_at, last_inbound_at)"""
    )
    _backfill_sender_account_activity(conn)
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_sender_accounts_linkedin_url_unique
           ON sender_accounts(org_id, linkedin_url) WHERE linkedin_url IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_sender_accounts_sales_nav_id_unique
           ON sender_accounts(org_id, linkedin_sales_nav_id) WHERE linkedin_sales_nav_id IS NOT NULL"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sender_accounts_email_domain ON sender_accounts(email_domain)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sender_domains (
            domain          TEXT PRIMARY KEY,
            reseller        TEXT,
            domain_cost     REAL,
            currency        TEXT NOT NULL DEFAULT 'USD',
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    try:
        conn.execute("ALTER TABLE sender_domains ADD COLUMN notes TEXT")
    except sqlite3.OperationalError:
        pass
    # sending_ip: user-registered static IP (optional, enables IP-based DNSBL checks).
    # dnsbl_status: dedicated JSON column for blacklist scan results -- never crammed
    # into notes, which is a blind-overwrite freeform field on both manual set and cloud sync.
    for col in ("sending_ip", "dnsbl_status"):
        try:
            conn.execute(f"ALTER TABLE sender_domains ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    # is_active: 0 == domain intentionally dropped from the provider. Lets
    # DNSBL scanning and routing skip it and distinguishes it from a domain
    # simply not registered yet. Default 1 (no backfill needed).
    try:
        conn.execute(
            "ALTER TABLE sender_domains ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    # purpose: what the domain is used for (sending / branch / email_finding) so
    # the company panel can label and group additional domains. company_id: the
    # optional owning company, letting one company carry multiple domains.
    try:
        conn.execute(
            "ALTER TABLE sender_domains ADD COLUMN purpose TEXT NOT NULL DEFAULT 'sending'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE sender_domains ADD COLUMN company_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sender_domains_company "
            "ON sender_domains(company_id)")
    except sqlite3.OperationalError:
        pass

    # Self-heal pre-existing lead_emails duplicates (case/whitespace variants
    # of a lead's own primary email, or repeated inserts of the same email)
    # before adding the uniqueness constraint below — CREATE UNIQUE INDEX
    # fails if duplicates already exist.
    conn.execute("""
        DELETE FROM lead_emails
        WHERE is_primary = 0
          AND lower(trim(email)) = (
              SELECT lower(trim(email)) FROM leads WHERE leads.id = lead_emails.lead_id
          )
    """)
    conn.execute("""
        DELETE FROM lead_emails
        WHERE id NOT IN (
            SELECT MIN(id) FROM lead_emails GROUP BY lead_id, lower(trim(email))
        )
    """)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lead_emails_unique "
            "ON lead_emails(lead_id, email COLLATE NOCASE)"
        )
    except sqlite3.OperationalError:
        pass

    # Wire audit trail: every payload that crosses the relay boundary, logged
    # before the HTTP call. See sync_audit.py.
    from sync_audit import SCHEMA_SQL as SYNC_AUDIT_SCHEMA, DEFAULT_RETENTION_DAYS
    conn.executescript(SYNC_AUDIT_SCHEMA)
    conn.execute(
        "DELETE FROM sync_audit WHERE created_at < datetime('now', ?)",
        (f"-{DEFAULT_RETENTION_DAYS} days",),
    )

    # --- Immutable surrogate identity (uid) ---------------------------------
    #
    # The relay entity_key is derived from mutable columns today (email wins over
    # linkedin_url), so finding a lead's email MOVES its wire identity: the old
    # snapshot orphans on the relay and a fresh one is created under the new key,
    # stranding all the workspace state filed under the old one. 52,693 leads are
    # currently one email-find away from exactly that.
    #
    # Worse, a lead with neither email nor linkedin gets an EMPTY key and is
    # skipped by the push loop entirely -- 2,830 real leads (plus junk) have never
    # reached the relay at all.
    #
    # A uid fixes both by construction: every row has one, it is generated once,
    # and it never changes. Natural keys (email, linkedin, sales-nav id) become
    # aliases instead of identities.
    for table in ("leads", "companies"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        except sqlite3.OperationalError:
            pass
        # randomblob(16) per row; SQLite evaluates it once per row in an UPDATE.
        conn.execute(
            f"UPDATE {table} SET uid = lower(hex(randomblob(16))) "
            f"WHERE uid IS NULL OR uid = ''"
        )
        try:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uid ON {table}(uid)"
            )
        except sqlite3.OperationalError:
            pass
        # Stamp new rows from the database, not from application code: SQLite
        # cannot ALTER ADD COLUMN with a non-constant DEFAULT, and a trigger means
        # no INSERT path can forget (there are several, across import, enrich,
        # webhook ingest and snapshot apply).
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_uid
            AFTER INSERT ON {table}
            WHEN NEW.uid IS NULL OR NEW.uid = ''
            BEGIN
                UPDATE {table} SET uid = lower(hex(randomblob(16))) WHERE id = NEW.id;
            END
        """)

    # Must run before _migrate_provider_observations: its ALTER TABLE RENAME
    # is the first DDL against this schema that forces SQLite to recompile
    # every trigger body, which is exactly when a stale one (see docstring)
    # blows up.
    _drop_stale_outbox_triggers(conn)

    # Stage 7: must run after the uid backfill above (obs_uid keys on lead_uid)
    # and before ensure_outbox below (the new table needs to exist first so it
    # gets an outbox trigger from the same migration that creates it).
    _migrate_provider_observations(conn)

    # Collapses per-domain domain_found_<domain> tags onto one flag. Runs
    # before ensure_outbox so the rewritten rows enqueue under the new trigger.
    _collapse_domain_found_tags(conn)

    # Must run after the collapse above -- it retags a subset of exactly what
    # that migration just produced -- and before ensure_outbox for the same
    # reason: the rewritten rows need to enqueue under the new trigger.
    _reconcile_stale_domain_discovered_tags(conn)

    # Must run after the uid columns above: the tombstone triggers read OLD.uid.
    ensure_outbox(conn)

    # leads.stage becomes a cache of workspace_leads.status; see the docstring.
    ensure_derived_lead_stage(conn)
    backfill_derived_lead_stage(conn)

    # linkedin_bio is already a leads column. A personalization row holding it is
    # a second copy that drifts from the first, and personalization is meant for
    # human-authored render values (a mailmerge-ready first_name is a genuinely
    # different fact from leads.name -- those stay). Fold any bio we hold only in
    # personalization back onto the lead, then drop the rows.
    conn.execute("""
        UPDATE leads SET linkedin_bio = (
            SELECT p.field_value FROM lead_personalization p
            WHERE p.lead_id = leads.id AND p.field_name = 'linkedin_bio'
              AND p.field_value IS NOT NULL AND TRIM(p.field_value) != ''
        )
        WHERE (linkedin_bio IS NULL OR TRIM(linkedin_bio) = '')
          AND EXISTS (
            SELECT 1 FROM lead_personalization p
            WHERE p.lead_id = leads.id AND p.field_name = 'linkedin_bio'
              AND p.field_value IS NOT NULL AND TRIM(p.field_value) != ''
          )
    """)
    conn.execute("DELETE FROM lead_personalization WHERE field_name = 'linkedin_bio'")

    _repair_sales_nav_id_casing(conn)
    _repair_keyless_workspace_tombstones(conn)
    _fold_shadow_source_personalization(conn)
    _add_lead_record_type(conn)
    _repair_provenance_transport_strings(conn)
    _install_provenance_transport_guard(conn)

    # Clear the machine-written notes relay_ingest used to stamp on every lead.
    # Deliberately an exact-shape match anchored at both ends, not a LIKE '%via
    # relay%': notes is a human field and a person's note that happens to mention
    # an import must survive this.
    conn.execute("""
        UPDATE leads SET notes = NULL
        WHERE notes IS NOT NULL
          AND notes GLOB 'Auto-imported from * via relay'
          AND notes NOT GLOB '*[' || char(10) || char(13) || ']*'
    """)

    # Stage D7: human-curated branch/department label on a company's known
    # domain (e.g. "College of Engineering" on coe.northeastern.edu) --
    # nullable, purely descriptive, never used by matching/ranking/confidence
    # logic (same discipline as `role`).
    try:
        conn.execute("ALTER TABLE company_identities ADD COLUMN label TEXT")
    except sqlite3.OperationalError:
        pass

    _add_company_identity_purpose(conn)

    conn.commit()
    if own_conn:
        conn.close()


def mark_all_lead_snapshots_pending(
    conn: Optional[sqlite3.Connection] = None,
    *,
    workspace_id: Optional[str] = None,
) -> None:
    """Queue full snapshot backfill (core + workspace membership).

    Scoped to one workspace's leads when workspace_id is given; otherwise
    every lead and membership in the account is marked pending.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    if workspace_id:
        conn.execute(
            """UPDATE leads SET updated_at = datetime('now')
               WHERE id IN (SELECT lead_id FROM workspace_leads WHERE workspace_id = ?)""",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE workspace_leads SET updated_at = datetime('now') WHERE workspace_id = ?",
            (workspace_id,),
        )
    else:
        conn.execute("UPDATE leads SET updated_at = datetime('now')")
        conn.execute("UPDATE workspace_leads SET updated_at = datetime('now')")
    if own_conn:
        conn.commit()
        conn.close()


def mark_all_entities_pending(conn: Optional[sqlite3.Connection] = None) -> None:
    """Queue a full account-wide resync of every synced entity type.

    Companies, sender accounts, and sender domains aren't workspace-scoped
    (unlike leads), so there's no per-workspace variant here -- this always
    touches the whole account. Bumping updated_at fires the same outbox
    triggers Stage 5 installed for every table in sync_contract.SYNC_MAP, so
    this is just "touch every row" -- the triggers do the actual dirtying.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    mark_all_lead_snapshots_pending(conn)
    conn.execute("UPDATE companies SET updated_at = datetime('now')")
    conn.execute("UPDATE sender_accounts SET updated_at = datetime('now')")
    conn.execute("UPDATE sender_domains SET updated_at = datetime('now')")
    if own_conn:
        conn.commit()
        conn.close()


def repair_malformed_tags(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Fix workspace tags stored as list literals (e.g. \"['nace']\" -> \"nace\")."""
    from pipeline_utils import normalize_tag, parse_tags_value

    rows = conn.execute(
        "SELECT id, workspace_id, lead_id, tag FROM workspace_lead_tags ORDER BY id"
    ).fetchall()
    fixed_rows = 0
    removed_rows = 0
    inserted_tags = 0
    examples: list[dict] = []

    for row in rows:
        raw_tag = row["tag"] or ""
        parsed = parse_tags_value(raw_tag)
        if len(parsed) == 1 and parsed[0] == normalize_tag(raw_tag):
            continue
        if not parsed:
            removed_rows += 1
            if len(examples) < 5:
                examples.append({"from": raw_tag, "to": []})
            if not dry_run:
                conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["id"],))
            continue

        fixed_rows += 1
        if len(examples) < 5:
            examples.append({"from": raw_tag, "to": parsed})
        if dry_run:
            inserted_tags += len(parsed)
            continue

        conn.execute("DELETE FROM workspace_lead_tags WHERE id = ?", (row["id"],))
        for tag in parsed:
            tag_id = (
                f"wlt_{row['workspace_id']}_{row['lead_id']}_"
                f"{hashlib.md5(tag.encode()).hexdigest()[:8]}"
            )
            cur = conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (tag_id, row["workspace_id"], row["lead_id"], tag),
            )
            inserted_tags += cur.rowcount

    return {
        "status": "ok",
        "dry_run": dry_run,
        "rows_fixed": fixed_rows,
        "rows_removed": removed_rows,
        "tags_inserted": inserted_tags,
        "examples": examples,
    }


def backfill_workspace_routing(conn: sqlite3.Connection):
    """Identity aliases for all leads; workspace_leads/maps only in single-workspace mode."""
    ensure_organization(conn)
    config = get_org_routing_config(conn, DEFAULT_ORG_ID)

    leads = conn.execute(
        "SELECT id, email, linkedin_url FROM leads"
    ).fetchall()
    for lead in leads:
        lid = lead["id"]
        if lead["email"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, 'email', ?, 'backfill', 1, datetime('now')
                   )""",
                (DEFAULT_ORG_ID, lid, lead["email"]),
            )
        if lead["linkedin_url"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, 'linkedin_url', ?, 'backfill', 1, datetime('now')
                   )""",
                (DEFAULT_ORG_ID, lid, lead["linkedin_url"]),
            )

    if config.mode == WORKSPACE_ROUTING_MULTI:
        return

    workspace_id = config.default_workspace_id or ensure_default_org_workspace(conn)
    for lead in leads:
        lid = lead["id"]
        stage_row = conn.execute("SELECT stage FROM leads WHERE id = ?", (lid,)).fetchone()
        status = stage_row["stage"] if stage_row else "prospecting"
        upsert_workspace_lead(conn, DEFAULT_ORG_ID, workspace_id, lid, status=status)

    campaigns = conn.execute("SELECT name FROM campaigns").fetchall()
    for row in campaigns:
        name = (row["name"] or "").strip()
        if not name:
            continue
        # One-shot per campaign name: skip if any row (active or inactive)
        # already exists for this key, so a re-run never resurrects a row that
        # was intentionally deactivated (assign_campaign_map forces is_active=1).
        name_normalized = normalize_campaign_name(name)
        existing = conn.execute(
            """SELECT 1 FROM campaign_workspace_map
               WHERE org_id = ? AND source_platform = '*'
                 AND match_strategy = 'name_exact'
                 AND campaign_name_normalized = ?""",
            (DEFAULT_ORG_ID, name_normalized),
        ).fetchone()
        if existing:
            continue
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id=workspace_id,
            campaign_name=name,
            match_strategy="name_exact",
            map_source="single_mode_backfill",
        )
