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
    upsert_workspace_lead,
)


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
            id TEXT PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS workspace_lead_events (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lead_id INTEGER NOT NULL,
            workspace_lead_id TEXT,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            external_event_id TEXT,
            idempotency_key TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
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
    """)
    try:
        conn.execute("ALTER TABLE crm_entity_map ADD COLUMN crm_note_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE crm_workspace_config ADD COLUMN contact_owner_id TEXT")
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
                       id, org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, ?, 'email', ?, 'backfill', 1, datetime('now')
                   )""",
                (f"id_email_{lid}", DEFAULT_ORG_ID, lid, lead["email"]),
            )
        if lead["linkedin_url"]:
            conn.execute(
                """INSERT OR IGNORE INTO lead_identities (
                       id, org_id, lead_id, identity_type, identity_value_normalized,
                       source, is_verified, created_at
                   ) VALUES (
                       ?, ?, ?, 'linkedin_url', ?, 'backfill', 1, datetime('now')
                   )""",
                (f"id_li_{lid}", DEFAULT_ORG_ID, lid, lead["linkedin_url"]),
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
        assign_campaign_map(
            conn,
            DEFAULT_ORG_ID,
            source_platform="*",
            workspace_id=workspace_id,
            campaign_name=name,
            match_strategy="name_exact",
        )
