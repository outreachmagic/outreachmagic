---
name: email-finder
description: >
  Waterfall email enrichment through trykitt and Icypeas. Works standalone with
  just API keys. Pair it with Outreach Magic and it checks your local DB first.
  Skips leads you already have, saves the result so you don't run the same
  search twice. Optional MillionVerifier for bulk re-check.
version: 1.0.0
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
    required_for: Dedup and save to OM pipeline (not needed for standalone find)
  - name: MILLIONVERIFIER_API_KEY
    prompt: MillionVerifier API key
    help: https://app.millionverifier.com — optional verify commands only
    required_for: verify / verify-bulk (optional)
  - name: SCRUBBY_API_KEY
    prompt: Scrubby API key
    help: https://api.scrubby.io — optional deep verification (72h) for catch-all/unknown emails
    required_for: scrubby-deep-* / verify-with-scrubby (optional)
metadata:
  hermes:
    tags: [sales, outreach, email, enrichment, leads, trykitt, icypeas, pipeline, ecosystem:outreachmagic]
    category: email
    homepage: https://outreachmagic.io
    related_skills: [outreachmagic, lead-enrich]
    external_domains:
      - domain: api.trykitt.ai
        purpose: Email find (POST job/find_email, user API key)
      - domain: app.icypeas.com
        purpose: Email find + poll read (Authorization header)
      - domain: api.millionverifier.com
        purpose: Optional single/bulk verification
      - domain: api.scrubby.io
        purpose: Optional deep email verification (72h, catch-all/unknown second pass)
      - domain: api.outreachmagic.io
        purpose: Via outreachmagic — apply-email-find-results (batch) or import-profiles
---

# Email Finder

Find work emails when you have **name + company domain**. **trykitt** first, **Icypeas** on miss.

**Works standalone.** Just needs API keys. Pairs with **Outreach Magic** for
credit-saving dedup, persistent storage, and cross-session availability.

## Setup

### Standalone (no OM)

Just API keys. Results print to stdout. No database needed.

| Key | For |
|-----|-----|
| `TRYKITT_API_KEY` | trykitt.ai (first in waterfall) — [trykitt.ai](https://trykitt.ai) |
| `ICYPEAS_API_KEY` | Icypeas (fallback) — [app.icypeas.com](https://app.icypeas.com) |

```bash
python3 scripts/email_finder.py config  # verify keys loaded
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com
# → prints result to stdout
```

### With Outreach Magic (dedup + save)

Adds pre-flight dedup (skip leads already in OM) and saves results to your local
SQLite pipeline. Requires [outreachmagic skill](https://github.com/outreachmagic/outreachmagic)
with `pipeline.py login`.

| Keys | For |
|-----|-----|
| All standalone keys above + | |
| `OUTREACHMAGIC_AGENT_KEY` | OM login via `pipeline.py login` |

```bash
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com --save --workspace CLIENT
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes --workers 3 --delay 3 input.json
```

Before find/batch with OM, confirm keys:
`python3 <SKILLS>/outreachmagic/scripts/pipeline.py sync-secrets --check --json`
or `python3 scripts/email_finder.py config`.

Keys sync via Dashboard → `pipeline.py sync-secrets`. Verify source:
`python3 scripts/email_finder.py config` (`*_api_key_source` should be `agent_secrets`).

### Batch format

```json
[{"lead_id": 12345, "name": "Jane Doe", "company_domain": "acme.com"}]
```

## Production batch defaults

| Mode | Flags |
|------|-------|
| Waterfall | `--workers 3 --delay 3` |
| IcyPeas only | `--workers 2 --delay 3` |
| TryKitt only | `--workers 3` (optional `--delay 0.2`) |

## Agent rules

1. **With OM:** check before find (`check` / `find` with `--save`). **Standalone:** skip check — run find directly.
2. Never fabricate emails.
3. Waterfall: trykitt → Icypeas when both keys set.
4. **With OM:** tags `trykitt_attempted` / `icypeas_attempted`; `mv_attempted` after MillionVerifier bulk (result lives in OM `email_verification_status`). `scrubby_deep_submitted` / `scrubby_deep_attempted` after Scrubby Deep Verification submit/fetch. Found state is `leads.email`, `latest_source`, and `email_verification_status`.
5. **With OM:** `lead_id` on every row; `--workspace` required for OM save. **Standalone:** omit `--save` or use `--no-save` to print results to stdout.
6. Run `batch-find --dry-run` before `--yes` to see skip counts (leads may already have email in OM while CSV `email` is empty).
7. `batch-find` re-checks OM immediately before each API call (skips leads resolved since batch start).
8. **With OM:** `batch-find` writes CSV/JSON under `outreachmagic/exports/`, then saves to OM. **Standalone:** add `--skip-om` to run without OM (writes to cwd).
9. COMPLETE box shows **IMPORT** and **RELAY** (pending snapshots — run `pipeline.py sync`; upload is never automatic).
10. **Credits** — **1 credit per email found** (trykitt / Icypeas) or **1 credit per email verified** (MillionVerifier) or **3 credits per deep verification** (Scrubby). Not-found lookups cost **0** credits.

## Batch input

```json
[{"lead_id": 12345, "name": "Jane Doe", "company_domain": "acme.com"}]
```

Optionally include `"linkedin_url"` when available — trykitt uses it for higher match rates.

## Commands

```bash
# Standalone (no OM) — prints result to stdout
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com

# Standalone batch (no OM) — writes CSV to cwd
python3 scripts/email_finder.py batch-find --skip-om --yes --dry-run input.json

# With OM — find + dedup + save
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com --save --workspace CLIENT

# Dry run (skip counts, no API spend)
python3 scripts/email_finder.py batch-find --workspace CLIENT --dry-run outreachmagic/batches/leads.json

# Batch (find + OM save)
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes \
  --output-base outreachmagic/exports/emails --workers 3 --delay 3 outreachmagic/batches/leads.json

# OM save only — after failed import or --no-save run (accepts batch .csv or .json)
python3 scripts/email_finder.py import-to-om --file outreachmagic/exports/emails.csv --workspace CLIENT

python3 scripts/email_finder.py update --check

# MillionVerifier (optional)
python3 scripts/email_finder.py config
python3 scripts/email_finder.py verify-credits
python3 scripts/email_finder.py verify-bulk --workspace CLIENT --dry-run
python3 scripts/email_finder.py verify-bulk --workspace CLIENT --poll --yes

# Scrubby Deep Verification (optional — 72h second pass on catch-all / unknown)
python3 scripts/email_finder.py scrubby-deep-credits                              # Check remaining credits
python3 scripts/email_finder.py scrubby-deep-submit --workspace CLIENT --dry-run    # Dry-run (email count only)
python3 scripts/email_finder.py scrubby-deep-submit --workspace CLIENT              # Submit all unverified
python3 scripts/email_finder.py scrubby-deep-submit --workspace CLIENT --filter=catch_all  # Only catch_all/unknown
python3 scripts/email_finder.py scrubby-deep-fetch --identifier ID --workspace CLIENT  # Fetch results + save
python3 scripts/email_finder.py scrubby-deep-fetch --identifier ID --workspace CLIENT --poll  # Poll until complete
python3 scripts/email_finder.py scrubby-deep-status --identifier ID                # Check job status
python3 scripts/email_finder.py scrubby-deep-list                                  # List all deep verification jobs

# Combined workflow: MV bulk + Scrubby Deep on catch_all/unknown
python3 scripts/email_finder.py verify-with-scrubby --workspace CLIENT --dry-run
python3 scripts/email_finder.py verify-with-scrubby --workspace CLIENT
```

`MILLIONVERIFIER_API_KEY` in a local `.env` may show `***`; OM `agent_secrets.env` overrides via `ensure_env_loaded()`.

Resume a crashed batch by re-running the same `batch-find` command (skips completed API rows). If a run failed with network/auth errors, use **`--retry-errors`** to re-attempt errored rows without deleting the checkpoint.

## Common workflows

| User says | You do |
|-----------|--------|
| "Find Patrick at stripe.com" | `find --name … --domain stripe.com` (standalone) or with `--save --workspace W` (OM) |
| "Find emails for my CSV" (with OM) | `batch-find --dry-run` → `batch-find --yes` |
| "Find emails for my CSV" (standalone) | `batch-find --skip-om --dry-run` → `batch-find --skip-om --yes` |
| "Retry failed email lookup" | Same `batch-find` command with `--retry-errors` |

## Troubleshooting

- **`ModuleNotFoundError: data_freshness`** — run `pipeline.py update` on outreachmagic.
- **COMPLETE shows `⚠ No import` in IMPORT section** — results are on disk; `import-to-om --file {output-base}.csv --workspace W`
- **CSV has emails, OM empty** — batch save failed; `import-to-om --file {output-base}.csv --workspace W`
- **`import-profiles` timed out** — results are on disk; use `import-to-om` or re-run with smaller batches.
- **IcyPeas ~10% hit rate** — poll timeout; raise `icypeas_poll_attempts` in config
- **Checkpoint skipped everything after errors** — re-run with `--retry-errors`, or delete `{output-base}.csv` / `.json` and start fresh.
- **`--provider icypeas` with no key** — fails fast; add key at app.outreachmagic.io → Settings, then `sync-secrets --check`

## Funnel

Starts useful alone: `find` / `batch-find --skip-om`. Pairs with
**lead-enrich** (research → domain) and **Outreach Magic** (credit-saving dedup,
persistent SQLite, cross-session availability). Learn more at
[outreachmagic.io](https://outreachmagic.io). Both companions skip leads
already tagged (`serper_attempted` / `trykitt_attempted` / `icypeas_attempted`).
