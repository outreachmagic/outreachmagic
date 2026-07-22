# Changelog

## [1.5.0] — Domain discovery, pipeline refactor, cost controls

### Added

- **`find-domains`** — new command: resolve a company name to its authoritative domain(s) and claim any name-matched public email addresses before spending credits on paid providers.
- **`claim-public-emails`** — standalone command to attach a verified-by-name-match public email to a lead without touching the waterfall.
- **`--skip-catchall-after N`** — stop paying for email finding on domains that have already been confirmed as catch-all. Prevents repeated credit waste on unverifiable addresses.
- **`--abandon-after N`** — give up on a domain after N failed provider attempts. Defaults to 0 (disabled) so existing installs are unaffected.
- **`import-linkedin-connections`** — bulk-import LinkedIn Connections CSV exports into the pipeline.
- **Outbox + dirty tracking** — SQLite triggers mark leads dirty on change; `outbox` command drains pending pushes reliably without cursor-drift.
- **Provider attempt tracking** — org-wide per-lead record of every provider attempt, verdict, and cost. Feeds `--skip-catchall-after` and `--abandon-after` decisions.
- **Blacklist monitor** (`blacklist_monitor.py`) — watches for leads that match blocklist entries and surfaces them.
- **`query` CLI** — read-only SQL REPL over the local database for ad-hoc inspection.
- **API key pool** (`api_key_pool.py`) — round-robin rotation across multiple API keys for providers that rate-limit per key.
- **Event deduplication** (`event_dedup.py`) — idempotent relay ingest; duplicate webhook payloads are absorbed without creating duplicate events.
- **Batch job tracking** — persisted job state for `scrubby-deep-submit`/`batch-find` so cross-session polling survives restarts.
- **Junk lead cleanup** (`junk_cleanup.py`) — automated quarantine and deletion of weak-identity leads.
- **Sync audit trail** (`sync_audit.py`, `sync_contract.py`) — per-column classification of what is and isn't synced to the relay; visible via `sync-debug`.

### Changed

- **`pipeline.py` refactored into 6 modules** — `pipeline_cli.py`, `pipeline_sync.py`, `pipeline_tags.py`, `pipeline_workspace.py`, `pipeline_migration.py`, `pipeline_utils.py`, `pipeline_update.py`, `pipeline_personalize.py`. Entry point is unchanged; all existing commands work as before.
- **Domain scoring** — apex domains rank above their own subdomains; aggregator subdomains (`jobs.`, `careers.`, etc.) are rejected; `www2.`/`www3.` host prefixes are stripped. Name-match validates email-derived domains against the company name before accepting them.
- **`domain_discovered` tag** — collapsed from one tag per domain (`domain_found_<domain>`) to a single `domain_discovered` flag. `reconcile-domain-tags` backfills existing installs.
- **Tag normalization fix** — read and write paths now use the same canonical normalizer (`pipeline_utils.normalize_tag`). A divergence where the read path converted spaces to underscores caused `--tag` filters to silently return 0 results for any tag containing spaces.
- **`personalized_` prefix** — replaces `mailmerge_` throughout the skill (personalization columns, CLI flags, exports).
- **`--tag` / `--exclude-tag` scoping** — added to `verify-bulk` and `email-finding-candidates` so you can target verification runs by tag without exporting first.
- **Sales Navigator ID casing** — IDs are now folded to lowercase; a one-time migration merges any case-split duplicates.
- **Entity key is immutable** — relay `entity_key` (uid) cannot change after creation; write attempts raise an error rather than silently forking a lead.
- **GHL CRM sync** — tracks `crm_note_id`, auto-generates an OM summary note on contact creation, and only pushes email events (not all event types).
- **Relay event envelope** — unified 5-field format (`platform`, `entity_key`, `event_type`, `received_at`, `payload`) across webhook and agent-push events.
- **Company snapshots** — relay now produces authoritative company snapshots; `pull` updates local `companies` table without relying on lead payloads for company-level fields.

### Fixed

- **`BrokenPipeError` in background batch runs** — progress output now silently drops when the parent agent disconnects stdout/stderr rather than crashing the process.
- **Concurrent batch process SIGTERM** — `batch-find` now acquires an exclusive `flock()` lock at startup. A second concurrent batch from another agent session fails immediately with a clear message instead of blocking and getting killed by the host watchdog.
- **`--skip-catchall-after` pre-flight seeding** — at batch start, domains already confirmed catch-all in `lead_provider_observations` are seeded into the in-memory skip table so the first lead from a known-bad domain is skipped without an API call, not just leads discovered mid-run.

### Removed

- `skills/email-finder/` and `skills/lead-enrich/` directories (consolidated in v1.4.0; residual exports cleaned up).

## [1.4.0] — Skill consolidation (lead-enrich + email-finder merged into outreachmagic)

### Added

- **Consolidated skill.** `lead-enrich` and `email-finder` are now merged directly into `outreachmagic`. One install, one SKILL.md, one update path. Agent discovers all capabilities — pipeline sync, person research, email finding, and email verification — from a single skill.
- **Provider split.** `providers.py` split into `waterfall.py` (orchestration + registry), `trykitt.py` (trykitt API client), and `icypeas.py` (Icypeas API client). New providers register in `_PROVIDER_REGISTRY` — no if/elif chain.

### Changed

- **`enrich.py`** and **`email_finder.py`** moved from companion directories into `skills/outreachmagic/scripts/`. All imports updated to use consolidated `shared.py`.
- **`shared.py`** replaces the old `companion_common.py` (canonical copy from email-finder with Scrubby functions).
- **SKILL.md** — consolidated frontmatter includes all API keys (`SERPER_API_KEY`, `TRYKITT_API_KEY`, `ICYPEAS_API_KEY`, `MILLIONVERIFIER_API_KEY`, `SCRUBBY_API_KEY`), all `external_domains`, and a combined "Common workflows" table covering all capabilities.
- **README.md** — capability table, combined ASCII diagram, and single keys table replace companion cross-references.
- **`skill-suite.json`** — removed `email-finder` and `lead-enrich` entries. Only `outreachmagic` remains.
- **CI/CD** — deleted `publish-email-finder.yml` and `publish-lead-enrich.yml`. Simplified `skill-scan.yml`.
- **`install.sh`** — removed companion repo cloning, CLI args, and install functions. Fresh install copies all 14 `.py` files from `skills/outreachmagic/scripts/`.
- **`update-manifest.json`** — regenerated to include all 14 `.py` files (auto-discovers via `generate_skill_manifest.py`).

### Removed

- `skills/lead-enrich/` — entire directory
- `skills/email-finder/` — entire directory
- `.github/workflows/publish-email-finder.yml`
- `.github/workflows/publish-lead-enrich.yml`
- `scripts/sync-companion-common.sh`
- `scripts/validate-companion-manifests.py`
- `platforms/common/install-companions.sh`
- `tests/test_companion_common_sync.py`

### Deprecation

- Existing standalone installs of `lead-enrich` and `email-finder` will stop receiving updates. Users should install `outreachmagic/outreachmagic` via `npx skills add outreachmagic/outreachmagic`, then remove the old companion skills. Final companion releases include deprecation notices.

## [1.3.0] - 2026-06-30

### Added

- **Company snapshots.** Relay now produces authoritative company snapshots (`relay_company_snapshots`) alongside lead core/workspace snapshots. `pipeline.py pull` fetches company snapshots and updates the local companies table (industry, headcount, location) with authoritative values.
- **Unified event envelope.** All relay events (webhook + agent push) now use a 5-field format: `platform`, `entity_key`, `event_type`, `received_at`, `payload`. The old `lead`/`raw`/`sender` top-level fields are replaced by `entity_key`/`payload`/`payload.sender`. Webhook events nest the entire original body under `payload`; agent events nest action + client + workspace + timestamp + data under `payload`.
- **Company dedup in lead sync.** Lead core snapshots no longer carry company-level fields (`company_domain`, `industry`, `headcount`, `hq_*`). Company data lives only in `relay_company_snapshots`, synchronized by the authoritative company snapshot pipeline.
- `mongodb_to_d1.py` migration script. One-time tool to import ~121K historical acme events from MongoDB into D1 in the new 5-field envelope format, with dedup by message_id and fingerprint. Supports `--dry-run` and `--resume-from`.

### Changed

- `relay_ingest.py`: All raw/payload and lead/entity_key references updated for the new envelope. Dedup keys now read from `payload.message_id`, `payload.sent_email_id` instead of `raw.*`. Timestamp extraction checks `sent_on` in addition to existing keys.
- `pipeline.py`: Company snapshot support added to all pull phases. `ensure_company()` gains `authoritative` mode that overwrites (instead of COALESCE) industry/headcount/location. Agent company-sync handler now uses `apply_agent_company_sync_payload` which updates company fields authoritatively.
- `lead_sync.py`: Removed company fields from lead sync payload. `link_lead_company` simplified to just link by email.

## [1.2.0] - 2026-06-26

### Added

- **Scrubby Deep Verification.** Optional second-pass email verification that takes 24–72 hours for higher accuracy on catch-all and unknown emails. Submit batches with `scrubby-deep-submit`, poll results with `scrubby-deep-fetch`, or use `verify-with-scrubby` for a combined MillionVerifier + Scrubby workflow. 3 credits per email. Job state is persisted locally for cross-session polling.
- Multiple emails per lead. Each lead can have one primary email and any number of secondary emails stored in the new `lead_emails` table. Emails are unique per org across all leads.
- `additional_emails` column in lead review exports. Secondary emails appear as a semicolon-separated list with inline verification status (e.g. `alice@example.com [valid]; bob@example.com [bounced]`).
- Editable `additional_emails` sync-back. Add or remove secondary emails directly in Google Sheets review sheets — changes sync back to OutreachMagic on review sync. `[status]` brackets are stripped automatically on sync-back.
- Multi-tab Google Sheets export support via `addTabToSheet` / `writeValuesToTab` for building workbooks with multiple review tabs under one spreadsheet.
- Per-email verification in `bounces.py`. Verification records now link to specific `lead_emails.id` and materialize verification status per email on the `lead_emails` table.
- Secondary emails sync to CRMs (GHL alternateEmails, HubSpot hs_additional_emails) and via relay.
- Daily Breakdown tab in campaign stats sheets (`sheets campaign-stats`). Same metrics as Campaign Overview (sent, delivered, bounced, replies, OOO vs human, LinkedIn activity) but one row per campaign per day. Timezone offset configurable via `DAY_SPLIT_OFFSET_HOURS`.
- Settings metadata note in cell A1 of every sheet tab -- workspace, time window, generation timestamp, and timezone offset.
- Frozen header rows enabled by default on all campaign stats sheets.

### Changed

- `find_lead_by_email` searches `lead_emails` first, then `leads.email`.
- `resolve_lead` stores primary email in `lead_emails` on create and update.
- `merge_leads` moves secondary emails from the deleted lead to the kept lead.
- `apply_email_find_results` adds found emails as secondaries when the lead already has a primary.
- CRM sync hash includes additional emails so add/remove triggers re-sync.
- "Manual" renamed to "Human" across all campaign stats sheets. Column headers, funnel stage labels, and tab references now read OOO vs Human instead of OOO vs Manual.
- Tab titles cleaned up. Removed date-range prefix from individual tab names (e.g. "Last 14d - Campaign Overview" is now just "Campaign Overview"). Time window stays in the workbook-level title.

## [1.1.0] - 2026-06-19

### Added

- Campaign stats module with Google Sheets export. Run `sheets campaign-stats` from the pipeline to push workspace-level stats to a hosted workbook. Stats include campaign overview, conversion funnels, and lead sentiment per campaign.
- Brand asset pipeline. Logo SVGs publish to outreachmagic/brand on merge.

### Changed

- Platform registry maps more Prosp event types to local fields. Relay pull now handles `send_connection`, `send_msg`, and reply events from Prosp workspaces.
- Public READMEs rewritten for the full product suite. The GitHub org profile at github.com/outreachmagic now mirrors the same README, so visitors see a single consistent story wherever they land.
- Install docs synced to v1.1.0 across all docs sites.

### Fixed

- Lead enrich: `normalize_input` now accepts a `max_people` override. The `stamp-attempted` path always tags leads via the lightweight bulk endpoint and only touches import-profiles when notes are provided. The `serper-search` command writes to an `--out-file` when you pass one.
- Companion env loading tests now isolate from dev-shell API keys. Two tests that checked SERPER_API_KEY loading in strict mode were failing locally because they read from your running shell instead of the temp Hermes tree.
- Layer 1 test gate now includes campaign stats, platform registry, brand publish, and manifest sync tests. These were already in the full suite but missing from the fast pre-tag gate.

## [1.0.0] - 2026-06-17

### Added

- Initial release of the Outreach Magic skill suite.
- `pipeline.py` with relay pull from Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, and Calendly. SQLite-backed workspace routing, lead dedup, and campaign stats.
- Email finder companion skill with fallback provider chain.
- Lead enrich companion skill with Serper.dev integration.
- Update mechanism via `pipeline.py update` — pulls from GitHub releases, validates hashes, keeps a rollback copy.
- Install script at install.sh with platform detection (Hermes, Cursor, Claude Code, Claude desktop).
- Companion common module shared between email-finder and lead-enrich for env loading, API key pool rotation, and agent integration.
- Manifest system: every skill publishes an update-manifest.json with SHA256 hashes. The update command verifies integrity before applying changes.
- Billing contract tests at the database level.

[//]: # (Keep entries user-facing and specific. When you add a version, write what it does
       for someone running pipeline.py, not what changed in the codebase.)
