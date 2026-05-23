---
name: outreachmagic
description: "Use when sending outreach (email, LinkedIn, WhatsApp), researching prospects, showing the pipeline, or connecting sequencer webhooks (paid). Auto-logs actions to local SQLite."
version: 1.3.0
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

Database: `~/.hermes/outreach_magic.db`

## Version

**One version for the whole skill.** To see what is installed, always run:

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py version
```

The `version:` line in this file is synced from `scripts/VERSION` when you run `install.sh` or `pipeline.py update`. If unsure, use the command above — not frontmatter alone.

## When to Use

- You are about to send outreach (email, LinkedIn message, WhatsApp, etc.)
- You are researching a prospect and want to track them
- The user asks "show me my pipeline" or "how is outreach going"
- The user says "track this" followed by outreach details
- The user wants to connect a sequencer (paid — requires token)

## MANDATORY: Always Pull First

**Before showing any pipeline data (show, stats, history, or any query), you MUST run `pull` first.**

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
```

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

If lead exists by email, returns `{"status": "exists", "id": N}`.

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
