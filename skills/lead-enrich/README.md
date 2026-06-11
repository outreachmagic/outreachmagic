# Lead Enrich — Person Research for AI Agents

Research people and enrich lead profiles using **Serper.dev** Google Search.
Works with **Hermes**, **Cursor**, and **Claude Code**. Pairs with
[Outreach Magic](https://github.com/outreachmagic/outreachmagic) for local SQLite
dedup and save. Use **[email-finder](https://github.com/outreachmagic/email-finder)** for trykitt find.

> **Credit-saving:** checks your local outreachmagic database first. Skips Serper
> when the same person **and company** already have LinkedIn (and email when present).
> Stamps **`serper_attempted`** on every enrichment run so re-runs skip already-tried leads.

## What it does

Given a name + company, the agent:

1. Checks outreachmagic (`enrich.py check` / `batch-check`) — tag-aware dedup, 0 credits when complete or already attempted
2. Runs targeted Serper searches (company website, LinkedIn profile)
3. Extracts structured fields using the agent's built-in model (no external LLM)
4. Saves via outreachmagic `import-profiles` with `serper_attempted` tag
5. Optionally runs **email-finder** when the user wants an address

## Batch workflow

```bash
python3 scripts/enrich.py batch-check --workspace YOUR_WS input.csv
python3 scripts/enrich.py batch-check --workspace YOUR_WS --skip-tagged input.csv
python3 scripts/enrich.py stamp-attempted --workspace YOUR_WS --lead-ids 1,2,3
```

## Install

Install the full suite (or outreachmagic + this skill) via the main repo agent guide:

https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md

Suite install: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) — `install.sh --platform <name>` (installs all three skills).

## API keys

| Key | Required? |
|-----|-----------|
| `SERPER_API_KEY` | Yes — [serper.dev](https://serper.dev) |
| Outreach Magic (`pipeline.py login`) | Yes — dedup + save |

Email find: install **email-finder** + `TRYKITT_API_KEY` or `ICYPEAS_API_KEY`. Full key table: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md#third-party-api-keys-companions).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
