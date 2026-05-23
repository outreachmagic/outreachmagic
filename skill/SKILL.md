---
name: outreachmagic
description: "Use when sending outreach (email, LinkedIn, WhatsApp), researching prospects, showing the pipeline, or connecting sequencer webhooks (paid). Auto-logs actions to local SQLite."
version: 1.3.2
author: Outreach Magic
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks]
    related_skills: []
---

# Outreach Magic — Pipeline Visibility for Hermes

The simplest pipeline tracker. Hermes auto-logs every outreach action to a local
SQLite database. Free forever. Connect Smartlead, Heyreach, Instantly via paid relay.

Database: `~/.hermes/outreachmagic.db`

## Version

**One version for the whole skill.** To see what is installed, always run:

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py version
```

The `version:` line in this file is synced from `scripts/VERSION` on install/update. If unsure, use the command above.

**Auto-update:** On each command (at most once per hour), the CLI checks GitHub for a newer `VERSION` and downloads it automatically. Users on Hermes get updates when you push to `main` without running `install.sh` manually. Disable: `"auto_update": false` in `~/.hermes/outreachmagic_config.json`.

## When to Use

- You are about to send outreach (email, LinkedIn message, WhatsApp, etc.)
- You are researching a prospect and want to track them
- The user asks "show me my pipeline" or "how is outreach going"
- The user says "track this" followed by outreach details
- The user wants to connect a sequencer (paid — requires token)

- The user asks for campaign breakdowns or counts by campaign name

## Agent Behavior Rules (Important)

- For bulk enrichment (CSV, spreadsheet export, Apollo/Clay dump): use **`import-profiles`**, not repeated `add-lead`.
- Always run `pull` first before showing pipeline data, history, stats, or campaigns.
- After any pull, explicitly report the exact number of new records imported.
- When the user asks about version, read the frontmatter of SKILL.md (version line).
- When the user asks for message content, use the `history` command on the specific lead.
- For copy-performance analysis (full subject/body on positive leads + winner), use `copy-insights`.
- For campaign counts, use `pipeline.py campaigns` (or `stats`, which includes a campaign section). Do not write raw SQL.

## MANDATORY: Always Pull First

**Before showing any pipeline data (show, stats, campaigns, history, or any query), you MUST run `pull` first.**

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py pull
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
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py version
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --id 1
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --email j@acme.com
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py stats
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py campaigns
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested --json
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py import-profiles --file leads.csv
```

### Campaign breakdown

Relay imports auto-populate campaign names from webhook payloads (Smartlead, PlusVibe, etc.).

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py campaigns
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py campaigns --json
```

`stats` also includes a campaign section. Use `campaigns` when the user only wants counts by campaign name.

### PlusVibe webhooks (status + sentiment)

Point PlusVibe webhooks at your relay URL (`…/plusvibe/{token}`). Subscribe separately to:

- **Reply events:** `ALL_EMAIL_REPLIES` (and optional `FIRST_EMAIL_REPLIES`, `ALL_POSITIVE_REPLIES`)
- **Label/status events:** `LEAD_MARKED_AS_INTERESTED`, `LEAD_MARKED_AS_NOT_INTERESTED`, `LEAD_MARKED_AS_OUT_OF_OFFICE`, plus any custom `LEAD_MARKED_AS_*` labels

Hermes stores each webhook as an event. **Interested / not interested / sentiment come from label webhooks**, not from reply webhooks alone. OOO is classified as **auto-reply** (metadata flag, query with `--auto-reply true`). Bounces set event sentiment `invalid` but **do not** auto-move the lead to stage `lost` (use `--sentiment invalid` to find them).

After `pull`, filter the pipeline by **current** status (latest status-bearing event per lead):

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show --sentiment positive
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show --sentiment invalid
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show --auto-reply true
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show --lead-status interested --json
```

Then open full timeline for any lead (all events, not just the status event):

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --id 1
```

Native copy-performance analysis (full message bodies + best template):

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py copy-insights --lead-status interested --json
```

Web API: `/api/leads?sentiment=positive&auto_reply=true`

## Core Workflow

### View a lead's full timeline

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --id 1
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --email jane@acme.com
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --name "Jane Doe"
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --id 1 --json
```

Outputs lead info + numbered event timeline with direction arrows (← inbound, → outbound),
human-readable timestamps, and event details.

### Add leads when researching prospects

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py add-lead \
  --name "Jane Doe" --company "Acme Corp" --title "VP Marketing" \
  --industry "Martech" --headcount "50-200" \
  --email "jane@acme.com" \
  --channel email --stage prospecting
```

If lead exists by email or LinkedIn, returns `{"status": "exists", "id": N}` (does not enrich existing rows — use `import-profiles` for that).

### Bulk import / enrich (CSV, JSON, research dumps)

**Use `import-profiles` for spreadsheets, enriched exports, or batched research** — not repeated `add-lead` calls. Match key is **email and/or LinkedIn**. Fills empty fields only (same as relay/PlusVibe); use `--overwrite` to replace existing values.

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py import-profiles \
  --file /path/to/contacts_enriched.csv

python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py import-profiles \
  --file leads.json

python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py import-profiles \
  --json '[{"email":"j@acme.com","name":"Jane","job_title":"VP Marketing","industry":"Martech","headcount":"11-50","company":"Acme"}]'

python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py import-profiles \
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
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py merge-leads --keep 12 --merge 34
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py merge-leads \
  --email j@acme.com --linkedin linkedin.com/in/janedoe
```

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py history --linkedin linkedin.com/in/janedoe
```

After `pull`, use **`campaigns`** for per-campaign event and lead counts (unchanged).

### Log every outreach send

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py log-event \
  --lead-id 1 --type email_sent --direction outbound \
  --subject "Quick intro"
```

### Update stage and log replies

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py update-stage \
  --id 1 --stage replied --next-action "Send case study"
```

Stages: `prospecting` -> `contacted` -> `replied` -> `interested` -> `proposal` -> `won` | `lost`

### Connect sequencers (paid)

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py pull --full   # after DB reset
```

### Update skill scripts

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py update
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

## Web Dashboard

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/server.py
# http://localhost:3100
```

## Common Pitfalls

1. **Always pull before show when checking "latest activity."**
2. Forgetting add-lead before log-event
3. Not updating stage after reply
4. Connect requires a token — sign up at outreachmagic.io
5. **Version:** run `pipeline.py version` — do not guess from SKILL.md frontmatter alone.
6. Relay archive stays on wbhk.org; `pull` dedupes locally. Use `pull --full` after deleting the local DB.
7. **`add-lead` on an existing email does not enrich** — use `import-profiles` or rely on relay `pull` for fill-if-empty updates.
