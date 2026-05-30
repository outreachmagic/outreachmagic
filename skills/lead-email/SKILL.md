---
name: lead-email
description: >
  Find work emails with trykitt.ai after you have a person's name and company
  domain. Checks Outreach Magic first to avoid duplicate API spend. Saves email
  and verification status via the outreachmagic skill. Pair with lead-enrich for
  domain discovery or use with CRM exports that already have company_domain.
version: 1.0.0
author: Outreach Magic
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: TRYKITT_API_KEY
    prompt: trykitt.ai API key
    help: Get a free key at https://trykitt.ai
    required_for: Email find via trykitt.ai job/find_email
  - name: OUTREACHMAGIC_AGENT_KEY
    prompt: Outreach Magic agent key
    help: Create at https://dev.outreachmagic.io/setup/agent (starts with om_agent_)
    required_for: Saving found emails and dedup checks against local SQLite
metadata:
  hermes:
    tags: [sales, outreach, email, enrichment, leads, trykitt, pipeline]
    related_skills: [outreachmagic, lead-enrich]
    external_domains:
      - domain: api.trykitt.ai
        purpose: Email find + SMTP verify (POST job/find_email with user API key)
      - domain: api.outreachmagic.io
        purpose: Via outreachmagic skill — save profiles and record verify-email locally
---

# Lead Email — trykitt.ai Email Finding

Find a deliverable work email when you already have **name + company domain**
(from **lead-enrich**, outreachmagic, or a CSV). Uses **trykitt.ai** only in v1;
waterfall providers are documented for manual/agent fallback.

> **Credit-saving:** always checks outreachmagic first. If the lead already has
> an email, no trykitt call is made.

## Prerequisites

### 1. outreachmagic

Required for dedup and save. Install under `~/.hermes/skills/outreachmagic/` and run:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

### 2. trykitt.ai API key

Add to `~/.hermes/.env` (see `default.env`):

```bash
TRYKITT_API_KEY=your_key_here
```

Free tier throttles at ~10 concurrent requests — use **8+ second** delays in
`batch-find` for 50+ leads.

### 3. Domain + LinkedIn context

Best results when `company_domain` and `linkedin_url` came from **lead-enrich**
Phase 2–4. Minimum required: `fullName` + `domain`.

## When to Use

- User asks to find someone's email after enrichment
- CSV has name + domain but no email
- lead-enrich saved LinkedIn + domain but skipped email (v2+)
- Re-find after a bounce (clear bad email, remove `trykitt_attempted` tag)

## Agent Behavior Rules

1. **Check outreachmagic first** — `lead_email.py check` or `find` (auto-skips if email exists).
2. **Never fabricate emails** — only save trykitt (or documented waterfall) results.
3. **Tag `trykitt_attempted`** after each attempt (hit or miss) so batch runs do not repeat.
4. **Record verification** — after save, `verify-email` records validity (`valid`, `risky`, etc.).
5. **Batch delays** — `batch-find --delay 8` on free tier.
6. **Setup in terminal** — `pipeline.py login`, not chat secrets.

## Commands

```bash
python3 scripts/lead_email.py config
python3 scripts/lead_email.py check "Jane Doe" "Acme Corp"
python3 scripts/lead_email.py find --name "Jane Doe" --domain acme.com \
  --linkedin "https://linkedin.com/in/janedoe" --company "Acme Corp" --save
python3 scripts/lead_email.py batch-find --delay 8 --workspace client_slug leads.json
python3 scripts/lead_email.py update --check
```

## Workflow with lead-enrich

1. `enrich.py check` → Serper only if needed  
2. Save enrichment via outreachmagic (`import-profiles`)  
3. `lead_email.py find --save` when domain is known and user wants email  

See `references/email-finding-research.md` for API details and waterfall order.

## Related

- **outreachmagic** — data layer ([outreachmagic.io](https://outreachmagic.io))
- **lead-enrich** — Serper person research + dedup

Suite overview: [skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md)
