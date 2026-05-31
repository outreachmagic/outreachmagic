# Lead Enrich — Person Research for AI Agents

Research people and enrich lead profiles using **Serper.dev** Google Search.
Works with **Hermes**, **Cursor**, and **Claude Code**. Pairs with
[Outreach Magic](https://github.com/outreachmagic/hermes-outreachmagic) for local SQLite
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

**Hermes** (with outreachmagic, profile symlinks):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.15/install.sh | bash -s -- \
  --with-lead-enrich --with-email-finder --migrate --tag v1.20.15 --lead-enrich-tag v2.0.1 --email-finder-tag v1.0.1
```

See [skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md).

## Requirements

| Key | Purpose |
|-----|---------|
| `SERPER_API_KEY` | Google Search |
| outreachmagic + agent key | Dedup + save |

Email find: install **email-finder** + `TRYKITT_API_KEY` (not in lead-enrich v2+).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
