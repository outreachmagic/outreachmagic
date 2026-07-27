# Changelog

## [Unreleased]

### Added

- **One company-domain model** (`company_identities.purpose`, `pipeline.py company domain-purpose|detach-domain|split`) — the company pane used to render *two different tables* both headed "this company's domains": the identity-derived alias set, and `sender_domains`, which is your own cold-email sending infrastructure and has nothing to do with the prospect. Purpose now lives on `company_identities` — the set email finding actually walks and dedup matches on — with the vocabulary `primary · branch · email_finding · parked`, and the pane shows exactly one table (domain · purpose · role · MX · source), with `companies.domain` badged as the primary row. `sender_domains` keeps its `company_id`/`purpose` columns (no production row ever used the link, so there is nothing to migrate) but is no longer surfaced as a company's domains anywhere; `pipeline_sender_accounts.company_domains()` is deleted rather than left as a trap. Setting `primary` also moves `companies.domain`, because "primary" and the canonical identity column disagreeing is what quietly breaks dedup. `detach-domain` promotes the next-best remaining domain rather than leaving a company with an alias set but no canonical identity (which blocks email finding and reads as "missing domain"); `split` moves a domain *and every lead at that company whose email domain matches* to another company, then queues a reverse merge candidate so a mistaken split is visible and undoable through the existing review queue. `purpose` rides the company snapshot additively. The three concepts are now documented as a table in `references/command-reference.md`.
- **Contacts export with presets and a field picker** (`scripts/lead_export.py`, `pipeline.py export --preset|--fields|--list-fields`, dashboard **Export CSV**) — five presets a lead-gen agency actually needs (`sequencer-upload`, `enrichment-input`, `client-report`, `replies-review`, `full`), each seeding a multi-select column picker that includes every personalization field actually present in the workspace, read from the data rather than a fixed list. Six new computed columns — `last_message_sent_at/subject/body` and `last_message_received_at/subject/body` — as correlated subqueries mirroring the latest-reply join `campaign_replies` already used, with "received" resolving through the canonical reply condition so it means the same thing it means on the Replies tab. The filter set is not a new one: `search_leads`' WHERE builder was extracted to `dashboard_queries.lead_filter_clause()` and the export calls it, so "export whatever is on screen right now, all N of it" is literal rather than approximate — an unrecognized filter is rejected instead of silently dropped, which would quietly export more rows than asked for. Server-side: writes a CSV under the export dir and returns the path, rather than streaming 100k rows into the browser to assemble a Blob. `pipeline_tags.export_leads` — the parallel, divergent filter set that motivated the extraction — is now a shim; its own legacy filters (`--never-contacted` / `--no-email` / `--require-domain`) have no equivalent in the new path, so the legacy shape stays the default rather than being silently approximated.
- **Companies page: derived tags and account-coverage stats** — a company carries tag T if any of its leads in the workspace carries T. Derived, never stored: a `company_tags` table would need a sync surface and would drift from the lead tags that are the real source of truth, and because a `company_placeholder` lead is itself taggable, company-level-only tags still work (you tag the placeholder). `search_companies` gains `tag`, `missing_domain`, `no_reachable_contact` and `placeholder_only`; the new `/api/companies/stats` adds a tile row measuring *coverage and penetration* rather than the per-person reachability the contacts tiles measure — companies, reachable accounts, the email-finder work queue (0 reachable contacts), placeholder-only accounts, contacted / replied / positive, average contacts per company, and the missing domain/industry/headcount blockers. Every tile whose population is expressible as a filter is click-through, and the filter selects exactly the rows the tile counted. The stats query collapses to one row per company *before* aggregating — counting over the lead join would silently weight every tile by how many contacts a company happens to have.
- **Replies list: sentiment and lead-status filters** — with a `facets` block computed *before* those two filters apply, so each dropdown always lists every value available for the current campaign and date selection. Filtering to "positive" no longer strips the status-label dropdown down to the labels that happen to be positive. One endpoint, one round trip: a separate facets call would race the list against its own filter options.
- **Phone numbers as a first-class table** (`phone_numbers`, `pipeline.py phone list|add|promote|remove`) — polymorphic over leads and companies, so a person's mobile and a company switchboard live in one place with one set of verbs. Two separate columns: `label` (what kind of number — mobile/direct/main/hq/branch/fax/whatsapp/other) and `source` (where it came from — google_maps/apify/serper/apollo/csv_import/manual/crm/sequencer), both controlled vocabularies, so "the Google Maps number" is expressible without either fact swallowing the other. Numbers are stored E.164-normalized through the same function that builds `phone` lead identities, so reformatting can't create a duplicate and the stored number can't drift from the identity derived from it. Deliberately **not** personalization fields: personalization holds one value per field (no second number), can't normalize or dedup, and is a user namespace a client's own `phone` column would collide with — the same argument that made `record_type` a real column. Import routes `phone`/`phone_mobile` onto the lead and `company_phone` onto the company (with `phone_label`/`phone_source` overrides), and all five are reserved so they can never fall through to personalization. **CRM sync now populates `lead_data["phone"]`**, which the HubSpot and GHL drivers already read but nothing had ever filled: the lead's primary number, falling back to the company's `main`/`hq`/`direct` — never the fax. Surfaced in the dashboard on both the lead and company panes.

- **Local web dashboard** (`pipeline.py dashboard`) — zero-dependency (Python stdlib only) web UI at `http://127.0.0.1:8765` over the same SQLite database. Per-workspace deliverability (daily bounce trend, mailbox health with SPF/DKIM/DMARC + 7-day bounce flags, domain DNSBL status), pipeline stage drill-down, attribute performance ranking, per-campaign reply/bounce rates with top subject lines, and an activity feed. Write actions (stage change, attribute edit, event logging, pull/push) go through the same functions as the CLI, so outbox triggers queue them for relay push. No auth — loopback binding, Host-header and CSRF-header hardening; see the Local dashboard section of `references/command-reference.md` for limitations.
- **Dashboard: daily activity matrix** — per-day columns for every event kind (email sent/received, DM sent/received, connects sent/accepted, bounces, interested, not interested, meetings), chartable and filterable by campaign and date range.
- **Dashboard: copy audit** — campaign click-through shows sender accounts, activity span, lead stages, and subject lines with expandable full message bodies (from stored event metadata).
- **Dashboard: per-lead event history** — the lead slide-over shows the full timeline with previews and expandable full messages.
- **Dashboard: Data quality + enrichment tab** — under-enriched-lead buckets (missing email / company / title, unknown name, linkable) with counts honoring the active-in-range semantic. Select leads to: **one-click link** to a company from their company text (`link_lead_company`); **trigger the email finder** (company/multi-domain aware — resolves each lead's ranked `company_identities` domains, previews no-domain/multi-domain before spending credits, reuses the provider waterfall + `save_find_result`); or **run Serper web research** as a background job (results surface in the sync status for the agent to map). Plus an org-wide **cleanup** of truly-empty (event-less, name-"unknown") leads with a dry-run preview before the confirmed delete (`cleanup_junk_leads`).
- **Dashboard: CRM tab** — synced/stale/eligible/error counts, per-lead and bulk "sync to CRM" (reuses `crm_sync.sync_workspace`), recent run log.
- **Dashboard: Activity search** — the Activity feed now searches the whole date range server-side (not just the loaded page): an event-type dropdown (distinct types with counts) plus a text search across lead name / email / email-domain / LinkedIn / company / company-domain. Honors the global date range and stays keyset-paginated.
- **Dashboard: per-contact enrichment** — the lead slide-over now has **Find email** and **Research (Serper)** buttons that run the same background jobs on that single lead (previously only reachable as a bulk multi-select on the Data quality tab). Shows the resolved domain / existing email so it's clear what will be searched.
- **Dashboard: Sync tab honesty about deletes** — the push loop only drains `op='upsert'` outbox rows; `op='delete'` tombstones are local trigger artifacts the push never sends (real deletes travel via `lead_merges` / company merges). The Sync tab previously counted those deletes as "Pending push," so a successful push that drained every upsert still looked like a no-op. It now splits the summary into "N change(s) will push" vs "M delete tombstone(s) — local-only, not sent by Push," from new `pushable_total` / `delete_total` fields on the outbox query.
- **Dashboard: Sync tab** — outbox audit of everything queued to push back to Outreach Magic. The entry list is now server-side drill-downable — click a group (entity_type/op) to filter the rows to it — and labels truncation honestly (`showing 100 of 5,858`) instead of silently showing only the newest page; the client-side page-only filter is gone. Rows show the tombstone `entity_key`. Click any row for a detail slide-over: its queued operations, the resolved live record, and the exact payload the push will send — rebuilt with the same `sync_contract` payload builders `sync_all()` uses. Deletes are labeled as tombstones and explain that no content body is sent (just the key to remove) rather than showing a bare `{}`.
- **Dashboard: global date range** (presets + custom), sortable/filterable tables throughout, attribute insights normalized case-insensitively and filterable by campaign, sending-domain click-through (reseller, pricing, notes, per-domain mailboxes).
- **`lead_actions.py`** — the workspace-scoped write path for `update-stage`/`log-event` (workspace resolution, `workspace_leads` upsert, status event, workspace event index) extracted from the inline CLI dispatch into shared functions used by both the CLI and the dashboard.
- **`pipeline.py sync-health [--json]`** — one local screen per entity type (`lead_core`/`lead_workspace`/`company`/`sender_account`/`sender_domain`): local table count, outbox upsert backlog, legacy `sync_shadow` rows. Replaces eyeballing the raw `sync_shadow` total, which double-counts legacy natural-key rows alongside `uid:` rows for the same entity and is not a usable parity signal on its own. Status is `BACKLOG` / `SHADOW_STALE` / `IN_PARITY`; local-only, no relay call.
- **`pipeline.py shadow [--prune-legacy] [--dry-run]`** — preview or delete legacy natural-key `sync_shadow` rows for `lead_core`/`lead_workspace`/`company` (D1 is uid-only for these types; legacy rows are orphaned local metadata). Replaces hand-written SQL for this cleanup.

### Fixed

- **Dashboard: switching workspace now actually refreshes the page.** The fetch was always correct — the failure was downstream, in three places at once. Every loader is `const d = await get(…); if (!d) return;`, so on an error, an empty response, or a slow one it returned *without touching the DOM*, leaving the previous workspace's rows on screen looking like current data; loaders now blank their containers **before** awaiting, so a table either repopulates or goes visibly empty. Per-workspace filter state survived the switch in the DOM — `contacts-campaign`, `contacts-tag`, `contacts-sender`, `daily-campaign`, `attr-campaign` still held ids belonging to the old workspace, so the requery legitimately returned nothing, which reads exactly like a broken switch; `resetWorkspaceScopedState()` clears them along with every cached payload and filter set. And two concurrent requests could resolve out of order, letting the workspace you just left win: `state.wsGeneration` is captured by `get()` at request time and a response arriving under an older generation is discarded (as a `null`, not a rejection — a workspace switch is not an error and should not raise a toast).
- **Dashboard: workspace sending domains** now derive from the workspace's own mailboxes (LEFT JOIN to `sender_domains` for reseller/pricing/DNSBL) — workspaces whose domains were never registered in `sender_domains` (e.g. newer client workspaces) previously showed an empty domain list.
- **Agent-originated stage changes are now indexed into `workspace_lead_events`** (like relay-ingested status events already were), so they show up in workspace-scoped analytics and CRM activity windows. Previously `update-stage` wrote the status event to `events` only.
- **Mailbox bounce-rate `-1` sentinels** (provider "not measured") no longer render as -100%.
- **`sync --status` no longer derives lead/workspace pending counts from the deprecated `updated_at` cursor.** A fully-pushed lead whose `updated_at` happened to be newer than an earlier `last_sync` value was reported pending forever, even with an empty outbox — the classic false "half my data never synced" reading. `get_sync_status()` now reads `leads_pending`/`workspace_leads_pending` from the outbox, same as the already-correct `get_local_pending_counts()`.
- **Locally-deleted leads, workspace memberships, and companies now actually tell the relay.** Previously, only merges pushed a delete (via the separate `lead_merges`/`company_merges` path) — any other delete (a direct delete, a workspace removal) left an unconsumed `outbox` tombstone and a permanent ghost row on the relay. `lead_workspace` deletes (removing a lead from a workspace) had **no push path at all** — every workspace removal orphaned a `relay_lead_workspace_snapshots` row forever. `sync` now drains these tombstones and pushes real `lead_core_delete`/`lead_workspace_delete`/`company_core_delete` entries (the relay already had working handlers for all three; this was a pure client-side gap). Also fixes a wire-format bug found while building this: the delete trigger captures the bare `uid` column, not the `uid:`-prefixed form the relay actually stores as `entity_key` — sending the raw form would have matched zero rows and silently done nothing.

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
