---
name: email-finder
description: >
  Find work emails with trykitt.ai and Icypeas (waterfall). Checks Outreach Magic
  first to avoid duplicate API spend. Saves email and verification via outreachmagic.
  Optional MillionVerifier for bulk re-check.
version: 2.2.5
author: Outreach Magic
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: TRYKITT_API_KEY
    prompt: trykitt.ai API key
    help: Get a free key at https://trykitt.ai
    required_for: Email find via trykitt (first in waterfall)
  - name: ICYPEAS_API_KEY
    prompt: Icypeas API key
    help: Get your key at https://app.icypeas.com
    required_for: Email find via Icypeas (fallback)
  - name: OUTREACHMAGIC_AGENT_KEY
    prompt: Outreach Magic agent key
    help: Create at https://app.outreachmagic.io/setup/agent (starts with om_agent_)
    required_for: Dedup and save to local SQLite
  - name: MILLIONVERIFIER_API_KEY
    prompt: MillionVerifier API key
    help: https://app.millionverifier.com — optional verify commands only
    required_for: verify / verify-bulk (optional)
metadata:
  hermes:
    tags: [sales, outreach, email, enrichment, leads, trykitt, icypeas, pipeline]
    related_skills: [outreachmagic, lead-enrich]
    external_domains:
      - domain: api.trykitt.ai
        purpose: Email find (POST job/find_email, user API key)
      - domain: app.icypeas.com
        purpose: Email find + poll read (Authorization header)
      - domain: api.millionverifier.com
        purpose: Optional single/bulk verification
      - domain: api.outreachmagic.io
        purpose: Via outreachmagic — apply-email-find-results (batch) or import-profiles
---

# Email Finder

Find work emails when you have **name + company domain** (from **lead-enrich**, outreachmagic, or CSV). **trykitt** first, **Icypeas** on miss. Checks outreachmagic before any paid lookup.

## Prerequisites

1. **outreachmagic** — `pipeline.py login` (dedup + save).
2. **API keys** in `~/.hermes/.env` (or profile `.env`): `TRYKITT_API_KEY`, `ICYPEAS_API_KEY` (at least one).
3. **Batch runs:** `lead_id` on every row and **`--workspace`** (required for OM save).

## Production batch defaults

| Mode | Suggested command flags |
|------|-------------------------|
| Waterfall (trykitt → IcyPeas) | `--workers 3 --delay 3` (or `--delay 8` on free trykitt tiers) |
| IcyPeas only | `--workers 2 --delay 3` |
| TryKitt only | `--workers 3` (optional `--delay 0.2`) |

Config overrides in `config.json`: `icypeas_poll_attempts` (default 30), `icypeas_poll_delay_seconds` (3), `icypeas_request_delay_seconds` (1.5).

## When to use

- After lead-enrich or import when email is still empty
- CSV / Sales Nav export with domain but no email
- Re-find after bounce (clear bad email + remove attempt tags first)

## Agent rules

1. **Check OM first** — `check` or `find` skips if email exists.
2. **Never fabricate emails** — provider API results only.
3. **Waterfall** — trykitt then Icypeas when both keys are set.
4. **Tags** — `trykitt_attempted` / `icypeas_attempted`; `email_found` when saved.
5. **Batch input** — `lead_id` on every row (maps to OM `id` on save).
6. **Batch find** — `batch-find --workspace W` writes CSV/JSON incrementally, then saves via `apply-email-find-results` (500/chunk, verification inline). Rows without `lead_id` use chunked `import-profiles` (200/chunk). Payloads >100KB use temp files.
7. **Secrets** — `pipeline.py login` in terminal, not chat.
8. **IcyPeas batches** — never use 3 workers with zero delay; low hit rate (~10%) often means poll timeout, not list quality.
9. **Result semantics** — `not_found` = no email; `error` + `icypeas_timeout` = still processing; `rate_limited` = retry with higher `--delay`; `DEBITED_NOT_FOUND` = charged, no email (`icypeas_status` in CSV).

## Batch input

```json
[
  {
    "lead_id": 12345,
    "name": "Jane Doe",
    "company_domain": "acme.com",
    "linkedin": "https://linkedin.com/in/jane"
  }
]
```

## Commands

```bash
# Find
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com \
  --linkedin "https://linkedin.com/in/jane" --save --workspace CLIENT
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes \
  --output-base ./export/emails --workers 3 --delay 3 leads.json
python3 scripts/email_finder.py batch-find --provider icypeas --workspace CLIENT --yes \
  --workers 2 --delay 3 --output-base ./export/icypeas leads.json
python3 scripts/email_finder.py batch-find --dry-run --workspace CLIENT leads.json

# Recovery after a completed find (OM save only)
python3 scripts/email_finder.py import-to-om --file ./export/emails.json --workspace CLIENT

# Optional MillionVerifier (needs MILLIONVERIFIER_API_KEY)
python3 scripts/email_finder.py verify --email jane@acme.com --workspace CLIENT
python3 scripts/email_finder.py verify-bulk --workspace CLIENT --output /tmp/mv_job.txt
python3 scripts/email_finder.py verify-status --file-id JOB_ID
python3 scripts/email_finder.py verify-download --file-id JOB_ID --workspace CLIENT

python3 scripts/email_finder.py update --check
```

Re-run the same `batch-find` after a crash to resume from `{output-base}.csv`.

## Large batches (500+ leads)

- **CSV/JSON** are written during the run; OM sync runs at the end (requires `--workspace`).
- **Success check** — final summary shows `imported` and `verified`, not only CSV rows.
- **Recovery** — if OM save fails, stderr lists checkpoint paths; use `import-to-om --file {output-base}.json --workspace W`.

## Troubleshooting

- **IcyPeas hit rate ~10%** — poll timeout; re-run or raise `icypeas_poll_attempts`.
- **CSV has emails but OM is empty** — import step failed; use `import-to-om` with `--workspace`.
- **Mass rate limit errors** — `--workers 2 --delay 3` for IcyPeas-only.
- **New leads created in OM** — every row needs `lead_id`; check stderr for unexpected `created` counts.

## Funnel

`lead-enrich` → save to OM (keep `lead_id`) → `batch-find --workspace W` → emails on existing leads.

Hub copy: [docs/positioning/hub-copy.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/positioning/hub-copy.md). API notes: `references/email-finding-research.md`.

## Related

- **outreachmagic** — [outreachmagic.io](https://outreachmagic.io)
- **lead-enrich** — Serper research + dedup

Suite: [skill-suite.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md)
