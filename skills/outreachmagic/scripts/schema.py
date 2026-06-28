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
    next_action_at           TEXT,
    latest_sender            TEXT,
    latest_sender_platform   TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
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
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_unique ON leads(linkedin_url) WHERE linkedin_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
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

CREATE TABLE IF NOT EXISTS relay_ingested (
    dedupe_key      TEXT PRIMARY KEY,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS lead_identities (
    id                      TEXT PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS workspace_lead_events (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    lead_id             INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    workspace_lead_id   TEXT REFERENCES workspace_leads(id) ON DELETE SET NULL,
    event_type          TEXT NOT NULL,
    event_at            TEXT NOT NULL,
    source_platform     TEXT NOT NULL,
    external_event_id   TEXT,
    idempotency_key     TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ws_events_lead ON workspace_lead_events(workspace_id, lead_id, event_at);
CREATE INDEX IF NOT EXISTS idx_ws_events_type ON workspace_lead_events(workspace_id, event_type, event_at);

CREATE TABLE IF NOT EXISTS campaign_workspace_map (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_id             TEXT,
    campaign_name_normalized  TEXT,
    workspace_id            TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    match_strategy          TEXT NOT NULL DEFAULT 'id_exact',
    priority                INTEGER NOT NULL DEFAULT 100,
    is_active               INTEGER NOT NULL DEFAULT 1,
    cloud_synced            INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_campaign_map_lookup ON campaign_workspace_map(
    org_id, source_platform, is_active, priority
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_id ON campaign_workspace_map(
    org_id, source_platform, campaign_id
);
CREATE INDEX IF NOT EXISTS idx_campaign_map_name ON campaign_workspace_map(
    org_id, source_platform, campaign_name_normalized
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_map_id_active ON campaign_workspace_map(
    org_id, source_platform, campaign_id
) WHERE campaign_id IS NOT NULL AND is_active = 1;

CREATE TABLE IF NOT EXISTS unmapped_campaign_queue (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    source_platform         TEXT NOT NULL,
    campaign_id             TEXT,
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
    org_id, source_platform, campaign_id, status
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

CREATE TABLE IF NOT EXISTS lead_email_verification (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    status          TEXT NOT NULL,
    sub_status      TEXT,
    source          TEXT NOT NULL,
    source_detail   TEXT,
    bounce_message  TEXT,
    free_email      INTEGER,
    mx_found        INTEGER,
    smtp_provider   TEXT,
    verified_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
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
"""
