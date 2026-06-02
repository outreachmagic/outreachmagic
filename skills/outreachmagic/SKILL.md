---
name: outreachmagic
description: >
  The outreach data layer for AI agents. Syncs events, replies, and lead
  attributes from Smartlead, Instantly, Heyreach, PlusVibe, and EmailBison
  into a local SQLite database your agent can query directly. Use for pipeline
  views, client briefings, deliverability diagnostics, campaign breakdowns,
  segment performance, and reply copy insights. Webhook payloads pass through
  api.outreachmagic.io; your data lives in a local SQLite file on your machine.
  Free tier: local tracking plus 1,000 relay events/mo. Pro: sequencer sync.
version: 1.23.5
author: Outreach Magic
license: MIT
platforms: [linux, macos]
metadata:
  cursor:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks, smartlead, instantly, sqlite, gtm]
    related_skills: [lead-enrich, email-finder]
    external_domains:
      - domain: api.outreachmagic.io
        purpose: Relay webhooks and authenticated event pull (payloads imported to local SQLite)
      - domain: app.outreachmagic.io
        purpose: Portal API for tokens, billing, and workspace routing config sync
  hermes:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks, smartlead, instantly, sqlite, gtm]
    category: productivity
    related_skills: [lead-enrich, email-finder]
    external_domains:
      - domain: api.outreachmagic.io
        purpose: Relay webhooks and authenticated event pull (payloads imported to local SQLite)
      - domain: app.outreachmagic.io
        purpose: Portal API for tokens, billing, and workspace routing config sync
---

# Outreach Magic — Pipeline Visibility

The outreach data layer for AI agents. Auto-logs outreach to a local SQLite database.
Free forever for local work. Connect Smartlead, Heyreach, Instantly via paid relay.

**Outreach Magic suite:** Pair with **lead-enrich** (Serper research + free dedup) and
**email-finder** (trykitt find). See [skill suite docs](https://github.com/outreachmagic/outreachmagic/blob/main/docs/skill-suite.md).

## CLI convention

All commands below use the pipeline CLI in this skill's `scripts/` directory (run from the skill root, or use absolute paths from `pipeline.py paths`):

```bash
python3 scripts/pipeline.py <command>
```

Resolve install paths anytime:

```bash
python3 scripts/pipeline.py paths
```

Optional config keys: `data_root` (share one DB across platforms), `api_base_url`, `dev_repo` for local development.

## Platform install

Install from [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh -o install.sh
bash install.sh --platform hermes --with-lead-enrich --with-email-finder --migrate
```

Pin a release (optional): add `--tag v1.21.5 --lead-enrich-tag v2.0.2 --email-finder-tag v1.0.2`.

Use `--platform cursor` or `--platform claude` for other agents. Setup: https://app.outreachmagic.io/setup/agent

### Hermes profiles

- **Real install:** `~/.hermes/skills/outreachmagic/` — never copy the full tree into `profiles/`
- **Profiles:** symlink only → `../../../skills/outreachmagic`
- **Verify:** `pipeline.py paths` (warns if a profile has a copy instead of a symlink)
- **Fix copies:** `install.sh --platform hermes --migrate --all-profiles`
- **Update:** `pipeline.py update` writes the global install; all profiles pick it up via symlink

### Cursor

Install to `~/.cursor/skills/outreachmagic/`. Invoke with `/outreachmagic` or ask about your pipeline in plain English.

### Claude Code

Install to `~/.claude/skills/outreachmagic/`. SKILL.md is the source of truth.

Environment variable: `OUTREACHMAGIC_AGENT_KEY` — overrides the config file `agent_key`. Set via `.env`, shell profile, or CI/CD.

## First-Time Setup (IMPORTANT — read this first)

On startup, **always check if the agent is already connected** by running:

```bash
python3 scripts/pipeline.py version
```

Then check whether an agent key exists in the config:

```bash
python3 scripts/pipeline.py pull
```

If `pull` returns an error like "No agent key or token configured", the user needs to set up.

**When setup is needed, tell the user exactly this:**

> Run this in your terminal (not in chat):
>
> `python3 scripts/pipeline.py login`
>
> A browser window will open — sign in or sign up, then authorize this device. Never paste secrets into chat.

If the skill is not installed yet, point them to **https://app.outreachmagic.io/setup/agent** or **https://app.outreachmagic.io/dashboard/agent** for install commands, then `login`.

`init` creates the database and project folders (`input/`, `export/`, `agent_resources/` under `<skill_home>/project` by default). Override with `"project_root"` in config.

If `pull` returns auth errors after a revoked key, tell them to run `login` again.

That's it. Don't list other commands, don't offer alternatives. Just: run `login` in terminal, done.

**When setup is already done** (pull succeeds or returns events), skip setup and go straight to showing data:

```bash
python3 scripts/pipeline.py pull
python3 scripts/pipeline.py show
```

## Network & privacy (Hermes / hub review)

- **Default:** All lead and pipeline data stays in local SQLite.
- **Inbound only:** `pull` imports webhook/agent events from `api.outreachmagic.io` (user- or cron-initiated).
- **Outbound upload:** Only when the user (or agent following user instruction) runs **`pipeline.py sync`**. Import and local edits never auto-upload.
- **Update check:** The CLI may query GitHub for a newer release tag (read-only, no lead data; at most once per hour). See [SECURITY.md](SECURITY.md).

## Version

**One version for the whole skill.** To see what is installed, always run:

```bash
python3 scripts/pipeline.py version
```

The `version:` line in this file is synced from `scripts/VERSION` on install/update. If unsure, use the command above.

**Updates are user-triggered.** The CLI may print an update notice (at most once per hour) when a newer GitHub **release** exists. It never downloads or replaces scripts automatically. Install updates with:

```bash
python3 scripts/pipeline.py update
# or
hermes skills update
```

Check without installing: `pipeline.py update --check`. Install a specific release: `pipeline.py update --tag v1.4.5`.

Install commands for each platform are in **Platform install** above. After install, run `python3 scripts/pipeline.py login`.

## When to Use

- You are about to send outreach (email, LinkedIn message, WhatsApp, etc.)
- You are researching a prospect and want to track them
- The user asks "show me my pipeline" or "how is outreach going"
- The user says "track this" followed by outreach details
- The user wants to connect a sequencer (paid — requires token)
- The user asks for campaign breakdowns or counts by campaign name
- The user asks for **workspace inventory** (counts by tag, LinkedIn connection accepted by sender)
- The user asks about connection status, webhook URLs, or platform health (`status`, `connections`)
- The user wants to add or remove a platform connection (`connect-platform`, `disconnect-platform`)

## Agent Behavior Rules (Important)

- For bulk enrichment (CSV, spreadsheet export, Apollo/Clay dump): use **`import-profiles`**, not repeated `add-lead`.
- Always run `pull` first before showing pipeline data, history, stats, or campaigns.
- After any pull, explicitly report the exact number of new records imported.
- When the user asks about version, read the frontmatter of SKILL.md (version line).
- When the user asks for message content, use the `history` command on the specific lead.
- For copy-performance analysis (full subject/body on positive leads + winner), use `copy-insights`.
- For campaign counts, use `pipeline.py campaigns` (or `stats`, which includes a campaign section). Do not write raw SQL.
- For **tag counts** or **LinkedIn connection counts by sender** in a workspace, use **`workspace summary --workspace <slug> --json`**. Do not use `export` or custom Python to aggregate.
- **Tags** (`workspace_lead_tags`) and **LinkedIn connection state** (`workspace_lead_linkedin_status`, `is_connected` per sender) are different. A tag like `*_connected` is not the same as `is_connected` on a sender profile.
- When the user asks about connections, webhook URLs, or platform health, use `status` or `connections`.
- When the user wants to connect a new platform, use `connect-platform --platform <id>`.
- If the user reports slowness, disk use, or pipeline oddities: run **`db-health`** first (local, no network). Explain `rulesTriggered` hints; suggest `archive --workspace <slug> --dry-run` when size rules fire.
- **Never run `sync` unless the user asked** to push to the cloud. `sync` also sends aggregate DB health (~1 KB, no lead content) unless `--no-health-report`.
- **Never run `archive --purge` without explicit user confirmation** after reviewing `--dry-run` counts.
- **NEVER use `python3 -c`, `sqlite3` directly, raw SQL, or any inline script to inspect or modify the database.** All database operations must go through `pipeline.py` commands. If a command errors, report the error verbatim and stop — do not attempt to debug by accessing the database directly.
- Before adding platforms or debugging vendor event types, run `python3 scripts/pipeline.py platform-map --json`.

## MANDATORY: Always Pull First

**Before showing pipeline activity (show, stats, campaigns, history), run `pull` first** — unless the user only wants **local inventory** (see below).

```bash
python3 scripts/pipeline.py pull
```

If routing sync times out but you only need relay events, use:

```bash
python3 scripts/pipeline.py pull --skip-routing-sync
```

This fetches the latest events from the relay, so the user always sees current data. The local DB may be stale. Never skip pull for activity/timeline queries. This applies across sessions: a new session's first pipeline query must pull.

**Pull progress:** The first page requests total pending count from the relay (`include_pending=1`). You may see `~1400 pending (2 pages @ 1000/page)` — the 1000 is the page size, not the total. Progress shows `records this page / total pending` until all pages import.

### Workspace inventory (local DB — pull optional)

**`workspace summary`** reads local SQLite only (fast, works offline). Use when the user asks for counts by tag or LinkedIn sender connection state. Optional `pull` first if they need freshly synced tags/connection imports.

```bash
python3 scripts/pipeline.py workspace summary --workspace <slug> --json
```

Example JSON keys: `lead_count`, `last_pull`, `tags` (`tag`, `lead_count`), `linkedin_senders` (`sender_slug`, `connected`, `pending`), `linkedin_connected_leads`.

Tag-only (same tag data as summary): `pipeline.py tag list --workspace <slug>`.

## Free Tier

- Unlimited agent-originated tracking and local pipeline queries
- CLI pipeline view + web dashboard
- Pipeline stages with auto-advancement
- **1,000 relay events/month** (webhook sync from sequencers)

## Pro Tier ($9/mo)

- **50,000 relay events/month** (cap — covers most teams)
- Smartlead, Heyreach, Instantly, PlusVibe, EmailBison sync
- Multi-platform unified pipeline

Local import, export, dedup checks (lead-enrich), and `verify-email` recording do **not** count toward relay limits.

Sign up at https://outreachmagic.io

## Quick Start

```bash
python3 scripts/pipeline.py version
python3 scripts/pipeline.py show
python3 scripts/pipeline.py history --id 1
python3 scripts/pipeline.py history --email j@acme.com
python3 scripts/pipeline.py stats
python3 scripts/pipeline.py campaigns
python3 scripts/pipeline.py platform-map --json
python3 scripts/pipeline.py workspace summary --workspace <slug> --json
python3 scripts/pipeline.py copy-insights --lead-status interested --json
python3 scripts/pipeline.py import-profiles --file leads.csv
python3 scripts/pipeline.py agent-changes
python3 scripts/pipeline.py agent-changes --file changes.csv
```

### Status and connection management

Dashboard-style status, connection management, and webhook URL generation — all from the CLI. These commands talk to the app API and do not require a local database.

```bash
# Dashboard overview: plan, usage, per-platform health, routing
python3 scripts/pipeline.py status

# List all connections with webhook URLs and 30-day event counts
python3 scripts/pipeline.py connections
python3 scripts/pipeline.py connections --json

# Generate a webhook URL for a new platform
python3 scripts/pipeline.py connect-platform --platform smartlead

# Remove a platform connection (webhook URL stops working)
python3 scripts/pipeline.py disconnect-platform --platform smartlead
python3 scripts/pipeline.py disconnect-platform --platform smartlead --yes
```

### Agent-created changes (cross-platform sync)

Show locally-created leads and events (not from relay) as JSON or CSV. Useful for transferring data between platforms (Cursor, Hermes, Claude Code).

```bash
# JSON to stdout (pipe to relay push or save to file)
python3 scripts/pipeline.py agent-changes

# CSV file (import-profiles compatible)
python3 scripts/pipeline.py agent-changes --file local_changes.csv

# Filter to a specific workspace
python3 scripts/pipeline.py agent-changes --workspace leadgenph

# Include all leads (not just locally-created)
python3 scripts/pipeline.py agent-changes --all
```

**Push to relay for cross-platform sync:**

```bash
python3 scripts/pipeline.py sync
```

`sync` pushes pending lead snapshots (profile, `external_id`, `company_domain`, HQ/location, tags, mailmerge, workspace status, LinkedIn connection flags) plus local events, and **quarantine resolutions** (`skip` / `assign`) to the relay. At the end of the same command it may POST aggregate local DB health to the portal (file size, row counts, top tables — throttled ~6h). Skip with `sync --no-health-report`. Other machines run `pull --full` after a DB reset to restore everything that was synced.

### Quarantine (multi-workspace)

Unmapped relay events land in `unmapped_campaign_queue`. Resolve them locally, then `sync` so other machines and `pull --full` stay consistent:

```bash
python3 scripts/pipeline.py quarantine list
python3 scripts/pipeline.py quarantine list --status all --json
python3 scripts/pipeline.py quarantine skip --id QUEUE_ID
python3 scripts/pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG
python3 scripts/pipeline.py sync
python3 scripts/pipeline.py pull
```

- **`skip`** — ignore junk/test events on the relay (permanent after sync).
- **`assign`** — route future pulls to a workspace (ingested on next `pull`, not immediately).
- **`replay`** — bulk re-ingest **pending** rows locally after adding `campaign-map` rules (no relay resolution).

Relay stores resolutions in D1 (`queue_resolutions`). The first event page of each `pull` requests them (`include_queue_resolutions=1`); later pages reuse the in-memory map.

**`sync --status` counters:** `relay_untracked_leads` = imported/local leads with no relay pull history (normal after CSV; data is still in the shared DB). `cloud_pending_leads` = rows waiting to push — run `sync`. `local_agent_events` = agent-originated events not yet on relay.

### Local database health

```bash
python3 scripts/pipeline.py db-health
python3 scripts/pipeline.py db-health --json
python3 scripts/pipeline.py db-health --full
```

Read `healthStatus`, `rulesTriggered` (each has a `hint`), `rowCounts`, and `tableBreakdown`. Cloud copy: `GET /api/agent/status` → `localDb` after user has run `sync`.

### Archive a workspace (local only)

```bash
python3 scripts/pipeline.py archive --workspace acme_corp --dry-run
python3 scripts/pipeline.py archive --workspace acme_corp --output ~/archives/acme_corp.db
python3 scripts/pipeline.py archive --workspace acme_corp --output ~/archives/acme_corp.db --purge
```

**Fresh DB + full CSV round-trip:**

```bash
pipeline.py import-profiles --file nace.csv --workspace acme_corp --import-batch-id nace-2026
pipeline.py sync
# new machine:
pipeline.py init && pipeline.py pull --full
```

**File-based transfer (no server):**

```bash
# Machine A: export
pipeline.py agent-changes --file changes.csv
# Machine B: import
pipeline.py import-profiles --file changes.csv --overwrite
```

### Workspace inventory

Counts by tag and LinkedIn connection accepted/pending per sender. **Local DB only** — no relay call; `last_pull` in output shows data freshness.

```bash
python3 scripts/pipeline.py workspace summary --workspace <slug> --json
python3 scripts/pipeline.py workspace summary --workspace <slug>
python3 scripts/pipeline.py tag list --workspace <slug>
```

### Campaign breakdown

Relay imports auto-populate campaign names from webhook payloads (Smartlead, PlusVibe, etc.).

```bash
python3 scripts/pipeline.py campaigns
python3 scripts/pipeline.py campaigns --json
```

`stats` also includes a campaign section. Use `campaigns` when the user only wants counts by campaign name.

### PlusVibe webhooks (status + sentiment)

Point PlusVibe webhooks at your relay URL (`…/plusvibe/{token}`). Subscribe separately to:

- **Reply events:** `ALL_EMAIL_REPLIES` (and optional `FIRST_EMAIL_REPLIES`, `ALL_POSITIVE_REPLIES`)
- **Label/status events:** `LEAD_MARKED_AS_INTERESTED`, `LEAD_MARKED_AS_NOT_INTERESTED`, `LEAD_MARKED_AS_OUT_OF_OFFICE`, plus any custom `LEAD_MARKED_AS_*` labels

Each webhook is stored as an event. **Interested / not interested / sentiment come from label webhooks**, not from reply webhooks alone. OOO is classified as **auto-reply** (metadata flag, query with `--auto-reply true`). Bounces set event sentiment `invalid` but **do not** auto-move the lead to stage `lost` (use `--sentiment invalid` to find them).

After `pull`, filter the pipeline by **current** status (latest status-bearing event per lead):

```bash
python3 scripts/pipeline.py show --sentiment positive
python3 scripts/pipeline.py show --sentiment invalid
python3 scripts/pipeline.py show --auto-reply true
python3 scripts/pipeline.py show --lead-status interested --json
```

Filter by date (created or updated on/after a date):

```bash
python3 scripts/pipeline.py show --since today
python3 scripts/pipeline.py show --since 2026-05-26 --json
python3 scripts/pipeline.py lead-table --workspace acme_corp --since today --json
```

Then open full timeline for any lead (all events, not just the status event):

```bash
python3 scripts/pipeline.py history --id 1
```

Native copy-performance analysis (full message bodies + best template):

```bash
python3 scripts/pipeline.py copy-insights --lead-status interested
python3 scripts/pipeline.py copy-insights --lead-status interested --json
```

`show --json` and `lead-table --json` include `personalization`, `tags`, and `latest_sender` when available.

### Export full profiles (CSV / JSON)

```bash
python3 scripts/pipeline.py export --workspace acme_corp --tag nace --format csv
python3 scripts/pipeline.py export --workspace acme_corp --since today --format json
```

Writes to `export/` by default. CSV uses `personalized_first_name`, `personalized_company_name`, plus lead fields, tags, HQ, and `latest_sender`.

### Reset local database (schema upgrade)

Prefer the guarded refresh command (syncs first, backs up, then rebuilds):

```bash
python3 scripts/pipeline.py refresh --yes
```

Preview tag fixes without writing:

```bash
python3 scripts/pipeline.py tag repair --dry-run
python3 scripts/pipeline.py tag repair
```

Manual equivalent (no pre-sync backup):

```bash
rm <skill_home>/databases/outreachmagic.db  # see pipeline.py paths
python3 scripts/pipeline.py init
python3 scripts/pipeline.py pull --full
```

**Tell your agent (rare):** “Run `pipeline.py refresh --yes` to back up, sync local changes to the relay, wipe the local DB, and re-import from the cloud. Do not use `pull --full` alone — it skips already-imported rows.”

`pull --full` only re-downloads relay pages; it does **not** clear `relay_ingested`. Use `refresh` when you need a true rebuild.

**LinkedIn IDs (v1.17):** Public profiles are stored in `linkedin_url` as `linkedin.com/in/handle` (no `https://`). Sales Nav (`ACwAA…`) and member IDs (`urn:li:member:…`) are stored as identity aliases and used for matching when the public slug arrives later.

## Core Workflow

### View a lead's full timeline

```bash
python3 scripts/pipeline.py history --id 1
python3 scripts/pipeline.py history --email jane@acme.com
python3 scripts/pipeline.py history --name "Jane Doe"
python3 scripts/pipeline.py history --id 1 --json
```

Outputs lead info + numbered event timeline with direction arrows (← inbound, → outbound),
human-readable timestamps, and event details.

### Add leads when researching prospects

```bash
python3 scripts/pipeline.py add-lead \
  --name "Jane Doe" --company "Acme Corp" --title "VP Marketing" \
  --industry "Martech" --headcount "50-200" \
  --email "jane@acme.com" \
  --channel email --stage prospecting
```

To also associate the lead with a workspace at creation time:

```bash
python3 scripts/pipeline.py add-lead \
  --name "Jane Doe" --email "jane@acme.com" --company "Acme Corp" \
  --workspace thesystemsmethod --stage contacted
```

`--workspace` is optional on `add-lead` — creating a lead is an org-wide operation. Use it when you know which workspace the lead belongs to; omit it when just researching.

If lead exists by email, LinkedIn, or (when both are missing) case-insensitive `name+company`, returns `{"status": "exists", "id": N}`.

### Bulk import / enrich (CSV, JSON, research dumps)

**Use `import-profiles` for spreadsheets, enriched exports, or batched research** — not repeated `add-lead` calls. Matching uses **tiered identities** (strongest first): `external_id` → email → LinkedIn → phone → name+domain → name+company → `import_key` (name-only rows). CSV columns `unified_lead_id` / `source_id` are accepted as aliases and stored as `external_id`. Fills empty fields only (same as relay/PlusVibe); use `--overwrite` to replace existing values.

```bash
python3 scripts/pipeline.py import-profiles \
  --file input/contacts_enriched.csv

python3 scripts/pipeline.py import-profiles \
  --file leads.json

python3 scripts/pipeline.py import-profiles \
  --json '[{"email":"j@acme.com","name":"Jane","job_title":"VP Marketing","industry":"Martech","headcount":"11-50","company":"Acme"}]'

python3 scripts/pipeline.py import-profiles \
  --file contacts.csv --dry-run

# With workspace association, tags, and LinkedIn status tracking
python3 scripts/pipeline.py import-profiles \
  --file contacts.csv --workspace default --sender-profile "https://linkedin.com/in/myprofile" \
  --source-detail "Q2 Apollo list" --import-batch-id "nace-2026-05"

# Rows with only name + company_domain + unified_lead_id (no email/LinkedIn)
python3 scripts/pipeline.py import-profiles \
  --file nace.csv --workspace acme_corp --import-batch-id nace-2026-05
```

**Core profile fields** (column aliases — first non-empty wins):

| Canonical field | Aliases | Required |
|---|---|---|
| `email` | `lead_email`, `work_email` | No (see identity tiers below) |
| `linkedin` | `linkedin_url`, `lead_linkedin_url`, `profile_url` | No |
| `name` | `full_name`, `display_name` (or `first_name` + `last_name`) | No |
| `title` | `job_title`, `role` | No |
| `company` | `company_name`, `organization`, `org` | No |
| `industry` | — | No |
| `headcount` | `company_size`, `employees`, `employee_count` | No |
| `location_city` | `city`, `lead_city` | No |
| `location_state` | `state`, `region`, `lead_state` | No |
| `location_country` | `country`, `lead_country` | No |

**Headcount normalization:** `headcount` is stored as-is (text) plus a computed `headcount_numeric` (integer midpoint). Ranges like `"11-50"` become `30`, `"500+"` becomes `500`, exact numbers pass through. Both leads and companies get the numeric column for sorting/filtering (`WHERE headcount_numeric BETWEEN 10 AND 100`).

**Extra fields** (auto-detected from CSV columns):

| Column | Effect |
|---|---|
| `company_domain` | Stored in `companies` table, normalized (strips protocol/www/path) |
| `hq_city` / `hq_state` / `hq_country` | Company HQ location, stored on `companies` table |
| `mailmerge_first_name` | Auto-populated as `first_name` in personalization table |
| `mailmerge_company_name` | Auto-populated as `company_name` in personalization table |
| `import_name` / `list_source` | Attribution + namespace for `external_id` when value has no `:` |
| `external_id` | CRM/list ID in `lead_identities` (namespaced `list_source:id` if bare) |
| `unified_lead_id`, `source_id` | Import aliases → same as `external_id` |
| `import_batch_id` (CLI flag) | Stable dedupe for name-only rows via `import_key` within a batch |
| `lead_status` | Requires `--workspace`; normalized (lowercase, spaces) and set on workspace_leads |
| `lead_sentiment` | Requires `--workspace`; normalized (lowercase) and set on workspace_leads |
| `tags` | Requires `--workspace`; semicolon or comma separated, normalized (lowercase), stored in `workspace_lead_tags` |
| `contact_order` | Requires `--workspace`; integer priority stored as `contact_priority` on workspace_leads |
| `is_connected_linkedin` | Requires `--workspace` + `--sender-profile`; `true`/`1`/`yes` sets connected status |
| `is_linkedin_request_pending` | Requires `--workspace` + `--sender-profile`; `true`/`1`/`yes` sets pending status |

**Normalization rules:**
- **Tags:** lowercased, whitespace collapsed — `"VIP"` and `"vip"` are the same tag
- **Status/sentiment:** lowercased, underscores to spaces — `"Not_Interested"` becomes `"not interested"`
- **Headcount:** range string preserved + numeric midpoint computed (`"11-50"` → 30)
- **Location:** stored as-is (city/state/country text)

**Attribution** is automatic: every import sets `original_source` (immutable first touch) and `latest_source` (updates each time) on the lead, following the Salesforce/HubSpot model. The `--source-detail` flag or `import_name`/`list_source` columns provide the detail.

### Personalization (mail-merge)

**Lead fields** (`first_name`, contact-specific lines): per lead. **Company fields** (`company_name`, `company_*`): org-wide, one write per account.

| Raw field | Mail-merge field | Scope |
|-----------|------------------|-------|
| `name` | `first_name` | lead |
| `company` / `companies.name` | `company_name` | company |

```bash
# Lead
python3 .../pipeline.py personalize-pending --fields first_name --json
python3 .../pipeline.py personalize-set --lead-id 5 --field first_name --value "Jane"
python3 .../pipeline.py personalize-set --lead-id 5 --field upcoming_event --value "SaaStr talk" --date 2026-09-10

# Company (org-wide)
python3 .../pipeline.py company-personalize-pending --fields company_name,company_icebreaker --json
python3 .../pipeline.py company-personalize-set --domain acme.com --field company_name --value "Acme"
python3 .../pipeline.py company-personalize-set --domain acme.com --field company_icebreaker --value "..."

# Read merged (export uses same shape: personalized_* columns)
python3 .../pipeline.py personalize-get --lead-id 5 --json
```

Import: `mailmerge_first_name` → lead; `mailmerge_company_name`, `mailmerge_company_*` → company. Sync pushes lead and company snapshots separately; merge is local.

### Email verification tracking (org-wide)

Record verification results from tools like ZeroBounce, NeverBounce, etc. Results are org-wide (not workspace-scoped). Platform bounces from Smartlead, Instantly, etc. are auto-recorded during relay sync.

```bash
# Record a verification result
python3 scripts/pipeline.py verify-email \
  --lead-id 5 --status valid --source zerobounce

# Batch verify from JSON
python3 scripts/pipeline.py verify-email --batch \
  --json '[{"lead_id":5,"status":"valid","source":"zerobounce"}]'

# Check verification status for a lead
python3 scripts/pipeline.py verify-status --lead-id 5
python3 scripts/pipeline.py verify-status --email j@acme.com

# List leads needing verification
python3 scripts/pipeline.py verify-pending --limit 50 --json
```

**Verification status values:** `valid`, `invalid`, `catch-all`, `unknown`, `spamtrap`, `abuse`, `do_not_mail`, `risky`, `bounced`, `soft_bounce`

**Bounce handling:** Platform bounces (from relay sync) are auto-recorded in `lead_email_verification` with `source="platform_bounce"`. Hard bounces override soft bounces. Tool verifications (ZeroBounce, etc.) take precedence over platform bounces — a tool "valid" result is only overridden by a hard bounce that came after the verification. The consolidated status is materialized on `leads.email_verification_status` for fast filtering.

### Companies and unified lead identity

- **`companies` table** — canonical company name, domain, industry, headcount (text + numeric midpoint), HQ location (city, state, country). Leads link via `company_id` (business email domain or company name on ingest).
- **Match by email and/or LinkedIn** — a lead can have email only, LinkedIn only, or both. Relay ingest resolves identity from webhook payload + envelope `lead` field.
- **Merge duplicates** when email and LinkedIn history were separate rows:
  - **Auto:** ingest with both identifiers matching two leads merges them (keeps row with more events).
  - **Manual:**

```bash
python3 scripts/pipeline.py merge-leads --keep 12 --merge 34
python3 scripts/pipeline.py merge-leads \
  --email j@acme.com --linkedin linkedin.com/in/janedoe
```

```bash
python3 scripts/pipeline.py history --linkedin linkedin.com/in/janedoe
```

After `pull`, use **`campaigns`** for per-campaign event and lead counts (unchanged).

### Log every outreach send

```bash
python3 scripts/pipeline.py log-event \
  --lead-id 1 --type email_sent --direction outbound \
  --subject "Quick intro" --workspace thesystemsmethod
```

`--workspace` is **required** in multi-workspace mode. Outreach events are workspace-scoped — they belong to a specific pipeline. In single-workspace mode it falls back to the default workspace.

### Update stage and log replies

```bash
python3 scripts/pipeline.py update-stage \
  --id 1 --stage replied --next-action "Send case study" --workspace thesystemsmethod
```

`--workspace` is **required** in multi-workspace mode. Stage is per-workspace — a lead can be "contacted" in one workspace and "interested" in another.

Stages: `prospecting` -> `contacted` -> `replied` -> `interested` -> `proposal` -> `won` | `lost`

### Connect sequencers (paid)

If the user already has a key, skip the browser flow:

```bash
python3 scripts/pipeline.py login
```

Generate webhook URLs for platforms directly from the CLI (requires agent key):

```bash
python3 scripts/pipeline.py connect-platform --platform smartlead
python3 scripts/pipeline.py connect-platform --platform instantly
python3 scripts/pipeline.py connections
```

### Update skill scripts

```bash
python3 scripts/pipeline.py update
```

## Lead Fields Reference

| Field | CLI flag | Notes |
|-------|----------|-------|
| name | `--name` | Required |
| company | `--company` | |
| title | `--title` | Job title |
| industry | `--industry` | e.g. Martech, Fintech, Healthcare |
| headcount | `--headcount` | Size band, e.g. 1-10, 50-200, 1000+ |
| email | `--email` | Dedup key — unique per lead |
| linkedin | `--linkedin` | LinkedIn profile URL |
| channel | `--channel` | email, linkedin, whatsapp (default: email) |
| stage | `--stage` | Pipeline stage (default: prospecting) |
| notes | `--notes` | Free-form |
| tags | `--tags` | JSON array string like '["vip","enterprise"]' |
| workspace | `--workspace` | Optional on `add-lead`; required on `log-event` and `update-stage` in multi-workspace mode |

## Privacy & Security

- **Local-first data.** Pipeline leads, events, and campaign stats live in local SQLite (`pipeline.py paths` → `database`).
- **Relay pass-through.** Webhooks hit `api.outreachmagic.io`; the CLI imports them locally via `pull`. We store tokens and usage on our side, not a searchable cloud copy of your outreach archive.
- **Portal API.** `app.outreachmagic.io` handles tokens, billing, and optional workspace routing sync when connected.
- **Credentials.** Store relay tokens in `config/outreachmagic_config.json` only. Never hardcode tokens in SKILL.md or commit them to git.
- **Read before connect.** See [SECURITY.md](https://github.com/outreachmagic/outreachmagic/blob/main/SECURITY.md) for full data boundaries and vulnerability reporting.

## Common Pitfalls

1. **Always pull before show when checking "latest activity."**
2. Forgetting add-lead before log-event
3. Not updating stage after reply
4. Setup/auth errors (including 401 Unauthorized) should run `python3 scripts/pipeline.py login` in terminal.
5. **Version:** run `pipeline.py version` — do not guess from SKILL.md frontmatter alone.
6. Relay archive stays on api.outreachmagic.io; `pull` dedupes locally. Use `refresh --yes` for a true rebuild (sync + backup + wipe + `pull --full`). `pull --full` alone only helps after deleting the DB manually.
7. **Tags:** always pass plain names (`nace`, `vip`) — not JSON list strings like `['nace']`. Run `tag repair` if legacy rows used bracket form.
8. **`add-lead` on an existing email does not enrich** — use `import-profiles` or rely on relay `pull` for fill-if-empty updates.

## Pull Troubleshooting Runbook

When relay flow appears stale, diagnose before using destructive reset commands:

```bash
python3 scripts/pipeline.py pull --diagnose
python3 scripts/pipeline.py pull --full --diagnose
```

Diagnostic verdicts:
- `relay empty` — no events returned for the current cursor window.
- `relay has events but deduped` — relay returned events already recorded in local `relay_ingested`.
- `cursor advanced` — event cursor moved forward (`last_max_id` increased).
- `cursor stalled` — relay returned a full page but cursor did not advance; inspect relay pagination.
- Pull uses id cursors only: `last_max_id` (webhooks) and per-table snapshot cursors (`last_snapshot_core_after_id`, `last_snapshot_workspace_after_id`, `last_snapshot_company_after_id`). No `since` on relay pull.

If events were ingested but still seem missing, inspect a specific lead timeline:

```bash
python3 scripts/pipeline.py history --email "<lead_email>" --json
```
