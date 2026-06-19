# Email Finder

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Find work emails when you have a name and company domain. **trykitt** first, **Icypeas** on miss. Works standalone with just API keys, or pairs with Outreach Magic to save every result and never search for the same lead twice.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## How it fits

```
                     trykitt ──┐
  name + domain ────►          ├── waterfall ──► email found? ──► stdout (standalone)
                     Icypeas ──┘       │                              │
                                        no                             │
                                          └──► MillionVerifier ───────┤
                                                                  with OM: saves to
                                                                  local SQLite DB,
                                                                  skips next time
```

| Mode | What happens | What you need |
|------|-------------|---------------|
| Standalone | Searches trykitt, falls back to Icypeas, prints the email | Just API keys |
| With Outreach Magic | Checks your pipeline first → skips if you already have it → saves the result | OM account + API keys |

The waterfall always runs trykitt first (fastest, cheapest). If it misses, it tries Icypeas. You can add MillionVerifier at the end to check deliverability on bulk finds.

## Quick start

**Find one email on your own:**
```bash
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com
```

**Find one email and save it to your pipeline:**
```bash
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com --save --workspace CLIENT
```

**Find a batch on your own:**
```bash
python3 scripts/email_finder.py batch-find --skip-om --yes --dry-run input.json
```

**Find a batch and save results:**
```bash
python3 scripts/email_finder.py batch-find --workspace CLIENT --yes --workers 3 --delay 3 input.json
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
| `TRYKITT_API_KEY` | trykitt.ai (first in waterfall) | Yes |
| `ICYPEAS_API_KEY` | Icypeas (fallback) | Yes |
| `MILLIONVERIFIER_API_KEY` | Optional bulk re-verification | No |
| Outreach Magic login | Dedup + save results to pipeline | Only with OM |

## License

MIT. [Outreach Magic](https://outreachmagic.io)
