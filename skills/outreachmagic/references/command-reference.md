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
| `personalized_first_name` | Stored as `first_name` in personalization table |
| `personalized_company_name` | Stored as `company_name` in personalization table |
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

Import: columns prefixed with `personalized_` (e.g. `personalized_first_name`) are stored as personalization fields. `personalized_company_name` / `personalized_company_*` → company; everything else → lead. Sync pushes separately; merge is local.

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

### Domain discovery (Serper)

Discover a domain + public emails for companies that don't have one yet, so `email_finder` has something to search against:

```bash
pipeline.py find-domains --workspace acme [--tag T ...] [--exclude-tag T ...] [--limit N] [--force] [--dry-run] [--max-queries N] [--debug]
```

**Targeting is workspace + tag scoped; every "already known?" check is org-wide.**
`--tag` selects companies with at least one lead carrying any of those workspace tags (that lead also becomes the one the observation is filed against). `--exclude-tag` drops a company if *any* of its leads carries the tag — a domain is a company-level fact, so a per-lead exclusion would be incoherent. Use it to keep unqualified segments out of a paid run:

```bash
pipeline.py find-domains --workspace acme --tag assisted_living --dry-run
pipeline.py find-domains --workspace acme --exclude-tag needs_review --max-queries 500
```

Tags narrow only what you pay to search. The freshness cache, the pre-flight identity/duplicate lookups, the `companies.domain` filter and `_attach_domain`'s clash guard all ignore workspace and tag, so a company resolved under one tag, workspace, or campaign is never searched again under another.

**Credit discipline** (this runs across thousands of companies; every query is billable):

- 1 Serper query per company by default (name + "email", unquoted, legal-suffix stripped). A 2nd, domain-targeted query fires only when a domain was found with no email on it. A 3rd (alt-domain) fires only when query 1 returned results that merely failed to name-match — a query that came back completely empty is a dead end, and stops at one credit.
- No query uses search operators (`site:` etc.), which free Serper accounts reject outright with an HTTP 400. A Serper failure is recorded as an `error` observation and the batch continues; one bad company costs one company.
- `--max-queries N` hard-caps the run and stops cleanly, reporting `stopped_reason: "query_budget_exhausted"` and `companies_remaining`.
- `--dry-run` lists target companies and `serper_queries_worst_case` without spending anything.
- Every run reports `serper_queries_spent`.
- Cached org-wide by `company_id` (30-day freshness), including negative results — a company already searched from any workspace is never re-queried; `--force` overrides. Companies whose names normalize identically are searched once per run.

**Results:**

- Prefers a domain with a real attached email over one that only looks like the company's website — a company can legitimately have more than one domain (website vs. email-sending vs. per-branch).
- Name matching handles franchise/branch names (`Amada Senior Care North Atlanta` → `amadaseniorcare.com`) via prefix/containment/acronym/trigram scoring against both the raw and normalized company name. Each candidate carries a `reason` explaining its score.
- A domain nothing matched by name is never promoted on ranking or a snippet mention alone — that is how directory sites used to win.
- Free-provider domains (`gmail.com`, `yahoo.com`, …) can never become the company domain. The addresses themselves are still kept.
- Discovered public emails land in `company_identities` as `identity_type='public_email'` with `role='corporate'|'free_provider'` and the source URL in `label`. They round-trip the relay and stay verifiable through the normal flow (`verify-email` against the company's rep lead).
- Domains write into `company_identities` (not a new column) — the same multi-domain store `rank_company_domains()` / `email_finder`'s domain fallback already read. `companies.domain` is backfilled only when it was `NULL`.
- Below a confidence floor the result is recorded and tagged but **not** attached — no `companies.domain`, no identity row, since nothing downstream corrects a wrong domain later. Status `low_confidence`.
- Tags workspace leads `domain_discovered` / `domain_low_confidence` / `domain_not_found`. Deliberately one flag, not `domain_found_<domain>`: embedding the value minted a tag per domain (288 of 340 tags in one workspace, most used by a single lead) and was a copy of `companies.domain` that nothing kept in step. Query the column for the value; tags mark the segment. A migration collapses existing per-domain tags on first run.
- Observations store a lean summary (~0.5 KB) — ranked candidates with scores and reasons, found emails, top 3 links. `--debug` adds the full raw Serper response (~5-8 KB), which also crosses the relay wire, so it is off by default.

## Email verification (MillionVerifier bulk)

```bash
pipeline.py verification-candidates --workspace acme [--tag T ...]      # who is due
email_finder.py verify-bulk --workspace acme --tag assisted_living --dry-run
email_finder.py verify-bulk --workspace acme --tag assisted_living --poll
```

`--tag` scopes to leads carrying any of those workspace tags (OR, deduped). MillionVerifier bills per email, so a campaign usually wants to verify the segment it is about to send to rather than every address in the workspace — on one real workspace that is 80 versus 339. Always `--dry-run` first: it reports credits required against credits remaining without submitting.

## Dedup (batch)

```bash
pipeline.py dedup find --workspace acme --tag campaign --output outreachmagic/exports/candidates.json
pipeline.py dedup merge --candidates outreachmagic/exports/candidates.json          # dry-run
pipeline.py dedup merge --candidates outreachmagic/exports/candidates.json --commit # apply
```

## Google Sheets export (lead review)

Requires `pipeline.py login`. Sheets created on `app.outreachmagic.io`.

```bash
pipeline.py sheets export --workspace acme --title "NACE Leads"
pipeline.py review export --template lead-review --workspace acme --tag nace --detail standard --title "NACE Leads"
```

## Dedup review (hosted Google Sheets)

```bash
pipeline.py review export --input outreachmagic/exports/candidates.json --title "Acme Dedup"
pipeline.py review sync --sheet-id SHEET_ID --dry-run
pipeline.py review sync --sheet-id SHEET_ID --commit
```

## Lead review sheet (export → edit → sync)

Detail levels: `--detail basic|standard|full|custom`. Use `review presets` for current column catalog. Dropdowns: `workspace_stage`, `lead_sentiment`.

```bash
pipeline.py review presets --template lead-review
pipeline.py review export-payload --workspace acme --tag nace --detail standard
pipeline.py review export --template lead-review --workspace acme --tag nace --detail standard --title "NACE Review"
pipeline.py review sync --template lead-review --workspace acme --sheet-id SHEET_ID --detail standard --dry-run
pipeline.py review sync --template lead-review --workspace acme --sheet-id SHEET_ID --detail standard --commit
```

## Email-finder candidates

```bash
pipeline.py email-finding-candidates --workspace acme --tag nace --no-email --require-domain --never-contacted
pipeline.py email-finding-candidates --workspace acme --file outreachmagic/batches/find-batch.json
```

## Campaign routing maps (multi-workspace)

Rules map campaigns to workspaces. A stale `name_exact` row (e.g. left by the
one-time single→multi backfill) can silently shadow a broader `rule_contains`
rule added later, because `name_exact` is checked first. Fix an affected DB in
three steps:

```bash
pipeline.py campaign-map conflicts                 # 1. see what's shadowed
pipeline.py campaign-map deactivate --id MAP_ID    # 2. clear the stale name_exact row
pipeline.py campaign-map reconcile --dry-run       # 3a. preview moving already-ingested data
pipeline.py campaign-map reconcile                 # 3b. move it for real
```

- **`conflicts`** — lists active `name_exact` rows shadowed by a broader active rule pointing at a different workspace.
- **`deactivate`** — soft-deactivates one map row by id (`is_active = 0`, kept for history).
- **`reconcile`** — re-applies current rules to already-ingested `workspace_leads`/`workspace_lead_events`. `--platform` is best-effort (via `leads.latest_source_platform`); events with no derivable campaign are reported as `skipped_no_campaign`. Run only after clearing shadowing rows, or resolution stays stale.

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

## Local dashboard

```bash
pipeline.py dashboard                       # serve http://127.0.0.1:8765 and open a browser
pipeline.py dashboard --port 9000           # different port
pipeline.py dashboard --no-browser          # don't auto-open a browser tab
```

A zero-dependency (stdlib-only) local web UI over the same SQLite database and
the same mutation functions the CLI uses. A global date range (presets or a
custom from/to) applies across views; tables are sortable (click headers) and
filterable. Per-workspace views:

- **Deliverability** — daily bounce-rate trend, per-mailbox health
  (warmup, SPF/DKIM/DMARC, health score, 7-day bounce trend flags), and
  sending domains derived from the workspace's mailboxes with DNSBL status;
  click a domain for reseller, pricing, notes, and its mailboxes.
- **Pipeline** — leads per stage with drill-down; click a lead for its full
  event timeline (previews + expandable full message bodies) and actions.
- **Contacts** — server-side lead search across name / email / email-domain /
  LinkedIn / company / title (whole range, paginated, sortable); row click
  opens the lead slide-over with edit, history, and a company-link control.
- **Companies** — company search + list (name, domain, branch count, industry,
  leads); click for attributes edit, all domains/branches, associated leads,
  and the pending company-merge review queue (approve / keep-separate).
- **Data quality** — under-enriched-lead buckets (missing email / company /
  title, unknown name, linkable) with counts. Select leads to **link** them to
  a company from their company text, **find emails** (company/multi-domain
  aware — previews no-domain/multi-domain before spending credits), or run
  **Serper research** as a background job (formatted results surface in the
  sync status for the agent to map to fields). Plus an org-wide **cleanup** of
  truly-empty (event-less, name-"unknown") leads: dry-run preview, then a
  confirmed delete.
- **Campaigns** — daily activity matrix (email sent/received, DM
  sent/received, connects sent/accepted, bounces, interested, not
  interested, meetings — filterable by campaign), sent / reply / positive /
  bounce rates per campaign, and a campaign click-through with sender
  accounts, activity span, lead stages, and subject lines with expandable
  full copy for auditing.
- **Attributes** — reply / positive-reply rate ranked by industry, title,
  headcount, country, or source platform; case-insensitive value grouping,
  minimum-sample threshold, filterable by campaign.
- **CRM** — synced / needs-update / eligible / error counts, synced-lead
  table with per-lead "Update", eligible-lead table with per-lead "Sync",
  bulk "Sync eligible now" (optional activity window), recent run log.
  Reuses `crm_sync.sync_workspace`; requires a `crm-sync setup` config.
- **Activity** — chronological workspace event feed, searched server-side
  across the whole date range: an event-type filter (distinct types + counts)
  plus text search over lead name / email / email-domain / LinkedIn / company /
  company-domain. Click through to leads.
- **Sync** — outbox audit: everything queued to push back to Outreach Magic.
  A group table (every entity_type/op with counts) plus a row list that is a
  LIMIT slice — click a group to drill the rows into that entity_type/op
  server-side; the row count states `showing N of M` so truncation is explicit.
  Rows show the tombstone `entity_key`. Click a row for a detail slide-over —
  its queued operations, the resolved live record, and the exact payload the
  push will send (rebuilt with the same `sync_contract` builders `sync_all`
  uses). Deletes are labeled tombstones: no content body is sent, just the key
  to remove. The summary splits **pushable** rows (`op='upsert'` — what **Push
  to relay** actually drains) from **delete tombstones** (`op='delete'` — local
  trigger artifacts the push never sends; real relay deletes travel via
  `lead_merges` / company merges). A large residual delete count after a
  successful push is expected, not a stuck push — the upserts drained and the
  tombstones stay.

Write actions (all routed through the same code paths as `update-stage`,
`log-event`, and `enrich`, so outbox triggers queue them for relay push):
change a lead's stage, edit lead attributes, log an event, trigger `pull`,
trigger `sync` and CRM sync (each asks for confirmation; the button click
counts as the user asking).

API endpoints live under `/api/*` (JSON; `?workspace=SLUG` on workspace-scoped
reads). POSTs require the `X-OM-Dashboard: 1` header.

**Limitations**

- **No authentication.** Binds `127.0.0.1` by default; the Host-header check
  and POST header are hardening, not auth. Do not bind `0.0.0.0` on shared
  networks.
- **No open/click/spam-complaint metrics** — the sequencer platforms do not
  deliver them to the local DB; deliverability is bounce + mailbox health +
  blacklist based.
- **Campaign-level copy insights only** (subject lines from stored events;
  there is no per-variant/step attribution).
- One dashboard-initiated sync at a time; a concurrently running CLI sync in
  another terminal is still possible (WAL + busy timeouts keep that safe).
- Data freshness = last `pull`; the header shows the latest event timestamp.
