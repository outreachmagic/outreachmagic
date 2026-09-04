# Outreach Magic — database schema

**Generated** by `scripts/generate_schema_reference.py`. Do not hand-edit; run
`make manifests` (or the script directly) after a schema change.

Interactive equivalents:

```bash
python3 scripts/pipeline.py schema                 # every table, with its purpose
python3 scripts/pipeline.py schema leads           # one table's columns and FKs
python3 scripts/pipeline.py schema --relations     # the join graph
```

## Scoping anything to a workspace

This is the question that costs the most time, because the answer is different
per entity. `leads` has **no** `workspace_id` column.

| Entity | How it is scoped |
|--------|------------------|
| `leads` | via workspace_leads(workspace_id, lead_id) — no column on leads |
| `campaigns` | direct campaigns.workspace_id |
| `events` | via events.campaign_id -> campaigns.workspace_id |
| `sender_accounts` | via workspace_sender_accounts(workspace_id, sender_account_id) |
| `bounce_events` | bounce_events.workspace_id (older rows may be null) |
| `campaign_workspace_map` | relay-platform routing only — not a join path for events |

## Tables

### `bounce_events`

Deduplicated bounces with message, SMTP code and recipient MX. Never pruned — full history.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `first_event_id` | INTEGER | → `events.id` |
| `latest_event_id` | INTEGER | → `events.id` |
| `platform` | TEXT | not null |
| `sender_email` | TEXT | not null |
| `lead_email` | TEXT | not null |
| `bounce_type` | TEXT | not null |
| `bounce_message` | TEXT |  |
| `smtp_code` | TEXT |  |
| `recipient_mx` | TEXT |  |
| `sender_mx` | TEXT |  |
| `campaign_id` | INTEGER | → `campaigns.id` |
| `campaign_name` | TEXT |  |
| `workspace_id` | TEXT |  |
| `relay_id` | TEXT |  |
| `occurrence_count` | INTEGER | not null |
| `first_seen_at` | TEXT | not null |
| `last_seen_at` | TEXT | not null |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |

### `campaign_leads`

Which leads belong to which campaign.

| Column | Type | Notes |
|---|---|---|
| `campaign_id` | INTEGER | primary key, not null, → `campaigns.id` |
| `lead_id` | INTEGER | primary key, not null, → `leads.id` |
| `added_at` | TEXT | not null |

### `campaign_workspace_map`

Relay-platform campaign routing (campaign_platform_id = the sequencer's own id). NOT a join path for events.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `source_platform` | TEXT | not null |
| `campaign_platform_id` | TEXT |  |
| `campaign_name_normalized` | TEXT |  |
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `match_strategy` | TEXT | not null |
| `priority` | INTEGER | not null |
| `is_active` | INTEGER | not null |
| `cloud_synced` | INTEGER | not null |
| `map_source` | TEXT | not null |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |

### `campaigns`

Campaign names and their workspace. events.campaign_id joins here.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `name` | TEXT | not null |
| `description` | TEXT |  |
| `status` | TEXT | not null |
| `workspace_id` | TEXT | → `workspaces.id` |
| `reporting_role` | TEXT |  |
| `created_at` | TEXT | not null |

### `companies`

Canonical company records. Linked from leads.company_id.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `name` | TEXT | not null |
| `domain` | TEXT |  |
| `industry` | TEXT |  |
| `headcount` | TEXT |  |
| `headcount_numeric` | INTEGER |  |
| `hq_city` | TEXT |  |
| `hq_state` | TEXT |  |
| `hq_country` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `uid` | TEXT |  |
| `category` | TEXT |  |

### `company_contact_observations`

Contacts seen on a company's own pages during sourcing.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `company_id` | INTEGER | not null |
| `workspace_id` | TEXT |  |
| `ran_at` | TEXT | not null |
| `provider` | TEXT | not null |
| `url` | TEXT |  |
| `cache_hit` | INTEGER | not null |
| `fetch_ms` | INTEGER |  |
| `http_status` | INTEGER |  |
| `regex_found` | INTEGER | not null |
| `extractor` | TEXT |  |
| `contacts_attached` | INTEGER | not null |
| `contacts_queued` | INTEGER | not null |
| `icp_config_hash` | TEXT |  |
| `cost_estimate_usd` | REAL |  |
| `outcome` | TEXT | not null |
| `error` | TEXT |  |
| `decision_json` | TEXT |  |

### `company_identities`

A company's domains and other identifiers, with provenance. Where a company's extra domains live.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `org_id` | TEXT | not null |
| `company_id` | INTEGER | not null, → `companies.id` |
| `identity_type` | TEXT | not null |
| `identity_value_normalized` | TEXT | not null |
| `role` | TEXT |  |
| `verified_mx` | INTEGER |  |
| `source` | TEXT |  |
| `is_verified` | INTEGER | not null |
| `created_at` | TEXT | not null |
| `label` | TEXT |  |
| `purpose` | TEXT |  |

### `company_merge_candidates`

Proposed company merges awaiting a decision.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `candidate_company_id` | INTEGER |  |
| `existing_company_id` | INTEGER | not null |
| `reason` | TEXT | not null |
| `status` | TEXT | not null |
| `payload_json` | TEXT | not null |
| `received_at` | TEXT | not null |
| `resolved_at` | TEXT |  |

### `company_merges`

Audit trail of executed company merges.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `keep_id` | INTEGER | not null, → `companies.id` |
| `merge_id` | INTEGER | not null |
| `reason` | TEXT |  |
| `merge_entity_key` | TEXT |  |
| `relay_delete_pushed` | INTEGER | not null |
| `merged_at` | TEXT | not null |

### `company_page_cache`

Fetched company pages, cached so sourcing does not pay twice.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `company_id` | INTEGER | → `companies.id` |
| `url` | TEXT | not null |
| `url_hash` | TEXT | not null |
| `fetched_at` | TEXT | not null |
| `fetch_method` | TEXT | not null |
| `http_status` | INTEGER |  |
| `content_hash` | TEXT |  |
| `char_count` | INTEGER |  |
| `markdown` | TEXT |  |

### `company_personalization`

Per-company personalization values.

| Column | Type | Notes |
|---|---|---|
| `company_id` | INTEGER | primary key, not null, → `companies.id` |
| `field_name` | TEXT | primary key, not null |
| `field_value` | TEXT | not null |
| `field_date` | TEXT |  |
| `source_hash` | TEXT |  |
| `processed_at` | TEXT | not null |

### `crm_entity_map`

Local entity to CRM record id mapping.

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `platform` | TEXT | not null |
| `crm_contact_id` | TEXT |  |
| `crm_deal_id` | TEXT |  |
| `crm_company_id` | TEXT |  |
| `crm_owner_id` | TEXT |  |
| `last_synced_at` | TEXT |  |
| `last_event_id_synced` | TEXT |  |
| `last_sync_status` | TEXT | not null |
| `sync_error` | TEXT |  |
| `sync_hash` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `crm_note_id` | TEXT |  |

### `crm_sync_log`

What was pushed to the CRM, and when.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `platform` | TEXT | not null |
| `started_at` | TEXT | not null |
| `completed_at` | TEXT |  |
| `leads_checked` | INTEGER | not null |
| `contacts_created` | INTEGER | not null |
| `contacts_updated` | INTEGER | not null |
| `opportunities_created` | INTEGER | not null |
| `opportunities_updated` | INTEGER | not null |
| `events_pushed` | INTEGER | not null |
| `skipped` | INTEGER | not null |
| `errors` | INTEGER | not null |
| `error_details` | TEXT |  |
| `status` | TEXT | not null |

### `crm_workspace_config`

Per-workspace CRM connection settings.

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `platform` | TEXT | not null |
| `api_key` | TEXT | not null |
| `location_id` | TEXT |  |
| `pipeline_id` | TEXT |  |
| `stage_mapping` | TEXT | not null |
| `contact_field_mapping` | TEXT |  |
| `overwrite_existing` | INTEGER | not null |
| `contact_owner_id` | TEXT |  |
| `enabled` | INTEGER | not null |
| `updated_at` | TEXT | not null |

### `event_push_log`

Outbound push state: has this event been sent to the relay.

| Column | Type | Notes |
|---|---|---|
| `event_id` | INTEGER | primary key, → `events.id` |
| `pushed_at` | TEXT | not null |

### `events`

Primary timeline: sends, replies, bounces, status labels. Use this for analytics.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `event_type` | TEXT | not null |
| `direction` | TEXT | not null |
| `channel` | TEXT | not null |
| `subject` | TEXT |  |
| `body_preview` | TEXT |  |
| `metadata_json` | TEXT |  |
| `campaign_id` | INTEGER | → `campaigns.id`, **The campaign the PLATFORM stamped on the event. A booking webhook stamps the calendar slot, so meetings counted off this column credit the calendar rather than the sequence that earned them.** |
| `sender` | TEXT |  |
| `relay_id` | INTEGER |  |
| `created_at` | TEXT | not null |

### `lead_emails`

Additional email addresses for a lead.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `email` | TEXT | not null |
| `is_primary` | INTEGER | not null |
| `created_at` | TEXT | not null |

### `lead_identities`

A lead's identifying values (email, LinkedIn) with provenance.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `org_id` | TEXT | not null |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `identity_type` | TEXT | not null |
| `identity_value_normalized` | TEXT | not null |
| `source` | TEXT |  |
| `is_verified` | INTEGER | not null |
| `created_at` | TEXT | not null |

### `lead_links`

Two people in one conversation (someone replied and forwarded us to a colleague). Directional: from_lead_id introduced us to to_lead_id. Non-destructive — both leads stay whole; reporting counts distinct conversation roots so the thread counts once.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `from_lead_id` | INTEGER | not null, → `leads.id` |
| `to_lead_id` | INTEGER | not null, → `leads.id` |
| `kind` | TEXT | not null |
| `note` | TEXT |  |
| `created_at` | TEXT | not null |
| `created_by` | TEXT |  |

### `lead_merge_jobs`

Proposed lead merges awaiting a decision.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `keep_lead_id` | INTEGER | not null |
| `merge_lead_id` | INTEGER | not null |
| `status` | TEXT | not null |
| `reason` | TEXT |  |
| `audit_json` | TEXT |  |
| `created_at` | TEXT | not null |

### `lead_merges`

Audit trail of merged lead records.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `keep_id` | INTEGER | not null, → `leads.id` |
| `merge_id` | INTEGER | not null |
| `reason` | TEXT |  |
| `merge_entity_key` | TEXT |  |
| `relay_delete_pushed` | INTEGER | not null |
| `merged_at` | TEXT | not null |

### `lead_personalization`

Per-lead personalization values.

| Column | Type | Notes |
|---|---|---|
| `lead_id` | INTEGER | primary key, not null, → `leads.id` |
| `field_name` | TEXT | primary key, not null |
| `field_value` | TEXT | not null |
| `field_date` | TEXT |  |
| `source_hash` | TEXT |  |
| `processed_at` | TEXT | not null |

### `lead_provider_observations`

Per-provider enrichment attempts and their outcomes.

| Column | Type | Notes |
|---|---|---|
| `obs_uid` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `kind` | TEXT | not null |
| `origin` | TEXT | not null |
| `provider` | TEXT | not null |
| `email` | TEXT |  |
| `status` | TEXT | not null |
| `sub_status` | TEXT |  |
| `domain` | TEXT |  |
| `source_detail` | TEXT |  |
| `bounce_message` | TEXT |  |
| `free_email` | INTEGER |  |
| `mx_found` | INTEGER |  |
| `smtp_provider` | TEXT |  |
| `result_email` | TEXT |  |
| `result_validity` | TEXT |  |
| `batch_id` | INTEGER |  |
| `metadata_json` | TEXT |  |
| `observed_at` | TEXT | not null |
| `completed_at` | TEXT |  |
| `created_at` | TEXT | not null |

### `leads`

One row per lead (email and/or LinkedIn identity). Global — workspace scoping lives in workspace_leads.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `name` | TEXT | not null |
| `company_id` | INTEGER | → `companies.id` |
| `company` | TEXT |  |
| `title` | TEXT |  |
| `industry` | TEXT |  |
| `headcount` | TEXT |  |
| `headcount_numeric` | INTEGER |  |
| `email` | TEXT |  |
| `email_domain` | TEXT |  |
| `linkedin_url` | TEXT |  |
| `linkedin_sales_nav_id` | TEXT |  |
| `location_city` | TEXT |  |
| `location_state` | TEXT |  |
| `location_country` | TEXT |  |
| `channel` | TEXT | not null |
| `stage` | TEXT | not null |
| `notes` | TEXT |  |
| `original_source` | TEXT |  |
| `original_source_detail` | TEXT |  |
| `original_source_platform` | TEXT |  |
| `original_source_at` | TEXT |  |
| `latest_source` | TEXT |  |
| `latest_source_detail` | TEXT |  |
| `latest_source_platform` | TEXT |  |
| `latest_source_at` | TEXT |  |
| `email_verification_status` | TEXT |  |
| `email_verified_at` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `last_contact_at` | TEXT |  |
| `next_action` | TEXT |  |
| `latest_sender` | TEXT |  |
| `latest_sender_platform` | TEXT |  |
| `linkedin_headline` | TEXT |  |
| `linkedin_bio` | TEXT |  |
| `uid` | TEXT |  |
| `record_type` | TEXT | not null |
| `superseded_at` | TEXT |  |
| `fallback_email_lead_id` | INTEGER | → `leads.id` |
| `is_test` | INTEGER | not null |

### `leads_junk_quarantine`

Rows held back as junk rather than imported.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `lead_id` | INTEGER |  |
| `uid` | TEXT |  |
| `original_source_detail` | TEXT |  |
| `quarantined_at` | TEXT | not null |

### `migration_flags`

One-shot migration markers, so a backfill runs once.

| Column | Type | Notes |
|---|---|---|
| `name` | TEXT | primary key |
| `done_at` | TEXT | not null |

### `organizations`

The org a database belongs to (single row in normal use).

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `name` | TEXT | not null |
| `workspace_routing_mode` | TEXT | not null |
| `default_workspace_id` | TEXT |  |
| `created_at` | TEXT | not null |

### `outbox`

Pending entity changes queued for the relay.

| Column | Type | Notes |
|---|---|---|
| `entity_type` | TEXT | primary key, not null |
| `entity_id` | TEXT | primary key, not null |
| `op` | TEXT | primary key, not null |
| `entity_key` | TEXT |  |
| `workspace_slug` | TEXT |  |
| `dirty_at` | TEXT | not null |
| `attempts` | INTEGER | not null |
| `last_error` | TEXT |  |

### `personalization_fields`

Registry of personalization field names and their scope.

| Column | Type | Notes |
|---|---|---|
| `field_name` | TEXT | primary key |
| `scope` | TEXT | not null |
| `label` | TEXT |  |
| `description` | TEXT |  |
| `first_seen_at` | TEXT | not null |
| `first_seen_source` | TEXT |  |

### `phone_numbers`

Phone numbers owned by a lead or company.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `owner_type` | TEXT | not null |
| `owner_id` | INTEGER | not null |
| `phone_e164` | TEXT | not null |
| `phone_raw` | TEXT |  |
| `label` | TEXT | not null |
| `source` | TEXT |  |
| `is_primary` | INTEGER | not null |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |

### `provider_batch_jobs`

Submitted provider batch jobs awaiting download.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `provider` | TEXT | not null |
| `kind` | TEXT | not null |
| `job_id` | TEXT | not null |
| `workspace_id` | TEXT |  |
| `item_count` | INTEGER | not null |
| `item_set_hash` | TEXT | not null |
| `status` | TEXT | not null |
| `submitted_at` | TEXT | not null |
| `completed_at` | TEXT |  |
| `metadata_json` | TEXT |  |

### `relay_ingested`

Inbound dedupe ledger — a hash per ingested relay event.

| Column | Type | Notes |
|---|---|---|
| `dedupe_hash` | BLOB | primary key |
| `lead_id` | INTEGER | → `leads.id` |
| `ingested_at` | TEXT | not null |

### `sender_accounts`

Your sending mailboxes. provider is sequencer-owned.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `org_id` | TEXT | not null |
| `email` | TEXT | not null |
| `channel` | TEXT | not null |
| `external_id` | TEXT |  |
| `provider` | TEXT |  |
| `first_name` | TEXT |  |
| `last_name` | TEXT |  |
| `daily_limit` | INTEGER |  |
| `warmup_status` | TEXT |  |
| `warmup_enabled_date` | TEXT |  |
| `status` | TEXT |  |
| `spf_status` | TEXT |  |
| `dkim_status` | TEXT |  |
| `dmarc_status` | TEXT |  |
| `warmup_max_daily_limit` | INTEGER |  |
| `overall_health_score` | INTEGER |  |
| `google_health_score` | INTEGER |  |
| `microsoft_health_score` | INTEGER |  |
| `other_health_score` | INTEGER |  |
| `ooo_rr` | REAL |  |
| `ooo_rr_14` | REAL |  |
| `ooo_rr_30` | REAL |  |
| `ooo_rr_90` | REAL |  |
| `bounce_rate` | REAL |  |
| `miss_warmup_rate` | REAL |  |
| `tags_json` | TEXT |  |
| `source_created_at` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `linkedin_url` | TEXT |  |
| `linkedin_sales_nav_id` | TEXT |  |
| `email_domain` | TEXT |  |
| `last_outbound_at` | TEXT |  |
| `last_inbound_at` | TEXT |  |
| `is_active` | INTEGER | not null |

### `sender_domains`

Your sending domains: reseller, provider, cost, DNSBL status, registration date.

| Column | Type | Notes |
|---|---|---|
| `domain` | TEXT | primary key |
| `reseller` | TEXT |  |
| `domain_cost` | REAL |  |
| `currency` | TEXT | not null |
| `notes` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `sending_ip` | TEXT |  |
| `dnsbl_status` | TEXT |  |
| `provider` | TEXT |  |
| `registration_date` | TEXT |  |
| `registration_checked_at` | TEXT |  |
| `registration_source` | TEXT |  |
| `is_active` | INTEGER | not null |
| `purpose` | TEXT | not null |
| `company_id` | INTEGER |  |

### `suppression_entries`

Materialized matches of a suppression rule to specific contacts.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `workspace_id` | TEXT | → `workspaces.id` |
| `scope` | TEXT | not null |
| `entry_type` | TEXT | not null |
| `value_raw` | TEXT | not null |
| `value_normalized` | TEXT | not null |
| `reason` | TEXT | not null |
| `note` | TEXT |  |
| `source` | TEXT |  |
| `created_at` | TEXT | not null |
| `created_by` | TEXT |  |
| `expires_at` | TEXT |  |
| `revoked_at` | TEXT |  |

### `sync_audit`

Record of sync pushes and pulls for debugging.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `direction` | TEXT | not null |
| `action` | TEXT |  |
| `entity_key` | TEXT |  |
| `workspace` | TEXT |  |
| `payload_json` | TEXT | not null |
| `content_hash` | TEXT |  |
| `relay_id` | INTEGER |  |
| `http_status` | INTEGER |  |
| `error` | TEXT |  |
| `batch_label` | TEXT |  |
| `created_at` | TEXT | not null |

### `sync_shadow`

Last-synced copy of an entity, for change detection.

| Column | Type | Notes |
|---|---|---|
| `entity_type` | TEXT | primary key, not null |
| `entity_key` | TEXT | primary key, not null |
| `workspace_slug` | TEXT | primary key, not null |
| `content_hash` | TEXT | not null |
| `relay_seq` | INTEGER |  |
| `synced_at` | TEXT | not null |

### `unmapped_campaign_queue`

Quarantined webhook events awaiting routing.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `source_platform` | TEXT | not null |
| `campaign_platform_id` | TEXT |  |
| `campaign_name_raw` | TEXT |  |
| `campaign_name_normalized` | TEXT |  |
| `external_event_id` | TEXT |  |
| `reason` | TEXT | not null |
| `status` | TEXT | not null |
| `payload_json` | TEXT | not null |
| `received_at` | TEXT | not null |
| `resolved_at` | TEXT |  |
| `assigned_workspace` | TEXT |  |

### `workspace_icp_profiles`

Saved ICP definitions per workspace.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `workspace_id` | TEXT | not null |
| `name` | TEXT | not null |
| `config_json` | TEXT | not null |
| `config_hash` | TEXT | not null |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |

### `workspace_lead_events`

Workspace-scoped ingest audit — a subset, not full relay volume.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | primary key |
| `org_id` | TEXT | not null |
| `workspace_id` | TEXT | not null |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `event_id` | INTEGER | → `events.id` |
| `event_type` | TEXT | not null |
| `event_at` | TEXT | not null |
| `idempotency_key` | TEXT | not null |
| `created_at` | TEXT | not null |

### `workspace_lead_linkedin_status`

Per-workspace LinkedIn connection state for a lead.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `sender_profile` | TEXT | not null |
| `is_connected` | INTEGER | not null |
| `is_request_pending` | INTEGER | not null |
| `connected_at` | TEXT |  |
| `request_sent_at` | TEXT |  |
| `updated_at` | TEXT | not null |

### `workspace_lead_suppressions`

Per-workspace suppression of one lead.

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | TEXT | primary key, not null, → `workspaces.id` |
| `lead_id` | INTEGER | primary key, not null, → `leads.id` |
| `entry_id` | TEXT | primary key, not null, → `suppression_entries.id` |
| `matched_on` | TEXT | not null |
| `matched_at` | TEXT | not null |

### `workspace_lead_tags`

Workspace-scoped tags on a lead.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `tag` | TEXT | not null |
| `created_at` | TEXT | not null |

### `workspace_leads`

Per-workspace lead status, sentiment anchor and activity. The leads-to-workspace join.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null |
| `workspace_id` | TEXT | not null, → `workspaces.id` |
| `lead_id` | INTEGER | not null, → `leads.id` |
| `status` | TEXT | not null |
| `owner_user_id` | TEXT |  |
| `stage_entered_at` | TEXT |  |
| `last_activity_at` | TEXT | **Any event in any campaign, inbound or outbound.** |
| `current_status_label` | TEXT |  |
| `current_status_sentiment` | TEXT | **The lead's sentiment RIGHT NOW. A lead that went positive and later cold is not counted here; `campaign-audit --positive-anchor first` answers the historical question.** |
| `current_sentiment_since` | TEXT | **When the status LAST CHANGED. Not first contact, not first positive. Windowing on this to find recent positives silently drops every lead whose status has not moved since; use first_positive_at.** |
| `contact_priority` | INTEGER |  |
| `latest_sender` | TEXT |  |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |
| `first_positive_at` | TEXT | **First time this lead ever turned positive — the anchor behind `--positive-anchor first`, and the column to window on.** |
| `email_sent_count` | INTEGER | not null, **LIFETIME and WORKSPACE-WIDE — not sends in one campaign. For per-campaign sends use `campaign-audit` or the v_campaign_lead_events view.** |
| `linkedin_sent_count` | INTEGER | not null, **LIFETIME and WORKSPACE-WIDE, like email_sent_count.** |
| `total_replies_count` | INTEGER | not null, **LIFETIME and WORKSPACE-WIDE — not replies in one campaign or window.** |
| `last_contacted_at` | TEXT |  |

### `workspace_sender_accounts`

Which workspaces a mailbox serves.

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | TEXT | primary key, not null, → `workspaces.id` |
| `sender_account_id` | INTEGER | primary key, not null, → `sender_accounts.id` |
| `created_at` | TEXT | not null |

### `workspaces`

Multi-workspace routing. Slug column is `slug`, not `workspace_slug`.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | primary key |
| `org_id` | TEXT | not null, → `organizations.id` |
| `name` | TEXT | not null |
| `slug` | TEXT | not null |
| `cloud_synced` | INTEGER | not null |
| `created_at` | TEXT | not null |
| `updated_at` | TEXT | not null |


## Two different campaign IDs

| Name | What it is |
|------|-----------|
| `campaigns.id` | The local campaign row. **This** is what `events.campaign_id` joins to. |
| `campaign_workspace_map.campaign_platform_id` | The *sequencer's own* campaign id, used for routing inbound webhooks (`campaignPlatformId` on the wire). It is **not** a join path from `events`. |

Reaching a workspace from an event goes `events.campaign_id → campaigns.id →
campaigns.workspace_id`, never through `campaign_workspace_map`.

## Ready-made helpers

`pipeline.py sql --workspace <slug>` pre-binds these CTEs so the joins above are
already done: `ws_workspace`, `ws_leads`, `ws_campaigns`, `ws_events`,
`ws_sender_accounts`.

```bash
python3 scripts/pipeline.py sql "SELECT stage, COUNT(*) FROM ws_leads GROUP BY stage" \
  --workspace acme --json
```

Higher-level answers already have commands — prefer them over raw SQL:

```bash
python3 scripts/pipeline.py query deliverability --workspace acme --since 7d --json
python3 scripts/pipeline.py deliverability domains --workspace acme --json
python3 scripts/pipeline.py deliverability bounces --workspace acme --group-by signature --json
python3 scripts/pipeline.py campaign-replies --workspace acme --kind human --json
```
