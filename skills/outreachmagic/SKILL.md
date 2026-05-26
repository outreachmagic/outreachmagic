---
name: outreachmagic
description: >
  The outreach data layer for AI agents. Syncs events, replies, and lead
  attributes from Smartlead, Instantly, Heyreach, PlusVibe, and EmailBison
  into a local SQLite database your agent can query directly. Use for pipeline
  views, client briefings, deliverability diagnostics, campaign breakdowns,
  segment performance, and reply copy insights. Webhook payloads pass through
  api.outreachmagic.io; your data lives in a local SQLite file on your machine.
  Free tier: Hermes tracking plus relay (100 events/mo). Pro: unlimited sequencer sync.
version: 1.6.0
author: Outreach Magic
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks, smartlead, instantly, sqlite, gtm]
    related_skills: []
    external_domains:
      - domain: api.outreachmagic.io
        purpose: Relay webhooks and authenticated event pull (payloads imported to local SQLite)
      - domain: dev.outreachmagic.io
        purpose: Portal API for tokens, billing, and workspace routing config sync
---

# Outreach Magic — Pipeline Visibility for Hermes

The simplest pipeline tracker. Hermes auto-logs every outreach action to a local
SQLite database. Free forever. Connect Smartlead, Heyreach, Instantly via paid relay.

Database: `~/.hermes/skills/outreachmagic/databases/outreachmagic.db`
Config (single source of truth): `~/.hermes/skills/outreachmagic/config/outreachmagic_config.json`

Optional config keys: `data_root` (e.g. `~/.claude` for Claude Code), `api_base_url`, `dev_repo` for local development.

Environment variable: `OUTREACHMAGIC_AGENT_KEY` — overrides the config file `agent_key`. Set via `.env`, shell profile, or CI/CD.

## First-Time Setup (IMPORTANT — read this first)

On startup, **always check if the agent is already connected** by running:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

Then check whether an agent key exists in the config:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
```

If `pull` returns an error like "No agent key or token configured", the user needs to set up.

**When setup is needed, tell the user exactly this:**

> To get started, you need an Agent Key. Two steps:
>
> 1. Go to **https://dev.outreachmagic.io/setup/agent** — sign up (or log in) and click "Create Agent Key"
> 2. Copy the key and paste it back here
>
> I'll handle the rest.

Then wait for the user to paste a key (starts with `om_agent_`). Once they do, run:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup --key <PASTED_KEY>
```

That's it. Don't list other commands, don't offer alternatives. Just: go get a key, paste it, done.

**When setup is already done** (pull succeeds or returns events), skip setup and go straight to showing data:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
```

## Version

**One version for the whole skill.** To see what is installed, always run:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

The `version:` line in this file is synced from `scripts/VERSION` on install/update. If unsure, use the command above.

**Updates are user-triggered.** The CLI may print an update notice (at most once per hour) when a newer GitHub **release** exists. It never downloads or replaces scripts automatically. Install updates with:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
# or
hermes skills update
```

Check without installing: `pipeline.py update --check`. Install a specific release: `pipeline.py update --tag v1.4.5`.

## Install

```bash
git clone https://github.com/outreachmagic/hermes-skill.git /tmp/om-hermes
mkdir -p ~/.hermes/skills/outreachmagic
cp -r /tmp/om-hermes/{SKILL.md,scripts,references} ~/.hermes/skills/outreachmagic/
rm -r /tmp/om-hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
hermes -s outreachmagic
```

Local dev sync: `bash scripts/sync-local.sh` from a clone. See `docs/install.md`.

## When to Use

- You are about to send outreach (email, LinkedIn message, WhatsApp, etc.)
- You are researching a prospect and want to track them
- The user asks "show me my pipeline" or "how is outreach going"
- The user says "track this" followed by outreach details
- The user wants to connect a sequencer (paid — requires token)
- The user asks for campaign breakdowns or counts by campaign name
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
- When the user asks about connections, webhook URLs, or platform health, use `status` or `connections`.
- When the user wants to connect a new platform, use `connect-platform --platform <id>`.
- **NEVER use `python3 -c`, `sqlite3` directly, raw SQL, or any inline script to inspect or modify the database.** All database operations must go through `pipeline.py` commands. If a command errors, report the error verbatim and stop — do not attempt to debug by accessing the database directly.

## MANDATORY: Always Pull First

**Before showing any pipeline data (show, stats, campaigns, history, or any query), you MUST run `pull` first.**

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
```

This fetches the latest events from the relay, so the user always sees current data. The local DB may be stale. Never skip this step — even if the user just asks "how's my pipeline" or "any activity?" — pull first, then show. This applies across sessions: a new session's first pipeline query must pull.

## Free Tier

- Unlimited Hermes-originated tracking
- CLI pipeline view + web dashboard
- Pipeline stages with auto-advancement
- 100 relay events/month

## Pro Tier ($19/mo)

- Unlimited relay events
- Smartlead, Heyreach, Instantly, PlusVibe, EmailBison sync
- Multi-platform unified pipeline

Sign up at https://outreachmagic.io

## Quick Start

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --id 1
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --email j@acme.com
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py stats
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py campaigns
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested --json
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py import-profiles --file leads.csv
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local --file changes.csv
```

### Status and connection management

Dashboard-style status, connection management, and webhook URL generation — all from the CLI. These commands talk to the app API and do not require a local database.

```bash
# Dashboard overview: plan, usage, per-platform health, routing
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py status

# List all connections with webhook URLs and 30-day event counts
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connections
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connections --json

# Generate a webhook URL for a new platform
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect-platform --platform smartlead

# Remove a platform connection (webhook URL stops working)
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py disconnect-platform --platform smartlead
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py disconnect-platform --platform smartlead --yes
```

### Export local changes for cross-platform sync

Export locally-created leads and events (not from relay) as JSON or CSV. Useful for transferring data between platforms (Cursor, Hermes, Claude Code).

```bash
# JSON to stdout (pipe to relay push or save to file)
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local

# CSV file (import-profiles compatible)
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local --file local_changes.csv

# Filter to a specific workspace
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local --workspace leadgenph

# Include all leads (not just locally-created)
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py export-local --all
```

**Push to relay for cross-platform sync (manual):**

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py push
```

Other platforms pick up the changes automatically on their next `pull`.

**File-based transfer (no server):**

```bash
# Machine A: export
pipeline.py export-local --file changes.csv
# Machine B: import
pipeline.py import-profiles --file changes.csv --overwrite
```

### Campaign breakdown

Relay imports auto-populate campaign names from webhook payloads (Smartlead, PlusVibe, etc.).

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py campaigns
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py campaigns --json
```

`stats` also includes a campaign section. Use `campaigns` when the user only wants counts by campaign name.

### PlusVibe webhooks (status + sentiment)

Point PlusVibe webhooks at your relay URL (`…/plusvibe/{token}`). Subscribe separately to:

- **Reply events:** `ALL_EMAIL_REPLIES` (and optional `FIRST_EMAIL_REPLIES`, `ALL_POSITIVE_REPLIES`)
- **Label/status events:** `LEAD_MARKED_AS_INTERESTED`, `LEAD_MARKED_AS_NOT_INTERESTED`, `LEAD_MARKED_AS_OUT_OF_OFFICE`, plus any custom `LEAD_MARKED_AS_*` labels

Hermes stores each webhook as an event. **Interested / not interested / sentiment come from label webhooks**, not from reply webhooks alone. OOO is classified as **auto-reply** (metadata flag, query with `--auto-reply true`). Bounces set event sentiment `invalid` but **do not** auto-move the lead to stage `lost` (use `--sentiment invalid` to find them).

After `pull`, filter the pipeline by **current** status (latest status-bearing event per lead):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --sentiment positive
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --sentiment invalid
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --auto-reply true
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --lead-status interested --json
```

Filter by date (created or updated on/after a date):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --since today
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show --since 2026-05-26 --json
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py lead-table --workspace popcam --since today --json
```

Then open full timeline for any lead (all events, not just the status event):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --id 1
```

Native copy-performance analysis (full message bodies + best template):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested --json
```

Web API: `/api/leads?sentiment=positive&auto_reply=true`

## Core Workflow

### View a lead's full timeline

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --id 1
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --email jane@acme.com
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --name "Jane Doe"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --id 1 --json
```

Outputs lead info + numbered event timeline with direction arrows (← inbound, → outbound),
human-readable timestamps, and event details.

### Add leads when researching prospects

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py add-lead \
  --name "Jane Doe" --company "Acme Corp" --title "VP Marketing" \
  --industry "Martech" --headcount "50-200" \
  --email "jane@acme.com" \
  --channel email --stage prospecting
```

To also associate the lead with a workspace at creation time:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py add-lead \
  --name "Jane Doe" --email "jane@acme.com" --company "Acme Corp" \
  --workspace thesystemsmethod --stage contacted
```

`--workspace` is optional on `add-lead` — creating a lead is an org-wide operation. Use it when you know which workspace the lead belongs to; omit it when just researching.

If lead exists by email or LinkedIn, returns `{"status": "exists", "id": N}` (does not enrich existing rows — use `import-profiles` for that).

### Bulk import / enrich (CSV, JSON, research dumps)

**Use `import-profiles` for spreadsheets, enriched exports, or batched research** — not repeated `add-lead` calls. Match key is **email and/or LinkedIn**. Fills empty fields only (same as relay/PlusVibe); use `--overwrite` to replace existing values.

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py import-profiles \
  --file /path/to/contacts_enriched.csv

python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py import-profiles \
  --file leads.json

python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py import-profiles \
  --json '[{"email":"j@acme.com","name":"Jane","job_title":"VP Marketing","industry":"Martech","headcount":"11-50","company":"Acme"}]'

python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py import-profiles \
  --file contacts.csv --dry-run
```

Column aliases (first non-empty wins): `email` / `lead_email`; `linkedin` / `linkedin_url`; `name` / `full_name`; `title` / `job_title`; `company` / `company_name`; `industry`; `headcount` / `company_size`. At least one of **email** or **linkedin** is required per row.

### Companies and unified lead identity

- **`companies` table** — canonical company name, domain, industry, headcount. Leads link via `company_id` (business email domain or company name on ingest).
- **Match by email and/or LinkedIn** — a lead can have email only, LinkedIn only, or both. Relay ingest resolves identity from webhook payload + envelope `lead` field.
- **Merge duplicates** when email and LinkedIn history were separate rows:
  - **Auto:** ingest with both identifiers matching two leads merges them (keeps row with more events).
  - **Manual:**

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py merge-leads --keep 12 --merge 34
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py merge-leads \
  --email j@acme.com --linkedin linkedin.com/in/janedoe
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --linkedin linkedin.com/in/janedoe
```

After `pull`, use **`campaigns`** for per-campaign event and lead counts (unchanged).

### Log every outreach send

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py log-event \
  --lead-id 1 --type email_sent --direction outbound \
  --subject "Quick intro" --workspace thesystemsmethod
```

`--workspace` is **required** in multi-workspace mode. Outreach events are workspace-scoped — they belong to a specific pipeline. In single-workspace mode it falls back to the default workspace.

### Update stage and log replies

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update-stage \
  --id 1 --stage replied --next-action "Send case study" --workspace thesystemsmethod
```

`--workspace` is **required** in multi-workspace mode. Stage is per-workspace — a lead can be "contacted" in one workspace and "interested" in another.

Stages: `prospecting` -> `contacted` -> `replied` -> `interested` -> `proposal` -> `won` | `lost`

### Connect sequencers (paid)

If the user already has a key, skip the browser flow:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_THEIR_KEY
```

Generate webhook URLs for platforms directly from the CLI (requires agent key):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect-platform --platform smartlead
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect-platform --platform instantly
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connections
```

Legacy per-platform token (not agent key):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull --full   # after DB reset
```

### Update skill scripts

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
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

## Web Dashboard

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/server.py
# http://localhost:3100
```

## Privacy & Security

- **Local-first data.** Pipeline leads, events, and campaign stats live in `~/.hermes/skills/outreachmagic/databases/outreachmagic.db` on your machine.
- **Relay pass-through.** Webhooks hit `api.outreachmagic.io`; the CLI imports them locally via `pull`. We store tokens and usage on our side, not a searchable cloud copy of your outreach archive.
- **Portal API.** `dev.outreachmagic.io` (production: app.outreachmagic.io) handles tokens, billing, and optional workspace routing sync when connected.
- **Credentials.** Store relay tokens in `config/outreachmagic_config.json` only. Never hardcode tokens in SKILL.md or commit them to git.
- **Read before connect.** See repo root [SECURITY.md](https://github.com/outreachmagic/hermes-skill/blob/main/SECURITY.md) for full data boundaries and vulnerability reporting.

## Common Pitfalls

1. **Always pull before show when checking "latest activity."**
2. Forgetting add-lead before log-event
3. Not updating stage after reply
4. Setup requires an Agent Key — get one at https://dev.outreachmagic.io/setup/agent
5. **Version:** run `pipeline.py version` — do not guess from SKILL.md frontmatter alone.
6. Relay archive stays on api.outreachmagic.io; `pull` dedupes locally. Use `pull --full` after deleting the local DB.
7. **`add-lead` on an existing email does not enrich** — use `import-profiles` or rely on relay `pull` for fill-if-empty updates.
