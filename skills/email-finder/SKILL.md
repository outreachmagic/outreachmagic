---
name: email-finder
description: >
  Find work emails with trykitt.ai and Icypeas (waterfall). Checks Outreach Magic
  first to avoid duplicate API spend. Saves email and verification via outreachmagic.
  Optional MillionVerifier for bulk re-check.
version: 2.2.18
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
    help: Create at https://app.outreachmagic.io/onboarding (starts with om_agent_)
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

Find work emails when you have **name + company domain**. **trykitt** first, **Icypeas** on miss. Checks outreachmagic before any paid lookup.

## Prerequisites

1. **outreachmagic** — `pipeline.py login`
2. **API keys** — save in Dashboard → API Keys, then `pipeline.py sync-secrets` (writes `<skill_home>/config/agent_secrets.env`; scripts load this automatically). Legacy: `~/.hermes/.env` with `TRYKITT_API_KEY` and/or `ICYPEAS_API_KEY`.
3. **Batch:** `lead_id` on every row + **`--workspace`**

Before find/batch, confirm keys: `python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py sync-secrets --check --json` or `python3 scripts/email_finder.py config`.

## Production batch defaults

| Mode | Flags |
|------|-------|
| Waterfall | `--workers 3 --delay 3` |
| IcyPeas only | `--workers 2 --delay 3` |
| TryKitt only | `--workers 3` (optional `--delay 0.2`) |

## Agent rules

1. Check OM first (`check` / `find`).
2. Never fabricate emails.
3. Waterfall: trykitt → Icypeas when both keys set.
4. Tags: `trykitt_attempted` / `icypeas_attempted`; `email_found` when saved; `mv_attempted` after MillionVerifier bulk (result lives in OM `email_verification_status`).
5. Batch: `lead_id` on every row; `--workspace` required for OM save.
6. `batch-find` re-checks OM immediately before each API call (skips leads resolved since batch start).
7. `batch-find` writes `{output-base}.csv` / `.json` incrementally, then saves to OM (`apply-email-find-results` when all rows have `lead_id`).
8. COMPLETE box always shows **IMPORT** status. If import was skipped or failed, run `import-to-om` with the checkpoint file.
9. **Credits** — **1 credit per email found** (trykitt / IcyPeas) or **1 credit per email verified** (MillionVerifier). Not-found lookups cost **0** credits. Never multiply by provider `jobCredits` (e.g. 0.005) — use `verify-credits` or `verify-bulk --dry-run` for MV balance checks.

## Batch input

```json
[{"lead_id": 12345, "name": "Jane Doe", "company_domain": "acme.com"}]
```

## Commands

```bash
# Find + save one lead
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com \
  --save --workspace CLIENT

# Batch (find + OM save)
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes \
  --output-base ./export/emails --workers 3 --delay 3 leads.json

# OM save only — after failed import or --no-save run (accepts batch .csv or .json)
python3 scripts/email_finder.py import-to-om --file ./export/emails.csv --workspace CLIENT

python3 scripts/email_finder.py update --check

# MillionVerifier (optional) — keys often come from OM agent_secrets, not local .env placeholders
python3 scripts/email_finder.py config
python3 scripts/email_finder.py verify-credits
python3 scripts/email_finder.py verify-bulk --workspace CLIENT --dry-run
python3 scripts/email_finder.py verify-bulk --workspace CLIENT --poll --yes
```

`MILLIONVERIFIER_API_KEY` in a local `.env` may show `***`; OM `agent_secrets.env` overrides via `ensure_env_loaded()`.

Resume a crashed batch by re-running the same `batch-find` command (skips completed API rows).

## Troubleshooting

- **`ModuleNotFoundError: data_freshness`** — run `pipeline.py update` on outreachmagic.
- **COMPLETE shows `⚠ No import` in IMPORT section** — results are on disk; `import-to-om --file {output-base}.csv --workspace W`
- **CSV has emails, OM empty** — batch save failed; `import-to-om --file {output-base}.csv --workspace W`
- **`import-profiles` timed out** — results are on disk; use `import-to-om` or re-run with smaller batches.
- **IcyPeas ~10% hit rate** — poll timeout; raise `icypeas_poll_attempts` in config
- **New leads created** — every row needs `lead_id`

## Funnel

`lead-enrich` → OM (keep `lead_id`, stamp `serper_attempted`) → `batch-find --workspace W`. Both companions skip leads already tagged (`serper_attempted` / `trykitt_attempted` / `icypeas_attempted`).
