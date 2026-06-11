---
name: lead-enrich
description: >
  Research people and enrich lead profiles using Serper.dev Google Search.
  Checks Outreach Magic first to avoid wasting API credits on existing leads.
  Extracts company domain, website, and LinkedIn URL via the agent's built-in
  model — no external LLM API needed. Saves results locally via the
  outreachmagic skill. For email finding, use the email-finder companion skill.
version: 2.1.7
author: Outreach Magic
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: SERPER_API_KEY
    prompt: Serper.dev API key
    help: Get a key at https://serper.dev
    required_for: Google Search during person/company research
  - name: OUTREACHMAGIC_AGENT_KEY
    prompt: Outreach Magic agent key
    help: Create at https://app.outreachmagic.io/onboarding (starts with om_agent_)
    required_for: outreachmagic dedup checks and saving enriched leads
metadata:
  hermes:
    tags: [sales, outreach, crm, enrichment, leads, research, linkedin, serper]
    related_skills: [outreachmagic, email-finder]
    external_domains:
      - domain: google.serper.dev
        purpose: Google Search API for company discovery and LinkedIn profile lookup
      - domain: api.outreachmagic.io
        purpose: Via outreachmagic skill — save enriched profiles to local SQLite
---

# Lead Enrich — Person Research for Outreach Pipelines

Research a person (name + company) and save structured enrichment back to
Outreach Magic. Uses **Serper.dev** for search, the **agent's built-in model**
for extraction, and the **outreachmagic** skill for persistence.

> **🪙 Credit-saving:** always checks outreachmagic *first*. If the lead already
> has **LinkedIn + email** at the **same company**, no Serper credits are spent.
> LinkedIn without email skips Serper — use **email-finder** for trykitt find when
> the user wants an address. Email-only records still get LinkedIn searches
> (1–2 Serper credits). Name matches at a different company return `ambiguous`
> so APIs are not skipped by mistake.

## Prerequisites

### 1. Serper.dev API key

Sign up at https://serper.dev → get API key.

**Hermes (recommended):** add to `~/.hermes/.env` (see `default.env` in this skill
for a copy-paste template):

```bash
SERPER_API_KEY=your_serper_key_here
```

`enrich.py` loads `~/.hermes/.env` automatically (also checks `default.env` in the
same folder). Hermes forwards these vars when the skill is loaded.

**Alternatives:** `config.json` (`serper_api_key`) or `export SERPER_API_KEY=...`.

### 2. outreachmagic skill

Required for saving results. Install outreachmagic and set the agent key in the
same Hermes env file:

```bash
OUTREACHMAGIC_AGENT_KEY=om_agent_your_key_here
```

**Hermes:** Install skills under `~/.hermes/skills/`; profile dirs use symlinks only (see outreachmagic `install.sh` or `docs/hermes-skills-layout.md`). `enrich.py` finds outreachmagic at `~/.hermes/skills/outreachmagic/`.

### 3. Email finding (email-finder skill)

Email find is **not** in lead-enrich v2+. Install **email-finder** with outreachmagic using the platform install guide: [install-companions.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/install-companions.md) (Hermes, Cursor, Claude).

After Serper enrichment saves `company_domain`, run:

```bash
python3 ~/.hermes/skills/email-finder/scripts/email_finder.py find --name "Jane Doe" \
  --domain acme.com --linkedin "https://linkedin.com/in/janedoe" --save
```

See `references/email-finder.md` and the email-finder skill docs.

**Workspace rollups (no Serper credits):** after saving leads, use outreachmagic
`workspace summary --workspace <slug> --json` for tag counts and LinkedIn
connection accepted per sender. On large workspaces (>2,000 leads), add
`--tags-only` for faster tag counts. Local DB only — pull optional.

## Common workflows

| User says | You do |
|-----------|--------|
| "Research this person" | Serper search → save via `import-profiles` |
| "Enrich my CSV" | `batch-check` / import → enrich missing fields → save to OM |
| "Find their email too" | After enrich, hand off to **email-finder** |

Sales Nav / Vayne CSVs: use outreachmagic `import-profiles --file …` (auto-detects columns).

## CSV / award-list workflow (preferred for 10+ people)

Paths like `input/awards.csv` are relative to your **workspace directory** (where the agent runs the command), not the skill install folder.

```bash
# 0 credits — dedup entire file first (auto-stamps serper_attempted on complete rows)
python3 scripts/enrich.py batch-check --workspace your_workspace input/awards.csv

# Re-run dedup skipping leads already tagged serper_attempted
python3 scripts/enrich.py batch-check --workspace your_workspace --skip-tagged input/awards.csv

# Serper only for rows that need LinkedIn/domain (skip team_award, exists_linkedin_*, skipped_serper_attempted)

# After research — patch title/industry only (0 Serper credits)
python3 scripts/enrich.py backfill --fields title,industry --workspace your_workspace input/patch.csv
```

`batch-check` accepts `.json` or `.csv`. `backfill` requires `email` or `linkedin` per row; uses chunked `import-profiles` via `companion_common` (200 rows/chunk, up to 300s/chunk; fills empty fields; add `--overwrite` to replace).

## When to Use

- User says "research this person" / "look up Jane Doe at Acme"
- User wants to enrich a list of prospects before outreach
- User asks "do we already have this lead?" before researching
- User provides a CSV/JSON of names and companies to enrich in bulk
- User mentions Serper, lead enrichment, or person research

## Agent Behavior Rules (Important)

1. **Dedup first, always.** Before any Serper API call, run `enrich.py check`. If
   the lead has **LinkedIn + email** at the same company, skip Serper entirely.
   If LinkedIn exists but **no email**, skip Serper and offer **email-finder** when
   the user wants an address. Never spend Serper credits on leads already complete
   in outreachmagic.
2. **Serper only.** Prefer `enrich.py serper-search --query "..."` (stdlib HTTP,
   key from config/env). Or use `curl` with `$SERPER_API_KEY` — never embed the
   key in chat logs. Never scrape Google or LinkedIn directly.
3. **Built-in model only.** You (the agent) extract JSON from Serper results.
   No external LLM APIs (no Gemini, no OpenAI) — your own reasoning is the
   extraction engine.
4. **Complete research before saving.** Run the full search ladder first, then save once.
5. **Save via outreachmagic.** Use `import-profiles` for leads with LinkedIn.
   Always append **`serper_attempted`** to tags on save (included automatically in
   `map-to-om` output). For leads without LinkedIn but with a known `lead_id`, stamp
   the tag via `stamp-attempted` or `import-profiles` with `id` + tags — do not rely
   on notes alone. For read-only dedup checks use `pipeline.py query` or
   `enrich.py check` — never raw `INSERT`/`UPDATE`. Never run both save paths for
   the same person.
6. **Tag after enrichment.** Every lead that goes through Serper must get
   `serper_attempted` on save. Prevents re-processing on future runs.
7. **Check tag before Serper.** Before spending Serper credits, check for
   `serper_attempted` (via `enrich.py check --skip-tagged` or `skip_reason` in
   check output). If present and LinkedIn is still empty, skip unless the user
   explicitly wants a retry (e.g. stale >30 days).
8. **Transparency.** Show which Serper queries ran, confidence, and what was
   saved. The user should see exactly where their credits went.
9. **Batch wisely.** Cap at 50 people per run. For CSV/award lists run **`batch-check` once** on the whole file (JSON or CSV) before any Serper. Process Serper only for statuses that need LinkedIn/domain. Skip `team_award`, `exists_linkedin_email`, and `skipped_serper_attempted` rows.
10. **Email finding — use email-finder.** After enrichment saves `company_domain`,
   hand off to **email-finder** (`email_finder.py find --save`). See `references/email-finder.md`.
   Never fabricate or pattern-guess emails in this skill.

## Quick Start

```bash
# Single person (most common)
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"
# → if "not_found", proceed with Serper search pack below

# With workspace (associates lead with your pipeline workspace)
python3 scripts/enrich.py check --workspace your_workspace "Jane Doe" "Acme Corp"

# Batch from JSON or CSV (run this before Serper on lists)
python3 scripts/enrich.py batch-check --workspace your_workspace input/people.json
python3 scripts/enrich.py batch-check --workspace your_workspace --skip-tagged input/awards.csv

# Stamp serper_attempted after failed LinkedIn lookup (when lead_id is known)
python3 scripts/enrich.py stamp-attempted --workspace your_workspace --lead-ids 42,43 \
  --notes "No LinkedIn found via Serper"

# Backfill title/industry on existing leads (linkedin or email required)
python3 scripts/enrich.py backfill --fields title,industry input/patch.csv

# Update skill safely from GitHub release (checksum-verified)
python3 scripts/enrich.py update --check
python3 scripts/enrich.py update
```

`update` verifies SHA256 checksums from `update-manifest.json` before replacing
files. If checksums are missing or mismatched, the update aborts.

## Core Workflow

### Phase 1 — Dedup Check (0 credits)

For each person, run:

```bash
# Without workspace (org-wide lookup)
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"

# With workspace (scoped to your pipeline workspace)
python3 scripts/enrich.py check --workspace your_workspace "Jane Doe" "Acme Corp"
```

Output per person:

| Status | Meaning | Action |
|--------|---------|--------|
| `exists_linkedin_email` | Same company, LinkedIn + email | Skip Serper and email APIs |
| `exists_linkedin_no_email` | Same company, LinkedIn, no email | Skip Serper → **email-finder** if user wants email |
| `exists_no_linkedin_email` | Same company, email, no LinkedIn | LinkedIn Serper queries only |
| `exists_no_linkedin` | Same company, neither | LinkedIn Serper queries only |
| `skipped_serper_attempted` | Has `serper_attempted` tag, no LinkedIn | Skip Serper — already tried |
| `ambiguous` | Name match, company mismatch | Run full Serper pack — do not skip |
| `not_found` | No match | Run full Serper search pack |
| `team_award` | Team/group row (no individual) | Skip Serper — tag `team_award`, add contact note |
| `dedup_disabled` | `dedup_before_search: false` in config | Run Serper as requested |

Check output includes `tags` and optional `skip_reason` (`has_linkedin` or
`skipped_serper_attempted`). Uses `batch-lead-lookup` (local, zero Serper credits).

### Phase 2 — Serper Search Pack

Only run for people who need it. **2–4 searches per person** depending on
result quality:

### Serper Credit Budget Estimator

- **Per person minimum:** `0` Serper credits (`exists_linkedin_email`).
- **Common path:** `2` credits (`2a` strict company + `2c` LinkedIn profile).
- **Per person hard max:** `4` credits when all fallbacks are needed (`2a` + `2b` + `2c` + `2e`).
- **Batch formula:** `min=0`, `max=4*N` where `N` is people in the run.
- **Batch cap example:** with `N=50`, hard max is `200` credits (worst case).

#### 2a. Company discovery — strict (always)

Preferred (no key in shell history):

```bash
python3 scripts/enrich.py serper-search --query '"Acme Corp" official website' --label company_discovery_strict
```

Or retry with a simpler query:

```bash
python3 scripts/enrich.py serper-search --query 'Acme Corp website' --label company_discovery_broad
```

#### 2b. Company discovery — broad (conditional)

Run only if 2a returns **no** organic results with an `http://` or `https://` link:

```bash
python3 scripts/enrich.py serper-search --query 'Acme Corp official website' --label company_discovery_broad
```
(Same template, unquoted company name.)

#### 2c. LinkedIn profile (always)

Build query (unquoted company — matches variant employer names in snippets):

```
site:linkedin.com/in {First Last} {up to 5 words of role} {Company Name}
```

Example:
```bash
python3 scripts/enrich.py serper-search \
  --query 'site:linkedin.com/in Jane Doe VP Marketing Acme Corp' \
  --label linkedin_profile
```

### Phase 3 — Model Extraction

Pass the formatted Serper results to yourself (the agent model) with this system
instruction:

```
You are a research assistant. The user message contains Serper.dev Google search
results in labeled sections. You do NOT have live web search — use only the pasted
blocks.

Task: for ONE specific person at ONE company, extract:
- company_domain: registrable hostname only (no path, no www), or empty string
- company_website: full https:// homepage URL if supported, or empty string
- linkedin_url: https://linkedin.com/in/… for the named person at this company, or empty string
- confidence: high | medium | low
- note: optional explanation of ambiguity or gaps

Rules:
- Every non-empty URL must appear in the Serper blocks (minor query-string normalization ok)
- Name on LinkedIn may differ from input (nickname vs legal name) — match on same human + employer
- Before accepting `linkedin_url`, verify match quality:
  - Extract first + last name tokens from `Full name: {full_name}` (ignore punctuation)
  - The chosen `/in/` result must have *both* tokens present in either the result `title` or `snippet`
  - If no `/in/` result meets the token requirement, return `linkedin_url` as an empty string and set `confidence` to `low`
- Company public site may use a different banner name than input — prefer official evidence
- When resolving `company_domain`:
  - Prefer `knowledgeGraph.website` when present
  - Reject common aggregators/registries (examples: `naceweb.org`, `usnews.com`, `wikipedia.org`, `niche.com`, `facebook.com`, `instagram.com`, `twitter.com`)
  - If `company_name` appears to be missing or matches the person name closely, return empty string
- Never fabricate URLs or slugs

Respond ONLY with a single JSON object (no markdown fences):
{"company_domain":"","company_website":"","linkedin_url":"","confidence":"medium","note":""}
```

Use this user message template:

```
### Target person
Full name: {full_name}
Stated role/title: {stated_role}
Company (as provided): {company_name}

### Search results
{formatted_serper_sections}

### Task
Return the JSON object described in the system instruction.
```

Then parse your own response: strip markdown fences, extract the JSON object.

**LinkedIn harvest fallback:** if `linkedin_url` is empty after extraction, scan
the raw Serper organic results for `/in/` URLs where the title contains both
first and last name tokens. Prefer matches whose snippet/title also mention the employer/company.

### Phase 4 — Save via outreachmagic

Map extracted fields to outreachmagic:

| Research field | outreachmagic field |
|----------------|---------------------|
| `full_name` | `name` |
| `stated_role` | `job_title` |
| `company_name` | `company` |
| `linkedin_url` | `linkedin` |
| `company_domain` | `company_domain` (structured) + optional `notes` |
| `company_website` | → `notes` |
| `confidence` | → `notes` |
| `note` | → `notes` |
| `tags` | `tags` (JSON array) |
| `import_name` | → `notes` prefix |

**If LinkedIn found:**

Every import from this skill sets `--source lead_enrich` and `--source-detail "lead-enrich"` by default.
If an `import_name` is provided, detail appends as `"lead-enrich/{import_name}"`.

```bash
# Org-wide (no workspace)
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --source lead_enrich --source-detail "lead-enrich" \
  --json '[{"name":"Jane Doe","company":"Acme Corp","job_title":"VP Marketing","linkedin":"linkedin.com/in/janedoe","company_domain":"acme.com","tags":["nace","serper_attempted"]}]'

# Scoped to a workspace
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --workspace your_workspace \
  --source lead_enrich --source-detail "lead-enrich" \
  --json '[{"name":"Jane Doe","company":"Acme Corp","job_title":"VP Marketing","linkedin":"linkedin.com/in/janedoe","company_domain":"acme.com","tags":["nace","serper_attempted"]}]'
```

**If no LinkedIn, no email:**
When `lead_id` is known (from `batch-check`), stamp attempt state — do not bury
failure only in notes:

```bash
python3 scripts/enrich.py stamp-attempted --workspace your_workspace --lead-ids 42 \
  --notes "No LinkedIn found via Serper"
```

Or via import-profiles when you also have name + company:

```bash
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --workspace your_workspace \
  --source lead_enrich --source-detail "lead-enrich/no-linkedin" \
  --json '[{"id":42,"name":"Jane Doe","company":"Acme Corp","tags":["nace","serper_attempted"],"notes":"No LinkedIn found"}]'
```

Without a `lead_id`, use `add-lead` with notes (last resort) or report unsaved.

### Email finding (email-finder skill)

After Phase 4 save, if the user wants an email and `company_domain` is known,
use the **email-finder** companion — not this skill:

```bash
python3 ~/.hermes/skills/email-finder/scripts/email_finder.py find \
  --name "Jane Doe" --domain acme.com \
  --linkedin "https://linkedin.com/in/janedoe" --save
```

See `references/email-finder.md` and email-finder's `email-finding-research.md`.

### Phase 5 — Report

Summarize per person:

```
Jane Doe @ Acme Corp
  ✅ Company: acme.com | https://acme.com
  ✅ LinkedIn: linkedin.com/in/janedoe
  🟢 Confidence: high
  💾 Saved to outreachmagic (lead #42)
  🔍 Serper: 2 queries
  📧 Email: use email-finder if needed
```

---

## Email-only mode

When contacts are **already enriched** (have `company_domain` + `linkedin` in CSV or
outreachmagic) but lack email:

1. Run `batch-check` — process only `exists_linkedin_no_email` (and optionally
   `not_found` rows that already have domain in the file).
2. **Skip Serper entirely** (0 Serper credits).
3. Run **email-finder** `batch-find` for each person with domain + LinkedIn.
4. Respect `trykitt_attempted` tag — email-finder skips rows already tagged.
5. Respect `serper_attempted` tag — lead-enrich skips rows already researched.

Useful after a prior enrichment pass or when importing a pre-researched list.

---

## Input Formats

### Single person

```
Research Jane Doe, VP Marketing at Acme Corp
```

Or with workspace:

```
Research Jane Doe at Acme Corp --workspace your_workspace
```

### Batch (JSON file or inline)

```json
{
  "people": [
    {"full_name": "Jane Doe", "company_name": "Acme Corp", "stated_role": "CEO"},
    {"full_name": "John Smith", "company_name": "Beta Inc"}
  ],
  "workspace": "your_workspace",
  "tags": ["nace"],
  "import_name": "NACE 2026 attendee"
}
```

Max 50 people per run.

---

## Config Reference

`config.json` (copy from `config.example.json`):

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `serper_api_key` | Yes* | — | Serper.dev API key (*or `SERPER_API_KEY` in `~/.hermes/.env`) |
| `serper_endpoint` | No | `https://google.serper.dev/search` | API endpoint |
| `outreachmagic_home` | No | auto-detect | Path to outreachmagic skill |
| `max_people_per_run` | No | 50 | Batch size limit |
| `dedup_before_search` | No | true | Check outreachmagic before Serper |
| `serper_num_results` | No | 10 | Results per Serper query |
| `serper_gl` | No | `us` | Country code |
| `serper_hl` | No | `en` | Language |

---

## Enrichment query patterns (SQL)

Use via `pipeline.py query --sql '…' --params '[…]' --json`.

**Leads that still need Serper** (no LinkedIn, not yet attempted):

```sql
SELECT l.id, l.name, l.company
FROM leads l
JOIN workspace_lead_tags n ON n.lead_id = l.id AND n.tag = ?
JOIN workspaces w ON n.workspace_id = w.id
WHERE w.slug = ?
  AND (l.linkedin_url IS NULL OR l.linkedin_url = '')
  AND l.id NOT IN (
    SELECT lead_id FROM workspace_lead_tags
    WHERE tag = 'serper_attempted' AND workspace_id = w.id
  )
```

Params: `["nace", "your_workspace"]`

**Enrichment attempted but failed** (retry-eligible when stale):

```sql
SELECT l.id, l.name, l.company, l.updated_at
FROM leads l
JOIN workspace_lead_tags s ON s.lead_id = l.id AND s.tag = 'serper_attempted'
JOIN workspaces w ON s.workspace_id = w.id
WHERE w.slug = ?
  AND (l.linkedin_url IS NULL OR l.linkedin_url = '')
  AND l.updated_at < datetime('now', '-30 days')
```

Params: `["your_workspace"]`

---

## What this skill does NOT do

- ❌ Call Outreach Magic person-research API (`/v1/person-research`)
- ❌ Use external LLM APIs (Gemini, OpenAI, etc.) for extraction
- ❌ Scrape HTML pages or LinkedIn directly
- ❌ Guess email addresses (use **email-finder** skill)
- ❌ Write raw SQL or ad-hoc DB mutations (use `import-profiles` / `add-lead`; reads via `pipeline.py query`)
- ❌ Upload to remote servers (local-only by default)

---

## Platform Support

| Platform | Install | Skill path |
|----------|---------|------------|
| Hermes | [hermes-outreachmagic `install.sh`](https://github.com/outreachmagic/hermes-outreachmagic) with `--with-lead-enrich` (symlinks all profiles by default) | `~/.hermes/skills/lead-enrich/` |
| Cursor | Copy or clone into `~/.cursor/skills/lead-enrich/` | `~/.cursor/skills/lead-enrich/` |
| Claude Code | Copy or clone into `~/.claude/skills/lead-enrich/` | `~/.claude/skills/lead-enrich/` |

**Hermes:** Real files live under `~/.hermes/skills/`. Each profile uses symlinks only (`profiles/<name>/skills/lead-enrich` → `../../../skills/lead-enrich`). Do not copy the skill into a profile directory.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Stale skill or empty DB | Hermes: run `install.sh --migrate` (links all profiles by default). Check `pipeline.py paths` for `warning`. |
| "No outreachmagic found" | Set `outreachmagic_home` in config.json to the absolute path |
| Serper 400 "not allowed" | Query too restrictive — fallback to simpler template |
| `import-profiles` rejects row | Requires email, LinkedIn, or `id` (lead_id). Use `stamp-attempted` for tag-only updates |
| Serper credits wasted on re-runs | Use `batch-check --skip-tagged`; ensure `serper_attempted` is stamped on save |
| Empty extraction | Serper results too thin — try broad queries, or mark confidence `low` |
| `ambiguous` on check | Name matched wrong company — run Serper or `check --force` |
| Team / group award row | `batch-check` returns `team_award` — skip research |
| outreachmagic not found | Install [hermes-outreachmagic](https://github.com/outreachmagic/hermes-outreachmagic) or set `outreachmagic_home` |
| Need email find | Install **email-finder** — see `references/email-finder.md` |
