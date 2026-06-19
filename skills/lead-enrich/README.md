# Lead Enrich

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Research a person by name and company. Get their LinkedIn, job title, company domain, and website through Serper.dev. Your agent's built-in model handles the extraction. No external LLM API needed.

Works standalone with just a Serper key. Pairs with Outreach Magic to check your pipeline first and skip leads you already have.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## How it fits

```
                      ┌── already have LinkedIn + email? ──► skip (0 credits)
  name + company ────►┤
                      └── Serper.dev search ──► agent model ──► structured JSON
                                                      │
                                            LinkedIn URL, job title,
                                            company domain, website
                                                      │
                                                 with OM: saves to
                                                 local SQLite DB
```

| Mode | What happens | What you need |
|------|-------------|---------------|
| Standalone | Searches Serper, extracts LinkedIn + domain + website via agent model | Just a Serper key |
| With Outreach Magic | Checks pipeline first → skips leads you already have → saves results so you never lose them | OM account + Serper key |

The dedup logic: if a lead already has LinkedIn + email at the same company, zero Serper credits spent. LinkedIn without email still skips Serper — use the email-finder companion for that. Email-only records keep searching for LinkedIn.

## Quick start

**Research one person — standalone:**
```bash
python3 scripts/enrich.py serper-search --query '"Acme Corp" official website'
python3 scripts/enrich.py serper-search --query 'site:linkedin.com/in Jane Doe Acme Corp'
```

**Check your pipeline before spending Serper credits:**
```bash
python3 scripts/enrich.py check "Jane Doe" "Acme Corp"      # 0 credits if found
python3 scripts/enrich.py batch-check --workspace W input.csv # batch dedup
```

**Save results to your pipeline:**
```bash
python3 scripts/enrich.py import-profiles --file results.json --workspace CLIENT
```

## Install

Install the full skill suite, or just this skill from the main repo:

```bash
npx skills add outreachmagic/outreachmagic
```

Or follow the agent install guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

## What you need

| Key | For | Required? |
|-----|-----|-----------|
| `SERPER_API_KEY` | Serper.dev Google Search | Yes |
| Outreach Magic login | Dedup + save enriched leads | Only with OM |

## License

MIT. [Outreach Magic](https://outreachmagic.io)
