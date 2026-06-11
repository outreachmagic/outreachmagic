---
name: outreachmagic
description: >
  The outreach data layer for AI agents. Syncs events, replies, and lead
  attributes from Smartlead, Instantly, Heyreach, PlusVibe, and EmailBison
  into a local SQLite database your agent can query directly. Use for pipeline
  views, client briefings, deliverability diagnostics, campaign breakdowns,
  segment performance, and reply copy insights. Webhook payloads pass through
  api.outreachmagic.io; your data lives in a local SQLite file on your machine.
  Free tier: local tracking plus 1,000 relay events/mo. Pro: 50k/mo. Agency: 250k/mo.
version: 1.33.0
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

Pin a release (optional): add `--tag vX.Y.Z --lead-enrich-tag lead-enrich-vA.B.C --email-finder-tag email-finder-vA.B.C`.

Use `--platform cursor` or `--platform claude` for other agents. Setup: https://app.outreachmagic.io/onboarding

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

**When setup is needed**, run `pipeline.py login` — a browser opens on the user's machine for sign-in. Tell the user: *"I'm opening Outreach Magic sign-in — come back when you're done and I'll continue."* Never paste secrets into chat.

If the skill is not installed yet, point them to **https://app.outreachmagic.io/onboarding** or **https://app.outreachmagic.io/agent**, then connect.

**Account pending approval:** New signups may wait for manual approval. Use `pipeline.py status --json` (`approval_pending`) or `pipeline.py login --wait-approval` to poll. Once approved, run `login` again.

## Common workflows (plain English)

| User says | You do |
|-----------|--------|
| "Show my pipeline" | `pull` → `show` |
| "Import my Sales Nav / Vayne CSV" | `import-profiles --file … --workspace W --dry-run` first, then import (auto-syncs) |
| "Find emails for these leads" | import if needed → `email-finder-candidates` → `batch-find --workspace W --yes` |
| "Export to Google Sheets" | `whoami --json` → `share_email`, then `sheets export --workspace W --share-email …` |
| "Connect Smartlead / Instantly" | `connections create --platform …` and share webhook URL |

`pipeline.py whoami --json` returns account email, org, and plan.

`init` creates the database under `<skill_home>/databases/`. Dashboard API keys sync to `<skill_home>/config/agent_secrets.env` (next to `outreachmagic_config.json`). CSVs and exports use **`input/`** and **`export/`** relative to your **workspace directory** (where the agent runs commands). Set `"project_root"` in config to pin a fixed folder instead of cwd.

If `pull` returns auth errors after a revoked key, ask Outreach Magic to log in again.

That's it. Don't list other commands, don't offer alternatives. Just connect via login, done.

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
- **Reads (analytics, counts, time windows):** use **`pipeline.py query`** presets first — **one query, max two attempts**. See [references/query-guide.md](references/query-guide.md). Do not tour `workspace_lead_events` when the question is relay volume (use **`events`**).
- **Writes:** only `pipeline.py` mutation commands (`add-lead`, `import-profiles`, `log-event`, `sync`, personalize, tags, etc.). Never `INSERT`/`UPDATE`/`DELETE` via ad-hoc scripts.
- After any `pull`, explicitly report the exact number of new records imported.
- When the user asks about version, run **`pipeline.py version`** (authoritative).
- When the user asks for message content, use `history` on the specific lead.
- For copy-performance analysis (full subject/body on positive leads + winner), use `copy-insights`.
- For **all-time** campaign totals (no time window), use `campaigns` or `stats`. For **last N hours/days by campaign**, use **`query engagement`** or **`query replies`** — not `campaigns`.
- For **tag counts** or **LinkedIn connection counts by sender**, use **`workspace summary --workspace <slug> --json`**. On large workspaces (>2,000 leads), add **`--tags-only`** for faster tag rollups. Do not use `export` or custom Python to aggregate.
- **Tags** (`workspace_lead_tags`) and **LinkedIn connection state** (`workspace_lead_linkedin_status`, `is_connected` per sender) are different.
- When the user asks about connections, webhook URLs, or platform health, use `status` or `connections`.
- When the user wants to connect a new platform, use `connect-platform --platform <id>`.
- If the user reports slowness, disk use, or pipeline oddities: run **`db-health`** first (local, no network).
- **Never run `sync` unless the user asked.** **Never run `archive --purge`** without explicit confirmation after `--dry-run`.
- Before adding platforms or debugging vendor event types, run `pipeline.py platform-map --json`.
- **Answer format for analytics:** (1) human table, (2) preset name or SQL used, (3) freshness note (local DB; offer `pull` if they need latest relay data).

### Which table? (read path)

| User intent | Use |
|-------------|-----|
| Replies / engagement / campaign breakdowns (time window) | **`query engagement`** or **`events` + `campaigns`** |
| Lead list with stage/sentiment | `show` / `lead-table` |
| Tag counts, LinkedIn connected-by-sender | `workspace summary --json` |
| Message bodies / copy winners | `history`, `copy-insights` |
| Workspace ingest audit (rare) | `workspace_lead_events` — **not** for volume analytics |

`workspace_lead_events` is a subset; full relay timelines are in **`events`**.

### Fast analytics (preferred)

```bash
python3 scripts/pipeline.py query engagement --workspace <slug> --since 48h --json
python3 scripts/pipeline.py query replies --workspace <slug> --since 7d --json
python3 scripts/pipeline.py query interested --workspace <slug> --since 48h --json
```

Advanced: `pipeline.py query --sql 'SELECT …' --params '["popcam |%"]' --json` (read-only).

Read commands print **data freshness** on stderr (and `last_pull` / `stale_minutes` in `--json`). If data is a few minutes old, tell the user before running `pull` unless they asked for up-to-the-minute.

| User intent | Command |
|-------------|---------|
| Reply/engagement counts in a time window | `query replies` / `query engagement --since … --json` |
| Lead rows / pipeline detail | `show` (use `--limit`; avoid `--json` unless needed) |
| All-time totals | `stats` / `campaigns --json` |
| Fresh webhook events | `pull` or `pull --kind events` |

## Pull policy (when to refresh)

**Do not run `pull` before local time-window analytics** (`last 48h`, `since today`, engagement by campaign) unless the user asks for latest/refresh or you need relay catch-up.

**Run `pull` first** when showing live pipeline activity the user expects to be current (`show`, `history` for “what just happened”), or when they say sync/refresh/latest.

```bash
python3 scripts/pipeline.py pull
python3 scripts/pipeline.py pull --if-stale 5m   # skip when last_pull is within 5 minutes
python3 scripts/pipeline.py pull --force         # always network (ignore --if-stale)
```

If routing sync times out but you only need relay events, use:

```bash
python3 scripts/pipeline.py pull --skip-routing-sync
```

This fetches the latest events from the relay. Skip for offline/local analytics; use for fresh activity timelines.

**Relay sync progress (stdout):** When interpreting `pull` / `sync` output or `export/batch_sync.log`, use the log legend in [docs/relay-sync-progress.md](../../docs/relay-sync-progress.md). Short version:

- **↓ pull / ↑ push** — cloud → local vs local → cloud
- **Event, Lead, Workspace, Company** — four streams (Lead = org lead core; Workspace = per-workspace lead overlay, not routing config)
- **Pending banner:** `~` on counts/page estimate once per stream start, e.g. `[02:10] ↓ Event : ~12,400 pending (~13p @ 1000/p) ...`
- **Per page:** `[02:11] ↓ Event : p13/62 — 1,000 this page, 13,000/62,400 (20%) ...` (pull, no `ok`)
- **`pull --probe`:** backlog only (`limit=1`/stream, no ingest) — use before a large catch-up pull
- **`pull --skip-snapshots`:** webhook events only (recommended after events catch-up; avoids slow snapshot phase)
- **`pull --reset-snapshot-cursors`:** zero snapshot cursors before pull (after DB delete or hung pull left config ahead of local leads)
- **`pull --kind events`:** same as `--skip-snapshots` when you only need the event cursor advanced
- **Push page:** `[03:56] ↑ Event : p2/13 — ok 7.9s, 5,000 this page (10,000/62,093 (16%))` then `done — N in Mp (Xs)`
- **`batch_sync.log`:** outer `batch 1/46` = lead-id walk; inner `pN/M` = HTTP pages inside one `sync`. Do not confuse them.

**Relay sync limits:** Same endpoints always — `POST /push` and `GET /pull`. No separate bulk URLs.

- **`sync` (upload):** When local `cloud_pending` snapshots ≥ **2500**, uses **5000 entries per `/push`**; otherwise routine batch size (default 200, max 500 per request).
- **`pull` (download):** **1000 rows/page** for events and all snapshots (D1 + local ingest). Progress shows `p2/118` and `% remaining` after a one-time pending probe per snapshot kind. Use `pull --kind events` if snapshots are already synced.
- Filter downloaded data locally (`show --since`, workspace queries) — the relay does not filter by date or workspace.

### Workspace inventory (local DB — pull optional)

**`workspace summary`** reads local SQLite only (fast, works offline). Use when the user asks for counts by tag or LinkedIn sender connection state. Optional `pull` first if they need freshly synced tags/connection imports.

```bash
python3 scripts/pipeline.py workspace summary --workspace <slug> --json
python3 scripts/pipeline.py workspace summary --workspace <slug> --json --tags-only
```

Example JSON keys: `lead_count`, `last_pull`, `tags` (`tag`, `lead_count`), `linkedin_senders` (`sender_slug`, `connected`, `pending`), `linkedin_connected_leads`. With `--tags-only`, LinkedIn keys are empty arrays/zero.

Companion attempt tags: **`serper_attempted`** (lead-enrich), **`trykitt_attempted`** / **`icypeas_attempted`** / **`email_found`** (email-finder), **`mv_attempted`** (MillionVerifier bulk — verification status still in `email_verification_status`).

Tag-only (same tag data as summary): `pipeline.py tag list --workspace <slug>`.

## Free Tier

- Unlimited local tracking, enrichment, queries, and exports
- **1,000 relay events / period** (sequencer webhooks + cloud sync)
- 1 sequencer connection, single workspace

## Pro Tier ($9/mo)

- **50,000 relay events / month**
- All sequencers, multi-workspace routing

## Agency Tier ($29/mo)

- **250,000 relay events / month**
- All sequencers, unlimited workspaces, priority support

### What counts

Only **relay-synced events**: sequencer webhooks and `pipeline.py sync` uploads to the cloud (**one event per sync batch**, not per lead). Local tracking (`log-event`, `add-lead`), enrichment dedup, email finding, queries, and exports do **not** count.

### Over limit

Sequencer webhooks are always accepted. Over-quota events are **buffered** and delivered on your next `pull` or when the quota resets. Events are only rejected if the **buffer cap** is reached. Check status with `pipeline.py status`.

Sign up at https://outreachmagic.io

## Quick Start

```bash
python3 scripts/pipeline.py version
python3 scripts/pipeline.py query engagement --workspace <slug> --since 48h --json
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

`sync` pushes pending lead snapshots (profile, `external_id`, `company_domain`, HQ/location, tags, mailmerge, workspace status, LinkedIn connection flags) plus local events, and **quarantine resolutions** (`skip` / `assign`) to the relay. Large backlogs use **5000 entries per `/push`** automatically (see relay sync limits above). At the end of the same command it may POST aggregate local DB health to the portal (file size, row counts, top tables — throttled ~6h). Skip with `sync --no-health-report`. Other machines run `pull --full` after a DB reset to restore everything that was synced.

### Quarantine (multi-workspace)

Unmapped relay events land in `unmapped_campaign_queue`. Resolve locally, then `sync` so other machines and `pull --full` stay consistent.

**No campaign id/name:** New relay events are quarantined automatically. Legacy rows in `events` with `campaign_id IS NULL` are backfilled on `init` into quarantine and **auto-skipped** (no workspace mapping possible). Use `history` to inspect event details if needed. Manual: `quarantine backfill-no-campaign`.

```bash
python3 scripts/pipeline.py quarantine list [--json] [--status all]
python3 scripts/pipeline.py quarantine skip --id QUEUE_ID
python3 scripts/pipeline.py quarantine skip --campaign-id CAMPAIGN_ID
python3 scripts/pipeline.py quarantine skip --reason REASON
python3 scripts/pipeline.py quarantine assign --id QUEUE_ID --workspace WORKSPACE_SLUG
python3 scripts/pipeline.py quarantine replay
python3 scripts/pipeline.py sync
```

- **`skip`** — ignore junk/test events on the relay (permanent after sync).
- **`assign`** — route future pulls to a workspace (ingested on next `pull`, not immediately).
- **`replay`** — bulk re-ingest **pending** rows locally after adding `campaign-map` rules (no relay resolution).

Relay stores resolutions in D1 (`queue_resolutions`). The first event page of each `pull` requests them (`include_queue_resolutions=1`); later pages reuse the in-memory map.

**`sync --status` counters:** `recommended_mode` is `bulk` when `cloud_pending_leads` ≥ 2500 (else `push`). `relay_untracked_leads` = imported/local leads with no relay pull history (normal after CSV; data is still in the shared DB). `cloud_pending_leads` = rows waiting to push — run `sync`. `local_agent_events` = agent-originated events not yet on relay.

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

Point PlusVibe webhooks at your relay URL (`…/plusvibe/{token}`). Select **all** event types and category labels in PlusVibe — including any custom categories in the user’s instance. Standard ones to verify:

- `EMAIL_SENT`, `ALL_EMAIL_REPLIES`, `BOUNCED_EMAIL`
- `LEAD_MARKED_AS_INTERESTED`, `LEAD_MARKED_AS_NOT_INTERESTED`, `LEAD_MARKED_AS_OUT_OF_OFFICE`, `LEAD_MARKED_AS_AUTOMATIC_REPLY`
- `LEAD_MARKED_AS_MEETING_BOOKED`, `LEAD_MARKED_AS_MEETING_COMPLETED`, `LEAD_MARKED_AS_WRONG_PERSON`, `LEAD_MARKED_AS_CLOSED`

**Do not enable** `ALL_POSITIVE_REPLIES` (duplicates `ALL_EMAIL_REPLIES`) or `FIRST_EMAIL_REPLIES` (subset of `ALL_EMAIL_REPLIES`). Leave “Skip out of office replies” and “Skip autoreplies” **unchecked**.

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

Writes to `export/` under your workspace by default. CSV uses `personalized_first_name`, `personalized_company_name`, plus lead fields, tags, HQ, and `latest_sender`.

**Not for Google Sheets** — `export` writes local files only. For a hosted Google Sheet, use `sheets export` or `review export` (below).

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
  --source csv_import --source-detail "Q2 Apollo list" --import-batch-id "nace-2026-05"

python3 scripts/pipeline.py import-profiles \
  --file sales_nav_export.csv --source sales_navigator --source-detail "Q2 Sales Nav list"

# Rows with only name + company_domain + unified_lead_id (no email/LinkedIn)
python3 scripts/pipeline.py import-profiles \
  --file nace.csv --workspace acme_corp --import-batch-id nace-2026-05
```

**Email-finder batch save (known `lead_id` on every row):** email-finder calls `apply-email-find-results` — updates email, workspace tags, and provider verification in one pass. Requires `--workspace`. Manual recovery:

```bash
python3 scripts/pipeline.py apply-email-find-results \
  --workspace your_workspace --source trykitt --source-detail "email-finder/batch" \
  --file import.json
```

Use `import-profiles` when rows lack `lead_id` / need tiered matching or CSV-only fields (personalization columns, `import_batch_id`, etc.).

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

**Attribution** is automatic: every import sets `original_source` (immutable first touch) and `latest_source` (updates each time) on the lead, following the Salesforce/HubSpot model. Use **`--source`** for the machine-readable channel (`sales_navigator`, `trykitt`, `lead_enrich`, `csv_import`, …). Use **`--source-detail`** or `import_name`/`list_source` columns for list/campaign labels. Per-row `list_source` overrides the CLI `--source` default when present.

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

### Dedup (batch duplicate find + merge)

```bash
python3 scripts/pipeline.py dedup find --workspace popcam --tag campaign --output export/candidates.json
python3 scripts/pipeline.py dedup merge --candidates export/candidates.json          # dry-run
python3 scripts/pipeline.py dedup merge --candidates export/candidates.json --commit   # apply
```

### Google Sheets export (lead review)

To export leads to an editable Google Sheet (two-way sync), use **`sheets export`** or **`review export`** — not `export --format csv`.

Requires `pipeline.py login`. Sheets are created on `app.outreachmagic.io` (no local Google credentials).

```bash
python3 scripts/pipeline.py sheets export --workspace popcam --title "NACE Leads"
# equivalent:
python3 scripts/pipeline.py review export --template lead-review --workspace popcam \
  --tag nace --detail standard --title "NACE Leads"
```

See **Lead review sheet** below for sync-back workflow.

### Dedup review (hosted Google Sheets)

Requires `pipeline.py login`. Sheets are created on `app.outreachmagic.io` and shared to your org owner email (or `--share-email`). Check **Merge?** in the sheet, then sync.

```bash
python3 scripts/pipeline.py review export --input export/candidates.json --title "Popcam Dedup"
python3 scripts/pipeline.py review sync --sheet-id SHEET_ID --dry-run
python3 scripts/pipeline.py review sync --sheet-id SHEET_ID --commit
```

### Lead review sheet (export → edit → sync)

Requires login. Sheets are created on `app.outreachmagic.io` with ✏️/🔒 header icons (no column colors). All usage notes live in the `lead_id` header cell note; freeze row and dropdowns apply before the export URL is returned. Detail levels: `--detail basic|standard|full|custom`. Use `review presets` for the current column catalog (full adds `lev_*`, `be_*`, `latest_source*`, `linkedin_sender_<handle>` keys). Dropdowns: `workspace_stage`, `lead_sentiment`. Export prints row progress and API timing to stderr.

```bash
python3 scripts/pipeline.py review presets --template lead-review
python3 scripts/pipeline.py review export-payload --workspace popcam --tag nace --detail standard
python3 scripts/pipeline.py review export --template lead-review --workspace popcam \
  --tag nace --detail standard --title "NACE Review"
python3 scripts/pipeline.py review sync --template lead-review --workspace popcam \
  --sheet-id SHEET_ID --detail standard --dry-run
python3 scripts/pipeline.py review sync --template lead-review --workspace popcam \
  --sheet-id SHEET_ID --detail standard --commit
```

### Email-finder candidates (safe domain export)

Never use `COALESCE(domain, company)` — use this command to emit batch-find JSON with real `companies.domain` only:

```bash
python3 scripts/pipeline.py email-finder-candidates --workspace popcam --tag nace \
  --no-email --require-domain --never-contacted
```

`export` also supports `--never-contacted`, `--no-email`, and `--require-domain`. Force large relay snapshot pages with `sync --bulk` (or `sync --no-bulk` for routine sizes).

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

1. **Time-window analytics:** use `query engagement` (no pull). **Latest activity:** pull before `show` / `history`.
2. Forgetting add-lead before log-event
3. Not updating stage after reply
4. Setup/auth errors (including 401 Unauthorized) should run `python3 scripts/pipeline.py login` in terminal.
5. **Version:** run `pipeline.py version` — do not guess from SKILL.md frontmatter alone.
6. Relay archive stays on api.outreachmagic.io; `pull` dedupes locally. Use `refresh --yes` for a true rebuild (sync + backup + wipe + `pull --full`). `pull --full` alone only helps after deleting the DB manually.
7. **Tags:** plain names (`nace`, `vip`) — not JSON list strings like `['nace']`. Run `tag repair` for bracket-form tags.
8. **`add-lead` on an existing email does not enrich** — use `import-profiles` or rely on relay `pull` for fill-if-empty updates.
9. **`ModuleNotFoundError: data_freshness`** — run `pipeline.py update`.
10. **Large `import-profiles` batches** — chunked 200 rows; if save times out, re-run with `--file` on your export JSON/CSV.

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