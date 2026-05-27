# Lead Enrich — Person Research for AI Agents

Research people and enrich lead profiles using **Serper.dev** Google Search.
Works with **Hermes**, **Cursor**, and **Claude Code**. Pairs with
[Outreach Magic](https://github.com/outreachmagic/hermes-outreachmagic) for local SQLite
dedup and save.

> **Credit-saving:** checks your local outreachmagic database first. Skips Serper
> when the same person **and company** already have a LinkedIn URL.

## What it does

Given a name + company, the agent:

1. Checks outreachmagic (`enrich.py check`) — company-aware dedup, 0 credits
2. Runs targeted Serper searches (company website, LinkedIn profile)
3. Extracts structured fields using the agent's built-in model (no external LLM)
4. Saves via outreachmagic `import-profiles` (`company_domain` + LinkedIn)
5. Returns a summary: what was found, confidence, credits used

## Install

```bash
git clone https://github.com/outreachmagic/lead-enrich.git ~/.hermes/skills/lead-enrich
cp ~/.hermes/skills/lead-enrich/config.example.json ~/.hermes/skills/lead-enrich/config.json
# Edit config.json → paste Serper API key
```

| Platform | Path |
|----------|------|
| Hermes | `~/.hermes/skills/lead-enrich/` |
| Cursor | `~/.cursor/skills/lead-enrich/` |
| Claude Code | `~/.claude/skills/lead-enrich/` |

**Monorepo dev:** if both skills live under `skills/` in
[outreachmagic-skill](https://github.com/magic-creators/outreachmagic-skill),
outreachmagic is auto-detected as a sibling — no copy step required.

## Prerequisites

### Serper.dev API key

[serper.dev](https://serper.dev) → API key → `config.json` or `export SERPER_API_KEY=...`

### outreachmagic (recommended)

[hermes-outreachmagic](https://github.com/outreachmagic/hermes-outreachmagic) for dedup + `import-profiles`.
Override path: `OUTREACHMAGIC_HOME` or `outreachmagic_home` in config.

**Standalone mode:** use `normalize`, `serper-queries`, `serper-search`, and
`serper-format` without outreachmagic; export JSON manually.

## Usage

### Single person

> Research Jane Doe, VP Marketing at Acme Corp

### CLI reference

```bash
python3 scripts/enrich.py config
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"
python3 scripts/enrich.py check --force "Jane Doe" "Acme Corp"
python3 scripts/enrich.py batch-check input.json
python3 scripts/enrich.py serper-search --query 'site:linkedin.com/in Jane Doe "Acme Corp"'
python3 scripts/enrich.py serper-queries input.json
python3 scripts/enrich.py map-to-om research.json
```

## Credits

| Scenario | Credits |
|----------|---------|
| Same company + LinkedIn in DB | **0** |
| Same company, no LinkedIn | 1–2 |
| `ambiguous` (name match, wrong company) | 2–4 (full pack) |
| New lead | 2–4 |

## Config

See `config.example.json`. Key flags:

| Key | Default | Description |
|-----|---------|-------------|
| `dedup_before_search` | `true` | Set `false` to always run Serper |
| `max_people_per_run` | `50` | Batch cap |

## License

MIT — see [LICENSE](../../LICENSE) in the monorepo root when developing here.
