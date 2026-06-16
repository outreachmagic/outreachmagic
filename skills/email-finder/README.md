# Email Finder

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Find work emails through **trykitt** and **Icypeas**. Works standalone or pairs with [Outreach Magic](https://github.com/outreachmagic/outreachmagic) for credit-saving dedup and persistent storage.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## How it works

```
                    trykitt ──┐
name + domain ────►          ├── waterfall ──► email result ──► stdout (standalone)
                    Icypeas ──┘                                      │
                                                               with OM: saves to
                                                                local SQLite DB
```

Standalone: just API keys, results print to stdout. With Outreach Magic: checks your local DB first, skips leads you already have, saves the result so you don't run the same search twice.

## Quick start

**Standalone (no OM needed):**
```bash
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com
```

**With Outreach Magic (dedup + save):**
```bash
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com --save --workspace CLIENT
```

**Batch (standalone):**
```bash
python3 scripts/email_finder.py batch-find --skip-om --yes --dry-run input.json
```

**Batch (with OM):**
```bash
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes --workers 3 --delay 3 input.json
```

## Install

Install via the main repo agent guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

Or install the full suite:
```bash
npx skills add outreachmagic/outreachmagic
```

## API keys

| Key | For |
|-----|-----|
| `TRYKITT_API_KEY` | trykitt.ai (first in waterfall) |
| `ICYPEAS_API_KEY` | Icypeas (fallback) |
| `MILLIONVERIFIER_API_KEY` | Optional bulk verification |
| Outreach Magic login | Dedup + save (only with OM) |

Don't see your email finder provider? [Open a GitHub issue](https://github.com/outreachmagic/outreachmagic/issues).

## License

MIT. [Outreach Magic](https://outreachmagic.io)
