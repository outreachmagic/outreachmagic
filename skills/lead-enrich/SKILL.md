---
name: lead-enrich
description: >
  Research people and enrich lead profiles using Serper.dev Google Search.
  Checks Outreach Magic first to avoid wasting API credits on existing leads.
  Extracts company domain, website, and LinkedIn URL via the agent's built-in
  model — no external LLM API needed. Saves results locally via the
  outreachmagic skill.
version: 1.1.5
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
    help: Create at https://dev.outreachmagic.io/setup/agent (starts with om_agent_)
    required_for: outreachmagic dedup checks and saving enriched leads
metadata:
  hermes:
    tags: [sales, outreach, crm, enrichment, leads, research, linkedin, serper]
    related_skills: [outreachmagic]
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

> **🪙 Credit-saving:** always checks outreachmagic *first* — if the lead already
> has a **LinkedIn URL** at the **same company**, no Serper credits are spent.
> Email-only records still get LinkedIn searches (1–2 credits). Name matches
> at a different company return `ambiguous` so Serper is not skipped by mistake.

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

The script auto-detects outreachmagic in `~/.hermes/skills/outreachmagic/`,
`~/.cursor/skills/outreachmagic/`, or `~/.claude/skills/outreachmagic/`.
Set `outreachmagic_home` in `config.json` to override the path.

## When to Use

- User says "research this person" / "look up Jane Doe at Acme"
- User wants to enrich a list of prospects before outreach
- User asks "do we already have this lead?" before researching
- User provides a CSV/JSON of names and companies to enrich in bulk
- User mentions Serper, lead enrichment, or person research

## Agent Behavior Rules (Important)

1. **Dedup first, always.** Before any Serper call, check outreachmagic with
   `enrich.py check`. If the lead exists and has LinkedIn, skip Serper entirely.
   The user's Serper credits are precious — never waste them.
2. **Serper only.** Prefer `enrich.py serper-search --query "..."` (stdlib HTTP,
   key from config/env). Or use `curl` with `$SERPER_API_KEY` — never embed the
   key in chat logs. Never scrape Google or LinkedIn directly.
3. **Built-in model only.** You (the agent) extract JSON from Serper results.
   No external LLM APIs (no Gemini, no OpenAI) — your own reasoning is the
   extraction engine.
4. **Complete research before saving.** Run the full search ladder first, then save once.
5. **Save via outreachmagic.** Use `import-profiles` for leads with LinkedIn.
   For leads without LinkedIn, use `add-lead` with notes (absolute last resort) or report unsaved.
   Never write raw SQL. Never run both save paths for the same person.
6. **Transparency.** Show which Serper queries ran, confidence, and what was
   saved. The user should see exactly where their credits went.
7. **Batch wisely.** Cap at 50 people per run. Process sequentially with 1s
   delay between Serper calls. Skip people who already exist.
8. **Email guess is separate.** If the user wants email addresses, point them
   to the `email-guess` skill — do not guess emails inline.

## Quick Start

```bash
# Single person (most common)
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"
# → if "not_found", proceed with Serper search pack below

# With workspace (associates lead with your pipeline workspace)
python3 scripts/enrich.py check --workspace your_workspace "Jane Doe" "Acme Corp"

# Batch from JSON file
python3 scripts/enrich.py batch-check --workspace your_workspace input/people.json

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
| `exists_linkedin` | Same company, has LinkedIn | Skip — return existing data |
| `exists_no_linkedin` | Same company, no LinkedIn (may have email) | LinkedIn queries only |
| `ambiguous` | Name match, company mismatch | Run full Serper pack — do not skip |
| `not_found` | No match | Run full Serper search pack |
| `dedup_disabled` | `dedup_before_search: false` in config | Run Serper as requested |

The helper script calls `pipeline.py history --name` and `--linkedin`, then
parses the output. All local, zero API credits.

### Phase 2 — Serper Search Pack

Only run for people who need it. **2–4 searches per person** depending on
result quality:

### Serper Credit Budget Estimator

- **Per person minimum:** `0` credits (dedup status `exists_linkedin`).
- **Common path:** `2` credits (`2a` strict company + `2c` primary LinkedIn).
- **Per person hard max:** `5` credits when all fallbacks are needed (`2a` + `2b` + `2c` + `2d` + `2e`).
- **Batch formula:** `min=0`, `max=5*N` where `N` is people in the run.
- **Batch cap example:** with `N=50`, hard max is `250` credits (worst case).

#### 2a. Company discovery — strict (always)

Preferred (no key in shell history):

```bash
python3 scripts/enrich.py serper-search --query '"Acme Corp" official website' --label company_discovery_strict
```

Or curl:

```bash
curl -s -X POST https://google.serper.dev/search \
  -H "X-API-KEY: $SERPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"\"Acme Corp\" official website","num":10,"gl":"us","hl":"en"}'
```

If HTTP 400 "query not allowed", retry with:

```bash
curl -s -X POST https://google.serper.dev/search \
  -H "X-API-KEY: $SERPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"Acme Corp website","num":10,"gl":"us","hl":"en"}'
```

#### 2b. Company discovery — broad (conditional)

Run only if 2a returns **no** organic results with an `http://` or `https://` link:

```bash
curl -s -X POST https://google.serper.dev/search \
  -H "X-API-KEY: $SERPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"Acme Corp official website","num":10,"gl":"us","hl":"en"}'
```
(Same template, unquoted company name.)

#### 2c. LinkedIn — primary (always)

Build query:
```
site:linkedin.com/in {First Last} {up to 5 words of role} "{Company Name}"
```

Example:
```bash
curl -s -X POST https://google.serper.dev/search \
  -H "X-API-KEY: $SERPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"site:linkedin.com/in Jane Doe VP Marketing \"Acme Corp\"","num":10,"gl":"us","hl":"en"}'
```

Fallback if rejected:
```
site:linkedin.com/in Jane Doe Acme Corp
```

#### 2d. LinkedIn — follow-up (conditional)

Run only if 2c has **no** `/in/` URLs, **or** none of the profile titles contain
both first and last name tokens. Use **unquoted** company:

```
site:linkedin.com/in Jane Doe VP Marketing Acme Corp
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

Every import from this skill is stamped with `--source-detail "lead-enrich"` by default.
If an `import_name` is provided, it appends as `"lead-enrich/{import_name}"`.

```bash
# Org-wide (no workspace)
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --source-detail "lead-enrich" \
  --json '[{"name":"Jane Doe","company":"Acme Corp","job_title":"VP Marketing","linkedin":"linkedin.com/in/janedoe","company_domain":"acme.com","tags":["nace"]}]'

# Scoped to a workspace
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --workspace your_workspace \
  --source-detail "lead-enrich" \
  --json '[{"name":"Jane Doe","company":"Acme Corp","job_title":"VP Marketing","linkedin":"linkedin.com/in/janedoe","company_domain":"acme.com","tags":["nace"]}]'
```

**If no LinkedIn, no email:**
Cannot use `import-profiles` (requires email or LinkedIn). Either:
- `add-lead --name ... --company ... --notes "domain: acme.com, no LinkedIn found"`
- Or report: "found domain X, no LinkedIn — not saved to pipeline DB"

### Phase 5 — Report

Summarize per person:

```
Jane Doe @ Acme Corp
  ✅ Company: acme.com | https://acme.com
  ✅ LinkedIn: linkedin.com/in/janedoe
  🟢 Confidence: high
  💾 Saved to outreachmagic (lead #42)
  🔍 Serper: 2 queries
```

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

## What this skill does NOT do

- ❌ Call Outreach Magic person-research API (`/v1/person-research`)
- ❌ Use external LLM APIs (Gemini, OpenAI, etc.) for extraction
- ❌ Scrape HTML pages or LinkedIn directly
- ❌ Guess or verify email addresses (use a dedicated email-finder workflow when available)
- ❌ Write raw SQL to the outreachmagic database
- ❌ Upload to remote servers (local-only by default)

---

## Platform Support

| Platform | Skill path | Auto-detected |
|----------|-----------|--------------|
| Hermes | `~/.hermes/skills/lead-enrich/` | ✅ |
| Cursor | `~/.cursor/skills/lead-enrich/` | ✅ |
| Claude Code | `~/.claude/skills/lead-enrich/` | ✅ |

Copy this directory to any of the above paths. The `enrich.py` script auto-finds
outreachmagic relative to the skills directory.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No outreachmagic found" | Set `outreachmagic_home` in config.json to the absolute path |
| Serper 400 "not allowed" | Query too restrictive — fallback to simpler template |
| `import-profiles` rejects row | Requires email or LinkedIn. Use `add-lead` for stub records |
| Empty extraction | Serper results too thin — try broad queries, or mark confidence `low` |
| `ambiguous` on check | Name matched wrong company — run Serper or `check --force` |
| outreachmagic not found | Install [hermes-outreachmagic](https://github.com/outreachmagic/hermes-outreachmagic) or set `outreachmagic_home` |
