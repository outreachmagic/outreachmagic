"""
Database initialization, schema migration, and backfill functions.

Extracted from pipeline.py's "Database Operations" section.
"""

from __future__ import annotations

import hashlib
import sqlite3
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
    return out


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
    ("lead_core", "SELECT CAST(id AS TEXT) FROM leads"),
    (
        "lead_workspace",
        "SELECT CAST(lead_id AS TEXT) || ':' || workspace_id FROM workspace_leads",
    ),
    ("company", "SELECT CAST(id AS TEXT) FROM companies"),
    ("sender_account", "SELECT CAST(id AS TEXT) FROM sender_accounts"),
    ("sender_domain", "SELECT domain FROM sender_domains"),
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
            conn.execute(
                f"""INSERT INTO outbox (entity_type, entity_id, op, dirty_at)
                    SELECT ?, entity_id, 'upsert', datetime('now') FROM ({select_sql}) AS s(entity_id)
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
    """
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
        CREATE TABLE IF NOT EXISTS lead_email_verification (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            status TEXT NOT NULL,
            sub_status TEXT,
            source TEXT NOT NULL,
            source_detail TEXT,
            bounce_message TEXT,
            free_email INTEGER,
            mx_found INTEGER,
            smtp_provider TEXT,
            verified_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (org_id, lead_id, source)
        );
        CREATE INDEX IF NOT EXISTS idx_verification_email ON lead_email_verification(email);
        CREATE INDEX IF NOT EXISTS idx_verification_status ON lead_email_verification(org_id, status);
        CREATE INDEX IF NOT EXISTS idx_verification_lead ON lead_email_verification(lead_id);
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
    ]:
        try:
            conn.execute(f"ALTER TABLE workspace_leads ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
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
        CREATE TABLE IF NOT EXISTS lead_provider_attempts (
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
        );
        CREATE INDEX IF NOT EXISTS idx_lpa_lookup ON lead_provider_attempts(lead_id, provider, status);
        CREATE INDEX IF NOT EXISTS idx_lpa_provider ON lead_provider_attempts(provider, status, attempted_at);
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

    # Must run after the uid columns above: the tombstone triggers read OLD.uid.
    ensure_outbox(conn)

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
