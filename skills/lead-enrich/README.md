# Lead Enrich — Person Research for AI Agents

Research people and enrich lead profiles using **Serper.dev** Google Search.
Works with **Hermes**, **Cursor**, and **Claude Code**. Pairs with
[Outreach Magic](https://github.com/outreachmagic/outreachmagic) for local SQLite
dedup and save. Use **[email-finder](https://github.com/outreachmagic/email-finder)** for trykitt find.

> **Credit-saving:** checks your local outreachmagic database first. Skips Serper
> when the same person **and company** already have LinkedIn (and email when present).

## What it does

Given a name + company, the agent:

1. Checks outreachmagic (`enrich.py check`) — email-aware dedup, 0 credits when complete
2. Runs targeted Serper searches (company website, LinkedIn profile)
3. Extracts structured fields using the agent's built-in model (no external LLM)
4. Saves via outreachmagic `import-profiles` (`company_domain` + LinkedIn)
5. Optionally runs **email-finder** when the user wants an address

## Install

Install the full suite (or outreachmagic + this skill) via the main repo agent guide:

https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md

Suite one-liner: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) — `install.sh --platform <name> --with-lead-enrich` (add `--with-email-finder` for email find).

## API keys

| Key | Required? |
|-----|-----------|
| `SERPER_API_KEY` | Yes — [serper.dev](https://serper.dev) |
| Outreach Magic (`pipeline.py login`) | Yes — dedup + save |

Email find: install **email-finder** + `TRYKITT_API_KEY` or `ICYPEAS_API_KEY`. Full key table: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md#third-party-api-keys-companions).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
