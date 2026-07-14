"""Declarative map of which local tables feed which relay entity.

One source of truth, consumed by three things:
  * the outbox triggers (pipeline_migration.build_outbox_triggers)
  * the push loop, to resolve an outbox row back to an entity
  * Stage 6's coverage test, which asserts every column of every synced table
    is classified

Adding a synced table is one line here, not three hand-written triggers.

Why an outbox at all: dirtiness used to be *derived* at push time from
`leads.updated_at` (40.7% of which is older than `created_at`, so the cursor
lies), and the derivation missed writes that never touched the parent row --
`record_provider_attempt()` bumps no parent timestamp, so a provider attempt
never marked its lead dirty. Recording dirtiness at write time, in the
database, makes those functions correct without changing them: the child-table
write itself fires the trigger.
"""

# entity_type -> the relay snapshot kind it maps to. These are the kinds the
# relay's pull/push endpoints already speak.
ENTITY_TYPES = (
    "lead_core",
    "lead_workspace",
    "company",
    "sender_account",
    "sender_domain",
)

# The table that *owns* each entity. A DELETE here means the entity itself is
# gone (tombstone); a DELETE on any other mapped table is just a content change
# to a still-living parent (upsert).
OWNER_TABLES = {
    "leads": "lead_core",
    "workspace_leads": "lead_workspace",
    "companies": "company",
    "sender_accounts": "sender_account",
    "sender_domains": "sender_domain",
}

# table -> (entity_type, entity_id expression). `{row}` is substituted with NEW
# or OLD depending on the trigger. entity_id is TEXT: lead_workspace needs a
# composite, and a single type keeps the outbox PK simple.
#
# lead_email_verification and lead_provider_attempts are here even though the
# plan's target map names `lead_provider_observations` instead -- that table is
# Stage 7. Until it exists, these two are the tables whose writes must mark a
# lead dirty, and they are exactly the ones that were silently failing to.
# Stage 7 retargets these two entries at the observations log; nothing else moves.
SYNC_MAP = {
    # --- lead_core -------------------------------------------------------
    "leads":                         ("lead_core", "{row}.id"),
    "lead_identities":               ("lead_core", "{row}.lead_id"),
    "lead_personalization":          ("lead_core", "{row}.lead_id"),
    "lead_emails":                   ("lead_core", "{row}.lead_id"),
    "lead_email_verification":       ("lead_core", "{row}.lead_id"),
    "lead_provider_attempts":        ("lead_core", "{row}.lead_id"),
    # --- lead_workspace --------------------------------------------------
    "workspace_leads":               ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "workspace_lead_tags":           ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "workspace_lead_linkedin_status": ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "crm_entity_map":                ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    # --- company ---------------------------------------------------------
    "companies":                     ("company", "{row}.id"),
    "company_personalization":       ("company", "{row}.company_id"),
    # --- sender ----------------------------------------------------------
    # sender_domains is keyed by the domain itself, not a surrogate id.
    "sender_accounts":               ("sender_account", "{row}.id"),
    "sender_domains":                ("sender_domain", "{row}.domain"),
}


def entity_id_expr(table: str, row: str) -> str:
    """The SQL expression yielding an entity_id for `table`, bound to NEW/OLD."""
    return SYNC_MAP[table][1].format(row=row)


def entity_type_for(table: str) -> str:
    return SYNC_MAP[table][0]


def tables_for(entity_type: str) -> list[str]:
    return [t for t, (et, _) in SYNC_MAP.items() if et == entity_type]


# --- Stage 6: column-level coverage --------------------------------------
#
# Every column of every table in SYNC_MAP must appear in exactly one of
# SYNCED_COLUMNS / NOT_SYNCED_COLUMNS below. tests/test_sync_contract.py
# enforces `PRAGMA table_info(table) == SYNCED_COLUMNS[table] | NOT_SYNCED_COLUMNS[table].keys()`
# for every table -- add a column anywhere in this list and forget to classify
# it here, and CI fails. That's the point: "a tag is updated but updated_at
# isn't" is a silent bug today; here it's a red test.
#
# SYNCED means: some payload builder actually reads this column and puts its
# value (or a value derived from it) on the wire for a synced row. NOT_SYNCED
# entries carry *why*, sourced from reading the real builders in lead_sync.py,
# pipeline_workspace.py, pipeline_personalize.py, pipeline_sender_accounts.py,
# activity_sync.py -- not guessed. `id`-shaped surrogate/join columns are
# NOT_SYNCED: the wire key is `uid` (leads/companies) or the SYNC_MAP entity_id
# expression (composite lead_id:workspace_id, etc.), never the local row id.

SYNCED_COLUMNS: dict[str, frozenset[str]] = {
    "leads": frozenset({
        "name", "title", "notes", "location_city", "location_state", "location_country",
        "email_verification_status", "linkedin_headline", "linkedin_bio",
        "linkedin_sales_nav_id", "email", "linkedin_url", "latest_sender",
        "latest_sender_platform", "email_verified_at", "original_source",
        "original_source_detail", "original_source_at", "latest_source",
        "latest_source_detail", "latest_source_at",
        "uid",  # transmitted as the entity_key (uid:<uid>), not a payload field
    }),
    "lead_identities": frozenset({"identity_type", "identity_value_normalized"}),
    "lead_personalization": frozenset({"field_name", "field_value", "field_date", "processed_at"}),
    "lead_emails": frozenset(),
    "lead_email_verification": frozenset(),
    "lead_provider_attempts": frozenset({
        "provider", "domain", "attempted_at", "completed_at", "status",
        "result_email", "result_validity",
    }),
    "workspace_leads": frozenset({
        "status", "current_status_label", "current_status_sentiment", "contact_priority",
        "email_sent_count", "linkedin_sent_count", "total_replies_count", "last_contacted_at",
    }),
    "workspace_lead_tags": frozenset({"tag"}),
    "workspace_lead_linkedin_status": frozenset({
        "sender_profile", "is_connected", "is_request_pending",
    }),
    "crm_entity_map": frozenset({
        "platform", "crm_contact_id", "crm_deal_id", "crm_company_id", "crm_owner_id",
        "last_synced_at", "last_event_id_synced", "last_sync_status", "sync_hash", "crm_note_id",
    }),
    "companies": frozenset({
        "name", "domain", "industry", "headcount",
        "uid",  # transmitted as the entity_key (uid:<uid>), not a payload field
    }),
    "company_personalization": frozenset({"field_name", "field_value", "field_date", "processed_at"}),
    "sender_accounts": frozenset({
        "email", "first_name", "last_name", "provider", "daily_limit", "warmup_status",
        "source_created_at", "status", "spf_status", "dkim_status", "dmarc_status",
        "warmup_enabled_date", "warmup_max_daily_limit", "overall_health_score",
        "google_health_score", "microsoft_health_score", "other_health_score",
        "ooo_rr", "ooo_rr_14", "ooo_rr_30", "ooo_rr_90", "bounce_rate", "miss_warmup_rate",
        "tags_json", "linkedin_url", "linkedin_sales_nav_id", "external_id",
    }),
    "sender_domains": frozenset({
        "domain",  # transmitted as the entity_key (sender_domain:<domain>)
        "reseller", "domain_cost", "currency", "notes", "sending_ip", "dnsbl_status",
    }),
}

NOT_SYNCED_COLUMNS: dict[str, dict[str, str]] = {
    "leads": {
        "id": "local autoincrement surrogate; `uid` is the wire key, not this row id",
        "company_id": "local FK; the company travels as its own uid-keyed snapshot",
        "company": "denormalized display fallback predating a company_id match; the company entity carries the canonical name",
        "industry": "legacy per-lead copy predating the company link; the company entity is authoritative",
        "headcount": "legacy per-lead copy predating the company link; the company entity is authoritative",
        "headcount_numeric": "derived numeric parse of headcount, recomputed locally rather than shipped",
        "email_domain": "derived from email; recomputed locally rather than shipped",
        "channel": "outreach channel default; not read by any sync payload builder today",
        "stage": "per-workspace fact (workspace_leads.status is authoritative); an org-wide stage is ill-defined by construction -- see SYNC_PROFILE_FIELDS comment in lead_sync.py",
        "original_source_platform": "Stage 8: ~85% of stored values were the transport string (e.g. 'relay'), not real provenance; dropped from the wire rather than propagate the lie",
        "latest_source_platform": "Stage 8, same reasoning as original_source_platform",
        "created_at": "audit/display column only",
        "updated_at": "audit/display column only -- 40.7% of rows have updated_at < created_at, so nothing may depend on it for sync semantics (Stage 5)",
        "last_contact_at": "not itself transmitted; written as a side effect when a workspace snapshot's activity block (last_contacted_at) is applied via apply_activity_sync_payload",
        "next_action": "local planning field; no sync payload builder reads it",
    },
    "lead_identities": {
        "id": "local autoincrement surrogate; not addressable from the relay side",
        "org_id": "implicit from the authenticated request, not carried per-row",
        "lead_id": "join key; covered by the parent lead_core entity_id",
        "source": "not selected by the lead_core payload query (lead_sync.py only reads identity_type/identity_value_normalized)",
        "is_verified": "not selected by the lead_core payload query",
        "created_at": "not selected by the lead_core payload query",
    },
    "lead_personalization": {
        "lead_id": "join key; covered by the parent lead_core entity_id",
        "source_hash": "local change-detection hash, not needed by the relay",
    },
    "lead_emails": {
        "id": "local surrogate",
        "lead_id": "join key",
        "email": "written only on *apply*, from payload['secondary_emails'] (itself derived from lead_identities) -- never read to build an outbound payload, so this table's own values never travel",
        "is_primary": "same as email -- local materialization only, populated on apply",
        "created_at": "local bookkeeping",
    },
    "lead_email_verification": {
        "id": "surrogate key",
        "org_id": "implicit from the authenticated request",
        "lead_id": "join key",
        "email": "table is never serialized as its own entity today (Stage 7 introduces lead_provider_observations to fix this)",
        "status": "write-only column -- never leaves local SQLite (Stage 7)",
        "sub_status": "write-only column -- never leaves local SQLite (Stage 7)",
        "source": "read locally by _lev_sources_for_lead to derive leads.original/latest_email_verification_source, but this raw column is never put on the wire itself",
        "source_detail": "write-only column -- never leaves local SQLite (Stage 7)",
        "bounce_message": "write-only column -- never leaves local SQLite (Stage 7)",
        "free_email": "write-only column -- never leaves local SQLite (Stage 7)",
        "mx_found": "write-only column -- never leaves local SQLite (Stage 7)",
        "smtp_provider": "write-only column -- never leaves local SQLite (Stage 7)",
        "verified_at": "read locally to derive leads.email_verified_at, but this row is never transmitted itself",
        "created_at": "local bookkeeping",
    },
    "lead_provider_attempts": {
        "id": "surrogate key",
        "lead_id": "join key; covered by the parent lead_core entity_id",
        "batch_id": "FK to provider_batch_jobs, meaningless outside this install; dropped before serialization",
        "metadata_json": "dropped before serialization -- the provider_attempt_rows query in lead_sync.py does not select it",
    },
    "workspace_leads": {
        "id": "local surrogate; the composite lead_id:workspace_id is the wire entity_id",
        "org_id": "implicit from the authenticated request",
        "workspace_id": "join key / part of the composite entity_id, not a payload field",
        "lead_id": "join key / part of the composite entity_id, not a payload field",
        "owner_user_id": "CRM contact-owner assignment is not wired into the sync payload yet",
        "stage_entered_at": "not read by _assemble_lead_workspace_sync_payload",
        "last_activity_at": "internal 'any activity happened' bookkeeping, distinct from last_contacted_at which is what travels",
        "latest_sender": "not read by the workspace payload builder; leads.latest_sender (the core-level column) is what travels",
        "created_at": "audit only",
        "updated_at": "audit only",
    },
    "workspace_lead_tags": {
        "id": "local surrogate",
        "workspace_id": "join key",
        "lead_id": "join key",
        "created_at": "used only to order tags locally; not itself transmitted",
    },
    "workspace_lead_linkedin_status": {
        "id": "local surrogate",
        "workspace_id": "join key",
        "lead_id": "join key",
        "connected_at": "not read by the workspace payload builder -- only the boolean flags travel",
        "request_sent_at": "not read by the workspace payload builder -- only the boolean flags travel",
        "updated_at": "audit only",
    },
    "crm_entity_map": {
        "workspace_id": "join key",
        "lead_id": "join key",
        "sync_error": "local diagnostic, excluded from the SELECT that builds the payload",
        "created_at": "audit only, excluded from the SELECT that builds the payload",
        "updated_at": "audit only, excluded from the SELECT that builds the payload",
    },
    "companies": {
        "id": "local surrogate; `uid` is the wire key, not this row id",
        "headcount_numeric": "derived numeric parse of headcount, recomputed locally rather than shipped",
        "hq_city": "collected for enrichment but not read by build_company_sync_payload today",
        "hq_state": "collected for enrichment but not read by build_company_sync_payload today",
        "hq_country": "collected for enrichment but not read by build_company_sync_payload today",
        "created_at": "audit only",
        "updated_at": "audit only",
    },
    "company_personalization": {
        "company_id": "join key; covered by the parent company entity_id",
        "source_hash": "local change-detection hash, not needed by the relay",
    },
    "sender_accounts": {
        "id": "local surrogate; not part of the payload body",
        "org_id": "implicit from the authenticated request",
        "channel": "not read by build_sender_account_sync_payload (_SYNC_PAYLOAD_COLUMNS)",
        "created_at": "audit only",
        "updated_at": "audit only",
        "email_domain": "derived from email, recomputed locally rather than shipped",
        "last_outbound_at": "activity bookkeeping, not read by build_sender_account_sync_payload",
        "last_inbound_at": "activity bookkeeping, not read by build_sender_account_sync_payload",
    },
    "sender_domains": {
        "created_at": "audit only",
        "updated_at": "audit only",
    },
}


def classified_columns(table: str) -> set[str]:
    """Union of SYNCED and NOT_SYNCED columns declared for `table`."""
    return set(SYNCED_COLUMNS.get(table, ())) | set(NOT_SYNCED_COLUMNS.get(table, {}))
