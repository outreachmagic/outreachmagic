# Outreach Magic — Full Command Reference

Detailed procedures moved from SKILL.md for progressive disclosure. Load this file when you need in-depth guidance on a specific workflow.

## Import / Enrich (CSV, JSON)

### Core profile fields (column aliases — first non-empty wins)

| Canonical field | Aliases |
|----------------|---------|
| `email` | `lead_email`, `work_email` |
| `linkedin` | `linkedin_url`, `lead_linkedin_url`, `profile_url` |
| `name` | `full_name`, `display_name` (or `first_name` + `last_name`) |
| `title` | `job_title`, `role` |
| `company` | `company_name`, `organization`, `org` |
| `headcount` | `company_size`, `employees`, `employee_count` |
| `location_city` | `city`, `lead_city` |
| `location_state` | `state`, `region`, `lead_state` |
| `location_country` | `country`, `lead_country` |

### Extra CSV columns

| Column | Effect |
|--------|--------|
| `company_domain` | Stored in `companies` table, normalized (strips protocol/www/path) |
| `hq_city` / `hq_state` / `hq_country` | Company HQ location on `companies` table |
| `mailmerge_first_name` | Auto-populated as `first_name` in personalization table |
| `mailmerge_company_name` | Auto-populated as `company_name` in personalization table |
| `external_id` | CRM/list ID in `lead_identities` |
| `unified_lead_id`, `source_id` | Aliases → same as `external_id` |
| `lead_status` | Set on workspace_leads (requires `--workspace`) |
| `lead_sentiment` | Set on workspace_leads (requires `--workspace`) |
| `tags` | semicolon or comma separated, normalized lowercase (requires `--workspace`) |
| `contact_order` | Integer `contact_priority` on workspace_leads (requires `--workspace`) |
| `is_connected_linkedin` | Sets connected status (requires `--workspace` + `--sender-profile`) |
| `is_linkedin_request_pending` | Sets pending status (requires `--workspace` + `--sender-profile`) |

### Normalization rules

- **Tags:** lowercased, whitespace collapsed — `"VIP"` and `"vip"` are the same tag.
- **Status/sentiment:** lowercased, underscores to spaces — `"Not_Interested"` → `"not interested"`.
- **Headcount:** range string preserved + numeric midpoint computed (`"11-50"` → 30).
- **Location:** stored as-is (city/state/country text).
- **Attribution:** `original_source` (first touch immutable) and `latest_source` (updates each time). `--source` for machine-readable channel, `--source-detail` for list labels.

### Identity matching tiers (strongest first)

`external_id` → email → LinkedIn → phone → name+domain → name+company → `import_key` (name-only rows). Fills empty fields only; use `--overwrite` to replace existing.

### Email-finder batch save

```bash
python3 scripts/pipeline.py apply-email-find-results \
  --workspace your_workspace --source trykitt --source-detail "email-finder/batch" \
  --file import.json
```

Updates email, workspace tags, and provider verification in one pass. Requires `--workspace`. Run `sync` after when pending snapshots reported.

## Personalization (mail-merge)

| Raw field | Mail-merge field | Scope |
|-----------|------------------|-------|
| `name` | `first_name` | lead |
| `company` / `companies.name` | `company_name` | company |

```bash
# Lead
pipeline.py personalize-pending --fields first_name --json
pipeline.py personalize-set --lead-id 5 --field first_name --value "Jane"
pipeline.py personalize-set --lead-id 5 --field upcoming_event --value "SaaStr talk" --date 2026-09-10

# Company (org-wide)
pipeline.py company-personalize-pending --fields company_name,company_icebreaker --json
pipeline.py company-personalize-set --domain acme.com --field company_name --value "Acme"

# Read merged
pipeline.py personalize-get --lead-id 5 --json
```

Import: `mailmerge_first_name` → lead; `mailmerge_company_name`, `mailmerge_company_*` → company. Sync pushes separately; merge is local.

## Email verification tracking

```bash
# Record a verification result
pipeline.py verify-email --lead-id 5 --status valid --source zerobounce

# Batch verify from JSON
pipeline.py verify-email --batch --json '[{"lead_id":5,"status":"valid","source":"zerobounce"}]'

# Check verification status
pipeline.py verify-status --lead-id 5
pipeline.py verify-status --email j@acme.com

# List leads needing verification
pipeline.py verify-pending --limit 50 --json
```

**Status values:** `valid`, `invalid`, `catch-all`, `unknown`, `spamtrap`, `abuse`, `do_not_mail`, `risky`, `bounced`, `soft_bounce`.

**Bounce precedence:** Platform bounces → `source="platform_bounce"`. Hard bounces override soft. Tool verifications (ZeroBounce, etc.) override platform bounces. Consolidated status on `leads.email_verification_status`.

## Companies and unified lead identity

- **`companies` table** — canonical company name, domain, industry, headcount (text + numeric midpoint), HQ location (city, state, country). Leads link via `company_id`.
- **Match by email and/or LinkedIn** — a lead can have email only, LinkedIn only, or both.
- **Merge duplicates** — auto on ingest when both identifiers match two leads. Manual:

```bash
pipeline.py merge-leads --keep 12 --merge 34
pipeline.py merge-leads --email j@acme.com --linkedin linkedin.com/in/janedoe
```

## Dedup (batch)

```bash
pipeline.py dedup find --workspace popcam --tag campaign --output outreachmagic/exports/candidates.json
pipeline.py dedup merge --candidates outreachmagic/exports/candidates.json          # dry-run
pipeline.py dedup merge --candidates outreachmagic/exports/candidates.json --commit # apply
```

## Google Sheets export (lead review)

Requires `pipeline.py login`. Sheets created on `app.outreachmagic.io`.

```bash
pipeline.py sheets export --workspace popcam --title "NACE Leads"
pipeline.py review export --template lead-review --workspace popcam --tag nace --detail standard --title "NACE Leads"
```

## Dedup review (hosted Google Sheets)

```bash
pipeline.py review export --input outreachmagic/exports/candidates.json --title "Popcam Dedup"
pipeline.py review sync --sheet-id SHEET_ID --dry-run
pipeline.py review sync --sheet-id SHEET_ID --commit
```

## Lead review sheet (export → edit → sync)

Detail levels: `--detail basic|standard|full|custom`. Use `review presets` for current column catalog. Dropdowns: `workspace_stage`, `lead_sentiment`.

```bash
pipeline.py review presets --template lead-review
pipeline.py review export-payload --workspace popcam --tag nace --detail standard
pipeline.py review export --template lead-review --workspace popcam --tag nace --detail standard --title "NACE Review"
pipeline.py review sync --template lead-review --workspace popcam --sheet-id SHEET_ID --detail standard --dry-run
pipeline.py review sync --template lead-review --workspace popcam --sheet-id SHEET_ID --detail standard --commit
```

## Email-finder candidates

```bash
pipeline.py email-finder-candidates --workspace popcam --tag nace --no-email --require-domain --never-contacted
pipeline.py email-finder-candidates --workspace popcam --file outreachmagic/batches/find-batch.json
```

## Quarantine (multi-workspace)

Unmapped webhook events land in `unmapped_campaign_queue`. Resolve locally, then `sync`.

```bash
pipeline.py quarantine list [--json] [--status all]
pipeline.py quarantine skip --id QUEUE_ID
pipeline.py quarantine skip --campaign-platform-id CAMPAIGN_PLATFORM_ID
pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG
pipeline.py quarantine replay
pipeline.py sync
```

- **`skip`** — ignore junk/test events on the relay (permanent after sync).
- **`assign`** — route future pulls to a workspace (ingested on next pull, not immediately).
- **`replay`** — bulk re-ingest pending rows locally after adding campaign-map rules.

## PlusVibe webhook setup

Point PlusVibe webhooks at `…/plusvibe/{token}`. Enable all event types and category labels:

- `EMAIL_SENT`, `ALL_EMAIL_REPLIES`, `BOUNCED_EMAIL`
- `LEAD_MARKED_AS_INTERESTED`, `LEAD_MARKED_AS_NOT_INTERESTED`, `LEAD_MARKED_AS_OUT_OF_OFFICE`, `LEAD_MARKED_AS_AUTOMATIC_REPLY`
- `LEAD_MARKED_AS_MEETING_BOOKED`, `LEAD_MARKED_AS_MEETING_COMPLETED`, `LEAD_MARKED_AS_WRONG_PERSON`, `LEAD_MARKED_AS_CLOSED`

**Do not enable** `ALL_POSITIVE_REPLIES` or `FIRST_EMAIL_REPLIES`. Leave "Skip out of office replies" and "Skip autoreplies" **unchecked**.

OOO = auto-reply. Bounces set sentiment `invalid` but do NOT auto-move stage.

## EmailBison webhook setup

Point at `…/emailbison/{token}`. Enable all seven:

- `email.sent`, `email.bounced`, `lead.replied`, `lead.interested`, `lead.unsubscribed`, `tag.attached`, `tag.removed`

`lead.interested` → stage `interested`. `lead.replied` → stage `replied`. Bounce fields: `data.bounce.type`, `data.bounce.reason`, `data.lead.mx_provider`.

## Export full profiles

```bash
pipeline.py export --workspace acme_corp --tag nace --format csv
pipeline.py export --workspace acme_corp --since today --format json
```

Writes to `outreachmagic/exports/`. **Not for Google Sheets** — use `sheets export` or `review export`.

## Reset local database

```bash
pipeline.py refresh --yes    # syncs first, backs up, then rebuilds
pipeline.py tag repair --dry-run
pipeline.py tag repair
```

Manual (no pre-sync backup):
```bash
rm <skill_home>/databases/outreachmagic.db  # see pipeline.py paths
pipeline.py init
pipeline.py pull --full
```

`pull --full` does NOT clear `relay_ingested`. Use `refresh` for a true rebuild.

**LinkedIn IDs:** Public profiles stored as `linkedin.com/in/handle` (no `https://`). Sales Nav (`ACwAA…`) and member IDs (`urn:li:member:…`) are identity aliases.

## Relay sync progress legend

- **↓ pull / ↑ push** — cloud → local vs local → cloud.
- **Event, Lead, Workspace, Company** — four streams.
- **Pending banner:** `~` on counts/page estimate, e.g. `[02:10] ↓ Event : ~12,400 pending (~13p @ 1000/p)`.
- **Per page:** `[02:11] ↓ Event : p13/62 — 1,000 this page, 13,000/62,400 (20%)`.
- **`pull --probe`:** backlog only (limit=1/stream, no ingest).
- **`pull --skip-snapshots`:** webhook events only.
- **`pull --kind events`:** same as `--skip-snapshots` when you only need event cursor.
- **Push page:** `[03:56] ↑ Event : p2/13 — ok 7.9s, 5,000 this page (10,000/62,093 (16%))`.
- **`batch_sync.log`:** outer `batch 1/46` = lead-id walk; inner `pN/M` = HTTP pages.

### Relay sync limits

- **`sync` (upload):** ≥2500 pending → 5000 entries per `/push`; otherwise default 200, max 500.
- **`pull` (download):** 1000 rows/page for events and snapshots.

## Pull Troubleshooting Runbook

```bash
pipeline.py pull --diagnose
pipeline.py pull --full --diagnose
```

Diagnostic verdicts:
- `relay empty` — no events for current cursor window.
- `relay has events but deduped` — events already in `relay_ingested`.
- `cursor advanced` — `last_max_id` increased.
- `cursor stalled` — full page returned but cursor didn't advance; inspect relay pagination.

Pull uses id cursors: `last_max_id` (webhooks) + per-table snapshot cursors (`last_snapshot_core_after_id`, `last_snapshot_workspace_after_id`). No `since` on relay pull.

```bash
pipeline.py history --email "<lead_email>" --json    # inspect specific lead
```

## Agent-created changes (cross-platform sync)

```bash
pipeline.py agent-changes                              # JSON to stdout
pipeline.py agent-changes --file local_changes.csv     # CSV file
pipeline.py agent-changes --workspace leadgenph        # filter by workspace
pipeline.py agent-changes --all                        # include all leads
```

Push to relay: `pipeline.py sync` (pending lead snapshots: profile, `external_id`, company_domain, HQ, tags, mailmerge, workspace status, LinkedIn flags, local events, quarantine resolutions).

## Local database health

```bash
pipeline.py db-health
pipeline.py db-health --json
pipeline.py db-health --full
```

Cloud copy: `GET /api/agent/status` → `localDb` after user has run `sync`.

## Archive a workspace

```bash
pipeline.py archive --workspace acme_corp --dry-run
pipeline.py archive --workspace acme_corp --output ~/archives/acme_corp.db
pipeline.py archive --workspace acme_corp --output ~/archives/acme_corp.db --purge
```

### Fresh DB + full CSV round-trip

```bash
pipeline.py import-profiles --file nace.csv --workspace acme_corp --import-batch-id nace-2026
pipeline.py sync
# new machine:
pipeline.py init && pipeline.py pull --full
```

### File-based transfer (no server)

```bash
# Machine A: export
pipeline.py agent-changes --file changes.csv
# Machine B: import
pipeline.py import-profiles --file changes.csv --overwrite
```

## `sync --status` counters

- `recommended_mode`: `bulk` when pending ≥ 2500 (else `push`).
- `relay_untracked_leads`: imported/local leads with no relay pull history (normal after CSV).
- `pending_lead_snapshots`: rows with `updated_at` > last sync — run `sync`.
- `local_agent_events`: agent-originated events not yet on relay.
