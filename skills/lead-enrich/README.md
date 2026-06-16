# Lead Enrich

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Person research for AI agents. Uses **Serper.dev** Google Search to find company domains, websites, and LinkedIn profiles. Your agent's built-in model handles extraction — no external LLM API needed.

Works standalone or pairs with [Outreach Magic](https://github.com/outreachmagic/outreachmagic) for credit-saving dedup and persistent storage.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## How it works

```
name + company ──► Serper.dev search ──► agent model ──► structured JSON ──► stdout (standalone)
                                                        LinkedIn, domain               │
                                                        company website          with OM: saves to
                                                                                 local SQLite DB
```

Standalone: just a Serper key, results as JSON. With Outreach Magic: checks your pipeline first. If you already have that lead with LinkedIn and email, zero Serper credits spent.

## Quick start

**Standalone (no OM needed):**
```bash
python3 scripts/enrich.py serper-search --query '"Acme Corp" official website'
python3 scripts/enrich.py serper-search --query 'site:linkedin.com/in Jane Doe Acme Corp'
```

**With Outreach Magic (dedup + save):**
```bash
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"       # 0 credits
python3 scripts/enrich.py batch-check --workspace W input.csv # batch dedup
```

After research, save to your pipeline with `import-profiles`.

## Install

Install via the main repo agent guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

Or install the full suite:
```bash
npx skills add outreachmagic/outreachmagic
```

## API keys

| Key | For |
|-----|-----|
| `SERPER_API_KEY` | [Serper.dev](https://serper.dev) Google Search |
| Outreach Magic login | Dedup + save (only with OM) |

Don't see your enrichment provider? [Open a GitHub issue](https://github.com/outreachmagic/outreachmagic/issues).

## License

MIT. [Outreach Magic](https://outreachmagic.io)
