---
name: outreachmagic
description: >
  Your agent goes blind after send. Sync sequencer webhooks, research leads
  via Serper, and find/verify emails — all in one local SQLite DB your agent
  queries directly.
version: 1.5.0
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

Requires Python 3.10+. Stock macOS ships `python3` at 3.9 — if commands fail with a
version error, use the Homebrew interpreter directly (e.g. `python3.12 scripts/pipeline.py ...`).
`install.sh`'s `_find_python()` already detects and uses the right interpreter automatically.

Config keys: `data_root` (share DB across agents), `api_base_url`, `dev_repo`.

## Platform install

```bash
OM_VERSION=v1.5.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

Agent-readable install guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/v1.3.0/AGENTS-INSTALL.md). Use `--platform cursor` / `--platform claude` for other agents.

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
| "Find email for Bill at stripe.com" | `email_finder.py find --name "Bill" --domain stripe.com` (saves by default, use `--no-save` to skip or `--dry-run` to preview) |
| "Find emails for my CSV" | `email_finder.py batch-find --dry-run` → `--yes` |
| "Verify these emails" | `email_finder.py verify-bulk --yes` |
| "Deep verify catch-all emails" | `email_finder.py verify-with-scrubby --workspace W --dry-run` → `--yes` |
| "Export to Google Sheets" | `whoami --json` → `share_email`, then `sheets export ...` |
| "Connect Smartlead / Instantly" | `connections create --platform ...` and share webhook URL |
| "Push leads to our CRM" | `crm-sync sync --workspace W --dry-run` first, then without `--dry-run` (GHL / HubSpot) |
| "Open the dashboard" | `dashboard` — local web UI at http://127.0.0.1:8765 (deliverability, pipeline, campaigns; stage/enrich/log-event actions) |

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
- **Never run `sync` unless the user asked.** Never run `archive --purge` without explicit confirm after `--dry-run`. (A user clicking the dashboard's "Push to relay" button counts as asking.)
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
pipeline.py phone list --lead-id 42                # numbers on a lead (or --company-id)
pipeline.py phone add --lead-id 42 --phone "612-555-0143" --label mobile --source apollo
pipeline.py status                                # dashboard overview
pipeline.py connections                           # webhook URLs + event counts
pipeline.py connect-platform --platform smartlead # generate webhook URL
pipeline.py db-health                             # local DB diagnostics
pipeline.py platform-map --json                   # vendor event type map
pipeline.py campaign-map list                     # show routing rules
pipeline.py campaign-map add --platform P --workspace W  # add routing rule
pipeline.py campaign-map conflicts                # list name_exact rows shadowing a broader rule
pipeline.py campaign-map deactivate --id MAP_ID   # soft-deactivate one stale routing row
pipeline.py campaign-map reconcile --dry-run      # preview re-routing already-ingested leads/events
pipeline.py agent-changes                         # cross-platform sync (JSON)
pipeline.py crm-sync sync --workspace W --dry-run # preview CRM push (GHL/HubSpot)
pipeline.py crm-sync sync --workspace W           # push leads to CRM
pipeline.py crm-sync sync --workspace W --max-age 30d  # only leads active in last 30 days
pipeline.py sync                                  # push to relay
pipeline.py dashboard                             # local web dashboard (http://127.0.0.1:8765)
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

## Company-only lists (Google Maps / directory scrapes)

A scraped business list has no people in it. Import it with an explicit
record type so the rows are never mistaken for contacts:

```bash
python3 scripts/pipeline.py import-profiles --file dealers.csv \
  --record-type company_placeholder --source google_maps
```

`company_placeholder` rows are real records — taggable, personalizable, linked
to a company — but they are **excluded from the contacts list, email-finder
targeting and CRM sync**, because there is no person to contact yet. Once
research turns up real contacts at those companies:

```bash
python3 scripts/pipeline.py record-type --resolve            # dry run
python3 scripts/pipeline.py record-type --resolve --yes      # execute
```

Stubs that were never sent to are deleted; stubs with outreach history are kept
and stamped `superseded_at`, so the record of what was sent survives.

Without `--record-type`, import auto-detects only when the name carries a clear
business signal (`Inc`, `LLC`, `Motors`, a marque, an `&`, a digit). That is
deliberately conservative: a sole trader whose company is their own name looks
identical to a scraped business, and misclassifying a real person hides them
from outreach. **Pass the flag when you know the list is companies.**

That whitelist misses plenty of real dealerships — `K L M of Riverton`,
`Corwin Vance`, `Brennt of Ashgrove` carry no business token at all, and 30 rows
of one 773-row manufacturer import landed as fake people because of it. Two
things now cover that:

- **Known directory sources skip the whitelist.** When `--source` is a
  manufacturer locator or a Maps scrape (`mercedes-benz-official`, anything
  ending `-official`, `google_maps` — see `COMPANY_DIRECTORY_SOURCES`), a row
  whose name IS its company with no email/title/LinkedIn is a company, whatever
  the name looks like.
- **Email finding refuses them anyway.** A `contact` row whose name equals its
  company and that has no email, title or LinkedIn is excluded from finder
  targeting, so a missed classification costs no credits.

## Personalization scope: contact or company?

A personalization field belongs to **exactly one scope**. Lead-scoped values are
per person; company-scoped values are shared by every contact at that company.
Writing to the wrong one is silent, so say which you mean:

| You want | Column name / command |
|---|---|
| Per-contact value | `personalized_<field>` · `personalize-set` |
| Per-company value | `company_personalized_<field>` · `company-personalize-set` |

**Before reusing a field name, look at what it already holds:**

```bash
python3 scripts/pipeline.py personalization-fields --values
#   lead     icp_segment    2857 entities   9 values
#            mercedes franchise    971
#            independent dealer    673
#            office_owners         275     ← another campaign's values
```

The first write to a scope claims the name; a later write to the other scope is
rejected rather than quietly landing in a second table. Fields that predate the
registry were backfilled from what was on disk.

`import-profiles` reports where every column went, in
`summary.personalization_routing`. Anything listed under
`personalization_routing_guessed` was routed by the old name-shape heuristic
because nothing had claimed it — worth a look before you trust it.

**Company personalization is one value per company, but a sheet is per lead.**
If the same company arrives with several values for one field, the import
aborts and names them rather than letting the last row win:

```bash
pipeline.py import-profiles --file segments.csv --company-conflict last-wins
```

## Updating personalization on existing leads

A sheet of `lead_id` + personalized columns is a personalization update, not an
import. Say so, or the merge value becomes the person's name — this is how
"Brian Williams" once became "Brian":

```bash
python3 scripts/pipeline.py import-profiles --file segments.csv \
  --mode personalization-only --workspace W
```

Rows must match an existing lead. Nothing is created, no profile column is
touched, and unmatched rows come back as a list instead of becoming new leads.
Validation runs first on every import (not just `--dry-run`) and reports rows
that won't match, values that would be overwritten, and company conflicts.

## Bulk input: always `--file`

Every command that takes a batch accepts `--file` (and `--json -` for stdin).
Passing a *path* to `--json` used to fail as `JSONDecodeError: Expecting value`,
which reads like bad data rather than the wrong flag:

```bash
pipeline.py personalize-set --batch --file segments.json
pipeline.py company-personalize-set --batch --file companies.json
pipeline.py tag bulk --workspace W --tags campaign-x --file lead_ids.txt
```

## Test data

Mark synthetic rows at the door so they can never reach a campaign:

```bash
pipeline.py import-profiles --file fixtures.csv --test --workspace W
pipeline.py test-leads suggest    # real-looking leads that may be test data
pipeline.py test-leads set --lead-ids 1,2,3
```

Test leads are excluded from lists, counts, exports and bulk actions by default
(`--test all` to include, `--test only` to see just them) — the same discipline
as suppression. Note that `test-leads suggest` is a **report**: a real contact
imported under a test-named source is a naming problem, not a test lead, and
flagging it would hide them from every export.

## Contact sourcing (`find-contacts`)

Turns companies with no people into companies with people: the staff page is
fetched to markdown, a regex pass pulls name+title pairs, and only the pages it
could not crack come to you.

```bash
python3 scripts/pipeline.py icp set --workspace acme --name decision-makers \
  --whitelist "general manager,service manager,owner" \
  --blocklist "assistant general manager"

# Always dry-run first: reports targets and worst-case credits, spends nothing.
python3 scripts/pipeline.py find-contacts --workspace acme --icp decision-makers --dry-run
python3 scripts/pipeline.py find-contacts --workspace acme --icp decision-makers --max-fetches 50

# The tail the regex pass could not crack:
python3 scripts/pipeline.py contact-extract-pending --workspace acme --limit 20 --json
python3 scripts/pipeline.py contact-apply --batch --workspace acme --json '[...]'

# Or hand a page to a person: every candidate, with the ICP's verdict on each.
python3 scripts/pipeline.py contact-review --workspace acme --icp decision-makers
python3 scripts/pipeline.py contact-apply --company-id 83544 --contact-ids 3,7 --workspace acme
python3 scripts/pipeline.py contact-review --company-id 83544 --none-of-these --workspace acme
```

**Always `--dry-run` before a real run, and always pass `--max-fetches`.** Firecrawl bills per page and the credits do not roll over. The dry run counts cache misses, so its estimate stays honest on a second pass, and `--reparse` re-extracts cached pages for zero credits when the ICP changes.

**Spawn a subagent per batch. This is not a style preference.**

`contact-extract-pending` returns whole page bodies — roughly 8–10k tokens each,
so a batch of 20 is ~200k. Read into the main conversation that is a 10M-token
run and a context you cannot recover; read into a subagent it dies with the
batch. For anything over ~5 companies:

> One subagent per batch of 20. The subagent calls `contact-extract-pending`,
> extracts, calls `contact-apply --batch`, and reports one line:
> `"batch 3/34: 31 contacts, 4 no-data"`. The main thread never sees markdown.

676 companies is ~34 such batches and ~34 short lines in the main thread.

**Applying is idempotent.** A contact is keyed on name + company domain, never
on the title (titles get re-scraped differently), so re-running a batch matches
the same leads instead of duplicating them. `--dry-run` reports what would be
attached and writes nothing. The ICP blocklist is enforced on apply even though
you were handed it — a "never contact this person" rule should not depend on
having been honoured.

**`contact-review` is the human surface, and you should not pre-empt it.** It
prints every person on the page — the ones the ICP kept and the ones it refused,
with the reason — and nothing is marked. Don't summarise it down to a
recommendation: the rejects are why someone is looking, and the whole point is
to find out where the ICP is wrong. Candidate ids are positions in the cached
page, so they survive an ICP edit but not a re-fetch; pass the payload's
`content_hash` to `contact-apply --content-hash` when the two are not seconds
apart. `--none-of-these` is a decision that gets recorded, not a skip.

## Lead Fields Reference

| Field | CLI flag | Notes |
|-------|----------|-------|
| name | `--name` | Required |
| record_type | `--record-type` | `contact` (default) or `company_placeholder` |
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
