---
name: email-finder
description: >
  Find work emails with trykitt.ai and Icypeas. Checks Outreach
  Magic first to avoid duplicate API spend. Saves email and verification status
  via the outreachmagic skill.
version: 2.1.1
author: Outreach Magic
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: TRYKITT_API_KEY
    prompt: trykitt.ai API key
    help: Get a free key at https://trykitt.ai
    required_for: Email find via trykitt.ai job/find_email
  - name: ICYPEAS_API_KEY
    prompt: Icypeas API key
    help: Get your key at https://app.icypeas.com
    required_for: Email find via Icypeas email-search API
  - name: OUTREACHMAGIC_AGENT_KEY
    prompt: Outreach Magic agent key
    help: Create at https://app.outreachmagic.io/setup/agent (starts with om_agent_)
    required_for: Saving found emails and dedup checks against local SQLite
metadata:
  hermes:
    tags: [sales, outreach, email, enrichment, leads, trykitt, icypeas, pipeline]
    related_skills: [outreachmagic, lead-enrich]
    external_domains:
      - domain: api.trykitt.ai
        purpose: Email find + SMTP verify (POST job/find_email with user API key)
      - domain: app.icypeas.com
        purpose: Email find + polling read endpoints (Authorization API key)
      - domain: api.outreachmagic.io
        purpose: Via outreachmagic skill — save profiles via import-profiles locally
---

# Email Finder — trykitt.ai + Icypeas Email Finding

Find a deliverable work email when you already have **name + company domain**
(from **lead-enrich**, outreachmagic, or a CSV). Supports **trykitt.ai** and
**Icypeas** with provider fallback.

> **Credit-saving:** always checks outreachmagic first. If the lead already has
> an email, no provider call is made.

## Prerequisites

### 1. outreachmagic

Required for dedup and save. Install under `~/.hermes/skills/outreachmagic/` and run:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

Batch dedup uses `pipeline.py batch-lead-lookup` (one DB pass). Update outreachmagic to a recent release if that command is missing.

### 2. Provider API keys

Add to `~/.hermes/.env` or your Hermes profile env (`~/.hermes/profiles/<name>/.env`).
Set `HERMES_PROFILE=<name>` when running in background/batch so subprocesses pick up the key:

```bash
TRYKITT_API_KEY=your_key_here
ICYPEAS_API_KEY=your_key_here
```

Free tier throttles at ~10 concurrent requests — use **8+ second** delays in
`batch-find` for 50+ leads, or `parallel-find --workers 3` for large CSV runs.

### 3. Domain + LinkedIn context

Best results when `company_domain` and `linkedin_url` came from **lead-enrich**
Phase 2–4. Minimum required: `fullName` + `domain`.

## When to Use

- User asks to find someone's email after enrichment
- CSV has name + domain but no email
- lead-enrich saved LinkedIn + domain but skipped email (v2+)
- Re-find after a bounce (clear bad email, remove attempt tags)

## Agent Behavior Rules

1. **Check outreachmagic first** — `email_finder.py check` or `find` (auto-skips if email exists).
2. **Never fabricate emails** — only save provider API results.
3. **Provider order:** when both are enabled, run trykitt first, then Icypeas on miss.
4. **Tag attempts** — `trykitt_attempted` and/or `icypeas_attempted`; add **`email_found`** when an email is saved.
5. **Verify found emails** — after batch import, `verify-email --batch` records validity (trykitt / Icypeas).
6. **Batch saves once** — one `import-profiles` + one `verify-email --batch` at end (incremental CSV/JSON during run).
7. **Input keys** — JSON accepts `fullName`, `full_name`, or `name`; optional `linkedin` / `lead_id`.
8. **Large batches** — `batch-find --workers 3 --yes --output-base results --max 500` (default cap 500).
9. **Setup in terminal** — `pipeline.py login`, not chat secrets.

## Commands

```bash
python3 scripts/email_finder.py config
python3 scripts/email_finder.py check "Jane Doe" "Acme Corp"
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com \
  --linkedin "https://linkedin.com/in/janedoe" --company "Acme Corp" --save --workspace client_slug
python3 scripts/email_finder.py batch-find --workspace client_slug --yes --output-base results leads.json
python3 scripts/email_finder.py batch-find --dry-run --workspace client_slug leads.json
python3 scripts/email_finder.py batch-find --workers 3 --delay 3 --provider trykitt --yes leads.json
python3 scripts/email_finder.py batch-find --skip-om --output-base results leads.json
python3 scripts/email_finder.py parallel-find --workers 3 --yes leads.json   # alias for batch-find
python3 scripts/email_finder.py prepare-import --csv results.csv --output import.json
python3 scripts/email_finder.py import-to-om --file import.json --workspace client_slug
python3 scripts/email_finder.py update --check
```

### Recommended large-batch workflow (Hermes style)

Use **one command** — `batch-find` with incremental saves and resume:

```bash
python3 scripts/email_finder.py batch-find \
  --workspace your_workspace --yes --output-base ./export/headshot-emails \
  --workers 3 --delay 3 input/leads.json
```

Re-run the same command after a crash; it resumes from `{output-base}.csv`.

## Workflow with lead-enrich

1. `enrich.py check` → Serper only if needed  
2. Save enrichment via outreachmagic (`import-profiles`)  
3. `email_finder.py find --save` when domain is known and user wants email  

See `references/email-finding-research.md` for API details and waterfall order.

## Related

- **outreachmagic** — data layer ([outreachmagic.io](https://outreachmagic.io))
- **lead-enrich** — Serper person research + dedup

Suite overview: [skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md)
