"""SQLite schema for fresh database init."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    domain          TEXT,
    industry        TEXT,
    headcount       TEXT,
    headcount_numeric   INTEGER,
    hq_city             TEXT,
    hq_state            TEXT,
    hq_country          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    name                     TEXT NOT NULL,
    company_id               INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    company                  TEXT,
    title                    TEXT,
    industry                 TEXT,
    headcount                TEXT,
    headcount_numeric        INTEGER,
    email                    TEXT,
    email_domain             TEXT,
    linkedin_url             TEXT,
    linkedin_sales_nav_id    TEXT,
    location_city            TEXT,
    location_state           TEXT,
    location_country         TEXT,
    channel                  TEXT NOT NULL DEFAULT 'email',
    stage                    TEXT NOT NULL DEFAULT 'prospecting',
    notes                    TEXT,
    original_source          TEXT,
    original_source_detail   TEXT,
    original_source_platform TEXT,
    original_source_at       TEXT,
    latest_source            TEXT,
    latest_source_detail     TEXT,
    latest_source_platform   TEXT,
    latest_source_at         TEXT,
    email_verification_status TEXT,
    email_verified_at         TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    last_contact_at          TEXT,
    next_action              TEXT,
    latest_sender            TEXT,
    latest_sender_platform   TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    workspace_id    TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    direction       TEXT NOT NULL DEFAULT 'outbound',
    channel         TEXT NOT NULL DEFAULT 'email',
    subject         TEXT,
    body_preview    TEXT,
    metadata_json   TEXT DEFAULT '{}',
    campaign_id     INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    sender          TEXT,
    relay_id        INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaign_leads (
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_updated ON leads(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_lead_created ON events(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);
-- One relay event, one row. relay_ingested is the primary dedupe, but it lives in
-- a separate table: lose or reset it while events survives and the next pull
-- re-ingests the lot. This is the guard that makes that impossible. Locally-logged
-- events have a NULL relay_id and are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_relay_unique ON events(relay_id) WHERE relay_id IS NOT NULL;
-- No plain index on leads(email): the partial unique index below already serves
-- every `WHERE email = ?` lookup (verified with EXPLAIN QUERY PLAN -- it's chosen
-- as a covering index), and a second copy of the same column is pure write cost.
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique ON leads(linkedin_url) WHERE linkedin_url IS NOT NULL;
-- idx_leads_sales_nav_id_unique intentionally NOT here: linkedin_sales_nav_id
-- is a new column added via migrate_db()'s ALTER TABLE, which runs AFTER this
-- script on existing databases. Indexing it here would run before the column
-- exists on any pre-existing leads table and crash init_db() for everyone
-- who isn't starting from a brand-new database. See pipeline_migration.py.
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
CREATE INDEX IF NOT EXISTS idx_leads_company_name ON leads(company);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL;

CREATE TABLE IF NOT EXISTS lead_merges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keep_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    merge_id        INTEGER NOT NULL,
    reason          TEXT,
    merge_entity_key TEXT,
    relay_delete_pushed INTEGER NOT NULL DEFAULT 0,
    merged_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Inbound dedupe ledger: "have we already ingested this relay event?"
--
-- Stores a 16-byte hash of the dedupe key, not the key itself. The keys are long
-- (the agent ones average 129 bytes: agent:{client_id}:{entity_key}:{action}:{ts})
-- and there are ~800k of them, so the raw text cost ~146 MB across the table and
-- its primary-key B-tree. They are only ever compared for exact equality, and are
-- always built in Python, so nothing needs the original string back --
-- relay_dedupe_hash() in relay_ingest.py is the one place that maps key -> hash.
--
-- Push markers deliberately do NOT live here; see event_push_log.
CREATE TABLE IF NOT EXISTS relay_ingested (
    dedupe_hash     BLOB PRIMARY KEY,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_relay_ingested_lead_id ON relay_ingested(lead_id);

-- Outbound push state: "has this event been pushed to the relay?"
--
-- This used to be an 'event:{id}' text key inside relay_ingested, which meant the
-- push query had to build the key in SQL ('event:' || e.id) and probe an 800k-row
-- text B-tree. It is push state, not dedupe state, and an integer FK says so.
CREATE TABLE IF NOT EXISTS event_push_log (
    event_id        INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    pushed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Org + workspace routing (org-wide lead, workspace-scoped status/events)
CREATE TABLE IF NOT EXISTS organizations (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    workspace_routing_mode  TEXT NOT NULL DEFAULT 'single',
    default_workspace_id    TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    cloud_synced    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, slug)
);

-- `id` is a surrogate nothing references -- every read is by
-- (org_id, identity_type, identity_value_normalized) or by lead_id. It used to be
-- a random 32-char hex string, which bought nothing and cost ~26 MB across the row
-- data and its primary-key index on 343k rows.
CREATE TABLE IF NOT EXISTS lead_identities (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id                  TEXT NOT NULL,
    lead_id                 INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    identity_type           TEXT NOT NULL,
    identity_value_normalized TEXT NOT NULL,
    source                  TEXT,
    is_verified             INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, identity_type, identity_value_normalized)
);

CREATE INDEX IF NOT EXISTS idx_lead_identities_lead ON lead_identities(org_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_identities_lead_type ON lead_identities(lead_id, identity_type);
CREATE INDEX IF NOT EXISTS idx_lead_identities_type ON lead_identities(identity_type, lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_identities_type_value_lower ON lead_identities(org_id, identity_type, LOWER(identity_value_normalized));

CREATE TABLE IF NOT EXISTS workspace_leads (
    id                       TEXT PRIMARY KEY,
    org_id                   TEXT NOT NULL,
    workspace_id             TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id                  INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status                   TEXT NOT NULL DEFAULT 'prospecting',
    owner_user_id            TEXT,
    stage_entered_at         TEXT,
    last_activity_at         TEXT,
    current_status_label     TEXT,
    current_status_sentiment TEXT,
    contact_priority         INTEGER,
    latest_sender            TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_leads_status ON workspace_leads(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_workspace_leads_owner ON workspace_leads(workspace_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_workspace_leads_activity ON workspace_leads(workspace_id, last_activity_at);
-- All four indexes above lead with workspace_id; every lead_id-only lookup
-- (relay ingest's stage-downgrade guard, merge_leads, activity_sync, etc.)
-- had no usable index and fell back to a full table scan of workspace_leads.
CREATE INDEX IF NOT EXISTS idx_workspace_leads_lead_id ON workspace_leads(lead_id);

-- The workspace-scoped index over `events`: inbound dedupe, plus the CRM
-- "has this lead been active lately" filter and its push cursor.
--
-- Deliberately NOT a content store. This table used to carry a payload_json copy
-- of events.metadata_json (`{"event": <metadata>}`) -- the same blob, body and
-- all -- which nothing read and which cost 91 MB on a 783 MB database. It now
-- carries an 8-byte event_id instead, so anything that wants content joins
-- `events`, where it lives exactly once.
CREATE TABLE IF NOT EXISTS workspace_lead_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id              TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    event_id            INTEGER REFERENCES events(id) ON DELETE CASCADE,
    event_type          TEXT NOT NULL,
    event_at            TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ws_events_lead ON workspace_lead_events(workspace_id, lead_id, event_at);
CREATE INDEX IF NOT EXISTS idx_ws_events_type ON workspace_lead_events(workspace_id, event_type, event_at);

CREATE TABLE IF NOT EXISTS campaign_workspace_map (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_platform_id    TEXT,
    campaign_name_normalized  TEXT,
    workspace_id            TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    match_strategy          TEXT NOT NULL DEFAULT 'id_exact',
    priority                INTEGER NOT NULL DEFAULT 100,
    is_active               INTEGER NOT NULL DEFAULT 1,
    cloud_synced            INTEGER NOT NULL DEFAULT 0,
    map_source              TEXT NOT NULL DEFAULT 'manual',
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_campaign_map_lookup ON campaign_workspace_map(
    org_id, source_platform, is_active, priority
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_id ON campaign_workspace_map(
    org_id, source_platform, campaign_platform_id
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_name ON campaign_workspace_map(
    org_id, source_platform, campaign_name_normalized
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_map_id_active ON campaign_workspace_map(
    org_id, source_platform, campaign_platform_id
) WHERE campaign_platform_id IS NOT NULL AND is_active = 1;

CREATE TABLE IF NOT EXISTS unmapped_campaign_queue (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_platform_id    TEXT,
    campaign_name_raw       TEXT,
    campaign_name_normalized TEXT,
    external_event_id       TEXT,
    reason                  TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending',
    payload_json            TEXT NOT NULL,
    received_at             TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at             TEXT,
    assigned_workspace      TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_status ON unmapped_campaign_queue(org_id, status, received_at);
CREATE INDEX IF NOT EXISTS idx_quarantine_campaign ON unmapped_campaign_queue(
    org_id, source_platform, campaign_platform_id, status
);

CREATE TABLE IF NOT EXISTS lead_merge_jobs (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    keep_lead_id    INTEGER NOT NULL,
    merge_lead_id   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed',
    reason          TEXT,
    audit_json      TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lead_personalization (
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    field_value     TEXT NOT NULL,
    field_date      TEXT,
    source_hash     TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lead_id, field_name)
);

CREATE TABLE IF NOT EXISTS company_personalization (
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    field_value     TEXT NOT NULL,
    field_date      TEXT,
    source_hash     TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (company_id, field_name)
);

CREATE TABLE IF NOT EXISTS workspace_lead_tags (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    tag             TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_wlt_workspace_tag ON workspace_lead_tags(workspace_id, tag);
CREATE INDEX IF NOT EXISTS idx_wlt_lead ON workspace_lead_tags(lead_id);
CREATE INDEX IF NOT EXISTS idx_wlt_tag_ws_lead ON workspace_lead_tags(tag, workspace_id, lead_id);

CREATE TABLE IF NOT EXISTS workspace_lead_linkedin_status (
    id                 TEXT PRIMARY KEY,
    workspace_id       TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    lead_id            INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    sender_profile     TEXT NOT NULL,
    is_connected       INTEGER NOT NULL DEFAULT 0,
    is_request_pending INTEGER NOT NULL DEFAULT 0,
    connected_at       TEXT,
    request_sent_at    TEXT,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace_id, lead_id, sender_profile)
);

CREATE INDEX IF NOT EXISTS idx_li_status_workspace ON workspace_lead_linkedin_status(workspace_id, sender_profile);
CREATE INDEX IF NOT EXISTS idx_li_status_lead ON workspace_lead_linkedin_status(lead_id);

-- lead_email_verification used to be created here. Stage 7 folded it into
-- lead_provider_observations (provider_observations.TABLE_SQL, created in
-- pipeline_migration._migrate_provider_observations) and retired this name to
-- a read-only VIEW of the same shape -- creating it as a table here would
-- collide with that VIEW ("views may not be indexed") on every DB that has
-- already migrated.

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
"""
