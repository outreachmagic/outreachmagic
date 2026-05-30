# Lead Enrich — Person Research for AI Agents

Research people and enrich lead profiles using **Serper.dev** Google Search.
Works with **Hermes**, **Cursor**, and **Claude Code**. Pairs with
[Outreach Magic](https://github.com/outreachmagic/hermes-outreachmagic) for local SQLite
dedup and save.

> **Credit-saving:** checks your local outreachmagic database first. Skips Serper
> when the same person **and company** already have LinkedIn (and email when present).
> Optional **trykitt.ai** email finding after enrichment (~65% find rate in testing).

## What it does

Given a name + company, the agent:

1. Checks outreachmagic (`enrich.py check`) — email-aware dedup, 0 credits when complete
2. Runs targeted Serper searches (company website, LinkedIn profile)
3. Extracts structured fields using the agent's built-in model (no external LLM)
4. Saves via outreachmagic `import-profiles` (`company_domain` + LinkedIn)
5. Optionally finds email via trykitt.ai (or waterfall) when `TRYKITT_API_KEY` is set
6. Returns a summary: what was found, confidence, Serper + email credits used

## Install

**Hermes** (with outreachmagic, profile symlinks):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.15/install.sh | bash -s -- \
  --with-lead-enrich --migrate --tag v1.20.15 --lead-enrich-tag v1.3.0
```

Or clone lead-enrich only: `git clone https://github.com/outreachmagic/lead-enrich.git ~/.hermes/skills/lead-enrich`

Add keys to `~/.hermes/.env` (see `default.env`).

| Platform | Path |
|----------|------|
| Hermes | `~/.hermes/skills/lead-enrich/` (profiles: symlink only) |
| Cursor | `~/.cursor/skills/lead-enrich/` |
| Claude Code | `~/.claude/skills/lead-enrich/` |

`enrich.py` finds outreachmagic at `~/.hermes/skills/outreachmagic/`. Override with `outreachmagic_home` in config.

## Prerequisites

### Serper.dev API key

[serper.dev](https://serper.dev) → add `SERPER_API_KEY=...` to `~/.hermes/.env`
(template: `default.env` in this repo). Also works via `config.json` or shell export.

### outreachmagic (recommended)

[hermes-outreachmagic](https://github.com/outreachmagic/hermes-outreachmagic) for dedup + `import-profiles`.
Set `OUTREACHMAGIC_AGENT_KEY=om_agent_...` in `~/.hermes/.env` (same file as Serper).
Override install path: `OUTREACHMAGIC_HOME` or `outreachmagic_home` in config.

### trykitt.ai (optional — email finding)

[trykitt.ai](https://trykitt.ai) → add `TRYKITT_API_KEY=...` to `~/.hermes/.env`.
Enables Phase 5 combined find + verify. Free tier limits ~10 concurrent calls;
use 8+ second delays for large batches. See `references/email-finding-research.md`.

**Existing users:** no migration required — add the key when you want email finding.

After enrichment, workspace rollups (tags, LinkedIn connections by sender) use outreachmagic — no Serper credits:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py workspace summary --workspace <slug> --json
```

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
python3 scripts/enrich.py update --check
python3 scripts/enrich.py update
python3 scripts/enrich.py update --tag v1.1.5
```

## Credits

| Scenario | Serper | trykitt |
|----------|--------|---------|
| LinkedIn + email in DB | **0** | **0** |
| LinkedIn, no email | **0** | 0–1 (Phase 5) |
| Email, no LinkedIn | 1–2 | 0–1 |
| `ambiguous` (wrong company) | 2–5 | 0–1 |
| New lead | 2–5 | 0–1 |

## Config

See `config.example.json`. Key flags:

| Key | Default | Description |
|-----|---------|-------------|
| `dedup_before_search` | `true` | Set `false` to always run Serper |
| `max_people_per_run` | `50` | Batch cap |

## License

MIT — see [LICENSE](LICENSE) in this repository.
