---
name: outreachmagic
description: >
  Your agent goes blind after send. Sync sequencer webhooks, research leads
  via Serper, and find/verify emails — all in one local SQLite DB your agent
  queries directly.
version: 1.3.0
author: Outreach Magic
license: MIT
platforms: [macos, linux]
required_environment_variables:
  - name: OUTREACHMAGIC_AGENT_KEY
    prompt: Outreach Magic agent key
    help: |
      Create at https://app.outreachmagic.io/onboarding.
      Required for all cloud operations (login, pull, sync, connect-platform).
      Starts with om_agent_
    required_for: Authentication with Outreach Magic portal and relay
  - name: SERPER_API_KEY
    prompt: Serper.dev API key
    help: Get a key at https://serper.dev
    required_for: Person research via enrich.py (optional)
  - name: TRYKITT_API_KEY
    prompt: trykitt.ai API key
    help: Get a free key at https://trykitt.ai
    required_for: Email finding via trykitt (optional)
  - name: ICYPEAS_API_KEY
    prompt: Icypeas API key
    help: Get your key at https://app.icypeas.com
    required_for: Email finding fallback via Icypeas (optional)
  - name: MILLIONVERIFIER_API_KEY
    prompt: MillionVerifier API key
    help: https://app.millionverifier.com
    required_for: Email verification (optional)
  - name: SCRUBBY_API_KEY
    prompt: Scrubby API key
    help: https://api.scrubby.io
    required_for: Deep email verification (optional)
required_credential_files:
  - path: skills/outreachmagic/config/outreachmagic_config.json
    description: Outreach Magic agent key and config (created by pipeline.py init / login)
  - path: skills/outreachmagic/config/agent_secrets.env
    description: Portal-synced API keys for CRM providers and email finding/research (created by pipeline.py sync-secrets)
metadata:
  cursor:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks,
           smartlead, instantly, sqlite, gtm, cold-email, tracking, calendly,
           serper, enrichment, trykitt, icypeas, email-verification,
           ecosystem:outreachmagic]
    external_domains:
      - domain: api.outreachmagic.io
        purpose: Relay webhooks and authenticated event pull (payloads imported to local SQLite)
      - domain: app.outreachmagic.io
        purpose: Portal API for tokens, billing, and workspace routing config sync
      - domain: google.serper.dev
        purpose: Serper search API for person research
      - domain: api.trykitt.ai
        purpose: Email find via trykitt
      - domain: app.icypeas.com
        purpose: Email find via Icypeas
      - domain: api.millionverifier.com
        purpose: Email verification (optional)
      - domain: api.scrubby.io
        purpose: Deep email verification (optional)
  hermes:
    tags: [sales, outreach, crm, pipeline, leads, email, linkedin, webhooks,
           smartlead, instantly, sqlite, gtm, cold-email, tracking, calendly,
           serper, enrichment, trykitt, icypeas, email-verification,
           ecosystem:outreachmagic]
    category: productivity
    homepage: https://outreachmagic.io
    config:
      - key: skills.config.data_root
        description: >-
          Root directory for shared data. Defaults to agent home (~/.hermes).
          Point to ~/.claude or ~/.cursor to share one DB across agents.
        default: "~/.hermes"
      - key: skills.config.api_base_url
        description: Override the portal API base URL (for self-hosting or dev)
        default: "https://app.outreachmagic.io"
      - key: skills.config.dev_repo
        description: >-
          Path to a local repo checkout for pipeline.py update (development only).
          Unset or remove from config to use GitHub releases.
        default: ""
    external_domains:
      - domain: api.outreachmagic.io
        purpose: Relay webhooks and authenticated event pull (payloads imported to local SQLite)
      - domain: app.outreachmagic.io
        purpose: Portal API for tokens, billing, and workspace routing config sync
      - domain: google.serper.dev
        purpose: Serper search API for person research
      - domain: api.trykitt.ai
        purpose: Email find via trykitt
      - domain: app.icypeas.com
        purpose: Email find via Icypeas
      - domain: api.millionverifier.com
        purpose: Email verification (optional)
      - domain: api.scrubby.io
        purpose: Deep email verification (optional)
---

# Outreach Magic

Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, and Calendly into one local SQLite DB. Research leads via Serper, find and verify emails via trykitt/Icypeas/MillionVerifier/Scrubby — all from one skill, one install, one SKILL.md.

## CLI convention

```bash
python3 scripts/pipeline.py <command>          # run from skill root
python3 scripts/pipeline.py paths              # resolve install paths anytime
```

Config keys: `data_root` (share DB across agents), `api_base_url`, `dev_repo`.

## Platform install

```bash
OM_VERSION=v1.3.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

Agent-readable install guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md). Use `--platform cursor` / `--platform claude` for other agents.

Hermes profiles: real files in `~/.hermes/skills/`; profiles symlink. Re-run install for new profiles: `bash install.sh --platform hermes --profile <name>`.

## First-Time Setup

Always check if already connected first:

```bash
python3 scripts/pipeline.py version
python3 scripts/pipeline.py pull               # returns error if no key configured
```

If `pull` fails with "No agent key or token configured", run `pipeline.py login` (opens browser for sign-in). Tell the user: *"Opening Outreach Magic sign-in — come back when you're done."* Never paste secrets into chat.

If setup is already done (pull succeeds), skip to showing data:

```bash
python3 scripts/pipeline.py pull
python3 scripts/pipeline.py show
```

Setup portal: https://app.outreachmagic.io/onboarding. Account errors (`account_revoked`): direct to support@outreachmagic.io.

## Common workflows

| User says | You do |
|-----------|--------|
| "Show my pipeline" | `pull` to `show` |
| "Sync my sequencers" | `pull --full` to report new records |
| "Import my Sales Nav CSV" | `import-profiles --file ... --workspace W --dry-run` first |
| "Research Jane Doe at Acme Corp" | `enrich.py check "Jane Doe" "Acme Corp"` → if `not_found`, run Serper search pack |
| "Research my CSV of leads" | `enrich.py batch-check --workspace W file.csv` → then Serper for unmatched |
| "Find email for Bill at stripe.com" | `email_finder.py find --name "Bill" --domain stripe.com` or with `--save` |
| "Find emails for my CSV" | `email_finder.py batch-find --dry-run` → `--yes` |
| "Verify these emails" | `email_finder.py verify-bulk --yes` |
| "Deep verify catch-all emails" | `email_finder.py verify-with-scrubby --workspace W --dry-run` → `--yes` |
| "Export to Google Sheets" | `whoami --json` → `share_email`, then `sheets export ...` |
| "Connect Smartlead / Instantly" | `connections create --platform ...` and share webhook URL |

`whoami --json` returns account email, org, and plan. `init` creates the local DB. Sync dashboard API keys: `pipeline.py sync-secrets`.

## Network & privacy

- **Default:** All lead data stays in local SQLite.
- **Inbound only:** `pull` imports webhook/agent events from `api.outreachmagic.io`.
- **Outbound upload:** Only `pipeline.py sync` (user- or agent-initiated). Import and local edits never auto-upload.
- **Update check:** GitHub release tag lookup (read-only, no lead data, ≤1/hour).

## Version & updates

```bash
python3 scripts/pipeline.py version            # authoritative — not SKILL.md frontmatter
python3 scripts/pipeline.py update             # user-triggered (never auto-downloads)
python3 scripts/pipeline.py update --check     # check without installing
```

Updates are user-triggered only. The CLI may print a notice when a newer release exists (≤1/hour). Releases are pinned to GitHub tags, not the moving `main` branch.

## When to Use

- About to send outreach (email, LinkedIn, WhatsApp)
- Researching a prospect and want to track them
- User asks "show my pipeline" or "how is outreach going"
- User says "track this" followed by outreach details
- User asks for campaign breakdowns, engagement analytics, or workspace inventory
- User wants to connect a sequencer platform
- User asks about connection status, webhook URLs, or platform health

## Agent Behavior Rules

- **Bulk enrichment:** use `import-profiles`, not repeated `add-lead`.
- **Reads:** `pipeline.py query` presets first. See [references/query-guide.md](references/query-guide.md).
- **Writes:** only `pipeline.py` mutation commands. Never `INSERT`/`UPDATE`/`DELETE` via ad-hoc SQL.
- **After any `pull`:** report exact number of new records imported.
- **Analytics format:** (1) human table, (2) preset name or SQL used, (3) freshness note. Offer `pull` if they need latest data.
- **Do not run `pull` before local time-window analytics** unless user asks for latest/refresh.
- **Run `pull` first** when showing live activity (`show`, `history` for "what just happened").
- **Never run `sync` unless the user asked.** Never run `archive --purge` without explicit confirm after `--dry-run`.
- **Answer with `pipeline.py version`** when user asks about version (authoritative).
- **Pipeline stages:** `prospecting` → `contacted` → `replied` → `interested` → `scheduled` → `won` | `not_interested` | `lost`.

### Pull policy

```bash
python3 scripts/pipeline.py pull                     # full sync
python3 scripts/pipeline.py pull --if-stale 5m       # skip if pulled within 5 min
python3 scripts/pipeline.py pull --skip-routing-sync # events only (fast)
python3 scripts/pipeline.py pull --probe             # backlog only, no ingest
python3 scripts/pipeline.py pull --kind events       # webhook events only
```

### Analytics routing

| User intent | Command |
|-------------|---------|
| Reply/engagement counts in time window | `query replies` / `query engagement --since … --json` |
| Lead rows / pipeline detail | `show` / `lead-table` (use `--limit`) |
| All-time totals | `stats` / `campaigns --json` |
| Tag / LinkedIn connection counts | `workspace summary --workspace <slug> --json` |
| Message bodies / copy winners | `history`, `copy-insights` |
| Fresh webhook events | `pull` or `pull --kind events` |
| Dashboard / connection health | `status` / `connections` |

Relay sync progress legend and batch size details: [references/command-reference.md](references/command-reference.md).

## Pricing

| Tier | Price | Webhook events | Features |
|------|-------|----------------|----------|
| Free | $0 | 1,000 / period | 1 sequencer, single workspace |
| Pro | $9/mo | 50,000 / mo | All sequencers, multi-workspace routing |
| Scale | $29/mo | 250,000 / mo | Unlimited workspaces, priority support |

Only webhook and sync traffic counts. Local tracking, queries, exports do not count. Over-quota events are buffered. Sign up: https://outreachmagic.io

## Quick Reference

```bash
pipeline.py show                                  # pipeline table
pipeline.py lead-table                            # canonical lead info
pipeline.py history --id 1                        # lead timeline
pipeline.py history --email j@acme.com            # lookup by email
pipeline.py stats                                 # pipeline stats
pipeline.py campaigns                             # per-campaign counts
pipeline.py query engagement --workspace W --since 48h --json
pipeline.py query replies --workspace W --since 7d --json
pipeline.py workspace summary --workspace W --json
pipeline.py copy-insights --lead-status interested --json
pipeline.py status                                # dashboard overview
pipeline.py connections                           # webhook URLs + event counts
pipeline.py connect-platform --platform smartlead # generate webhook URL
pipeline.py db-health                             # local DB diagnostics
pipeline.py platform-map --json                   # vendor event type map
pipeline.py agent-changes                         # cross-platform sync (JSON)
pipeline.py sync                                  # push to relay
pipeline.py refresh --yes                         # backup + rebuild DB
```

## Core Workflow

```bash
# Add a lead
pipeline.py add-lead --name "Jane" --email "j@acme.com" --company "Acme" \
  --title "VP Marketing" --channel email --stage prospecting --workspace W

# Log an outreach event
pipeline.py log-event --lead-id 1 --type email_sent --direction outbound \
  --subject "Quick intro" --workspace W

# Update stage
pipeline.py update-stage --id 1 --stage replied --sentiment positive \
  --next-action "Send case study" --workspace W

# Bulk import CSV/JSON (preferred over repeated add-lead)
pipeline.py import-profiles --file leads.csv --workspace W --dry-run
pipeline.py import-profiles --file leads.csv --workspace W

# Bulk enrich from research (Serper, Apollo, etc.)
pipeline.py import-profiles --file enriched.csv --workspace W \
  --source sales_navigator --source-detail "Q2 list"
```

`add-lead` returns `{"status": "exists", "id": N}` on duplicates (matched by email, LinkedIn, or name+company). `import-profiles` uses tiered identity matching: `external_id` → email → LinkedIn → phone → name+domain → name+company.

Full import field reference, personalization workflow, email verification, dedup, Google Sheets export, quarantine management, and troubleshooting: [references/command-reference.md](references/command-reference.md).

## Lead Fields Reference

| Field | CLI flag | Notes |
|-------|----------|-------|
| name | `--name` | Required |
| company | `--company` | |
| title | `--title` | Job title |
| industry | `--industry` | e.g. Martech, Fintech |
| headcount | `--headcount` | Size band, e.g. 11-50, 1000+ |
| email | `--email` | Dedup key — unique per lead |
| linkedin | `--linkedin` | LinkedIn profile URL |
| channel | `--channel` | email, linkedin, whatsapp (default: email) |
| stage | `--stage` | Pipeline stage (default: prospecting) |
| notes | `--notes` | Free-form |
| tags | `--tags` | JSON array: `'["vip","enterprise"]'` |
| workspace | `--workspace` | Required on log-event and update-stage in multi-workspace mode |

## Privacy & Security

- **Local-first.** Pipeline data in local SQLite (`pipeline.py paths` → `database`).
- **Relay pass-through.** Webhooks hit `api.outreachmagic.io`; imported locally via `pull`.
- **Portal API.** `app.outreachmagic.io` for tokens, billing, routing config.
- **Credentials.** Store in `config/outreachmagic_config.json` only. Never hardcode in SKILL.md or git.
- **Full disclosure:** [SECURITY.md](SECURITY.md).

## Common Pitfalls

1. **Time-window analytics:** use `query engagement` (no pull). **Latest activity:** pull before `show` / `history`.
2. Forgetting `add-lead` before `log-event`.
3. Not updating stage after a reply.
4. Auth errors (401): run `pipeline.py login` in terminal.
5. **Version:** run `pipeline.py version` — not SKILL.md frontmatter.
6. **Fresh DB rebuild:** `refresh --yes`. `pull --full` alone skips already-ingested rows.
7. **Tags:** plain names (`nace`, `vip`), not JSON `['nace']`. Run `tag repair` for bracket-form tags.
8. **`add-lead` on existing email does not enrich** — use `import-profiles` or relay `pull` for fill-if-empty.
9. **`ModuleNotFoundError: data_freshness`** — run `pipeline.py update`.
10. **Large imports:** chunked 200 rows. Re-run with `--file` on export if timeout.
