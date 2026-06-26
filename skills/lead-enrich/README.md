<p align="center">
  This is a read-only mirror. Stars, issues, and pull requests belong at
  <a href="https://github.com/outreachmagic/outreachmagic">outreachmagic/outreachmagic</a>.
</p>

# Lead Enrich — Find LinkedIn, Job Titles, and Domains

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Research a person by name and company. Get their LinkedIn, bio, job title, company domain, and website through Serper.dev. Your agent's built-in model handles the extraction. No external LLM API needed.

Works standalone with just a Serper key. Pairs with Outreach Magic to check your pipeline first and skip leads you already have.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

Serper searches cost about a tenth of a cent. With OM, you only pay for leads you don't already have.

## How it fits

Give it a name and company. It checks your pipeline first if Outreach Magic is connected. Already have LinkedIn and domain? Zero Serper credits spent. If not, it hits Serper.dev and your agent extracts the details.

```
                      ┌────────────────────┐
name + company ──────►│  lead enrichment   │
(single or list)      └─────────┬──────────┘
                                │
                   ┌────────────▼────────────┐
                   │  OM check (if enabled)  │
                   └────────────┬────────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
      ┌──────────────────────┐      ┌──────────────────────┐
      │  already have        │      │  Serper search +     │
      │  LinkedIn + domain?  │      │  agent extraction    │
      │  (0 credits)         │      └──────────┬───────────┘
      └──────────┬───────────┘                 │
                 │                             ▼
                 │                    ┌──────────────────┐
                 │                    │  enriched lead   │
                 │                    └────────┬─────────┘
                 └────────┬────────────────────┘
                          ▼
                     ┌──────────┐
                     │  results │
                     └────┬─────┘
                          │
               ┌──────────┴──────────┐
               ▼                     ▼
     ┌──────────────────┐  ┌──────────────────┐
     │  agent replies   │  │  OM database     │
     │  (not saved)     │  │  (if connected)  │
     └──────────────────┘  └──────────────────┘
```

| Mode | What happens | What you need |
|------|-------------|---------------|
| Standalone | Searches Serper, extracts LinkedIn + domain + website via agent model | Just a Serper key |
| With Outreach Magic | Checks pipeline first, skips leads you already have, saves results so you never lose them | OM account + Serper key |

Here's how the credit saving works. If a lead already has LinkedIn and domain at the same company, the check returns right away. Zero Serper credits spent. If they have LinkedIn but no domain, it skips Serper too -- that is a job for the email waterfall finder. If they only have an email, the search still runs to find their LinkedIn profile and domain.

## Quick start

Once it's installed, try prompts like these:

```
use the lead enrich skill to find the job title, linkedin and domain for jane doe at acme corp
```

```
use the lead enrich skill to find the job title, linkedin and domain for everyone in leads.csv
```

Not sure what it can do? Ask your agent:

```
tell me everything the lead enrich skill can do and how i can minimize credit use
```

## Install

You can install just the lead enrichment skill on its own. Or install the full Outreach Magic suite, which gives you the email waterfall finder, the local database, and lead enrichment all at once.

**Install just the lead enrichment:**
```bash
npx skills add outreachmagic/lead-enrich
```

**Install the full Outreach Magic suite (email finder + database + lead enrich):**
```bash
npx skills add outreachmagic/outreachmagic
```

Or follow the agent install guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

## What you need

| Key | For | Required? |
|-----|-----|-----------|
| `SERPER_API_KEY` | Serper.dev Google Search | Yes |
| Outreach Magic login | Dedup and save enriched leads | Only with OM |

Set your API keys in your agent's environment config. If you use Outreach Magic, you can set them in the portal instead and they get passed through automatically.

## License

MIT. [Outreach Magic](https://outreachmagic.io)
