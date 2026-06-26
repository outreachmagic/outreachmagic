# Outreach Magic

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Your agent goes blind after send. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite database your agent can query directly. Every reply, bounce, stage change, and booked call lands there.

Every other GTM skill tells your agent what to write. This one tells your agent what's happening.

This repo is the source for everything: the pipeline skill, the email waterfall finder, the lead enrichment tool, the install script, CI, tests, and docs. If you just want to install and use the skills, head to [outreachmagic.io](https://outreachmagic.io). If you want to contribute or understand how it works, you're in the right place.

## How it fits

The problem: every Friday you export CSVs from Smartlead, Instantly, and HeyReach, then merge them in Sheets. Your agent wrote great emails but has no idea who replied. You're stitching spreadsheets just to answer "did we get any replies this week?"

Outreach Magic fixes that. Every sequencer sends webhooks to api.outreachmagic.io. Those events sync to your agent's local database. Every reply, bounce, booking, and stage change lands there. Your agent queries it directly. No CSV stitching, no blind spots, and it syncs across multiple agents so nothing gets lost.

```
                         ┌──────────────────────────────────────────┐
     Smartlead ─────────►│        Platform webhook events           │
     Instantly ─────────►│        sync across all agents            │
     HeyReach  ─────────►│        so nothing gets lost              │
     PlusVibe  ─────────►│        api.outreachmagic.io              │
     EmailBison ────────►│                                          │
     Prosp     ─────────►│                                          │
     Calendly  ─────────►│                                          │
                         └──────────────────┬───────────────────────┘
                                            │  ▲
                                            ▼  │
                         ┌──────────────────────────────────────────┐
                         │  Your agent's local SQLite database      │
                         │  Cursor · Claude Code · Hermes Agent     │
                         └──────────────────┬───────────────────────┘
                                            |  ▲
                                            |  │
           ┌────────────────────────────────┼──|───────────────────────────────────┐
           ▼                                ▼  |                                   ▼
    ┌──────────────┐         ┌──────────────────────────────────┐        ┌────────────────────┐
    │  "show me    │         │  "find job title, linkedin +     │        │  "analyse my most  │
    │  the best    │         │  email for Bill Smith at         │        │  recent bounces    │
    │  performing  │         │  Acme Corp"                      │        │  for deliverability│
    │  copy"       │         │                                  │        │  + export Sheets"  │
    └──────────────┘         └──────────────────────────────────┘        └────────────────────┘
```

## Quick start

Once it's installed, try prompts like these:

```
use outreach magic skill suite to show me the best performing copy
```

```
use outreach magic skill suite to find job title, linkedin + email for Bill Smith at Acme Corp
```

```
use outreach magic skill suite to analyse my most recent bounces for deliverability insights
```

```
use outreach magic skill suite to do a detailed campaign stats export to Google Sheets
```

Not sure what it can do? Ask your agent:

```
tell me everything the outreach magic skill suite can do in natural language with example prompts
```

## Install

**Using npx:**

```bash
npx skills add outreachmagic/outreachmagic
```

**Using an agent prompt:**

```
Fetch this file and follow its instructions to install the Outreach Magic skill suite on this machine:

https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md
```

## What you need

| Key | For | Required? |
|-----|-----|-----------|
| OM account | Portal access, billing, webhook URLs | Yes |
| Sequencer webhooks | Smartlead, Instantly, HeyReach, etc. | At least one |
| Companion API keys | Lead enrichment (Serper) and email waterfall finder (trykitt, Icypeas, MillionVerifier) | When you use those features |

Set keys in your agent's environment config or in the portal. They sync through automatically.

## Repo layout

```
skills/outreachmagic/          # Main skill — pipeline.py, SQLite DB, sync, stats, CRM drivers
skills/email-finder/           # Email waterfall companion — trykitt, Icypeas, Scrubby
skills/lead-enrich/            # Lead research companion — Serper.dev
install.sh                     # Cross-platform installer (Hermes, Cursor, Claude Code)
platforms/                     # Platform overlays and install wrappers
brand/                         # Logo SVGs (published to outreachmagic/brand)
scripts/                       # Dev scripts — tests, manifests, sync, release check
tests/                         # pytest suite
docs/                          # Dev docs — releasing, skill suite
```

## Quick start for contributors

```bash
git clone https://github.com/outreachmagic/outreachmagic
cd outreachmagic
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Run the tests:

```bash
bash scripts/run-tests.sh
bash scripts/skill-scan.sh
```

Build the manifests:

```bash
make manifests
make release-check
```

## Companion skills

Two standalone skills ship from this repo. They work on their own with just API keys. Pair them with Outreach Magic for dedup and persistent storage.

| Skill | What it does | Repo |
|-------|-------------|------|
| Email finder | Waterfall find + verify work emails through trykitt, Icypeas, and MillionVerifier | [outreachmagic/email-finder](https://github.com/outreachmagic/email-finder) |
| Lead enrich | Research people by name and company through Serper.dev | [outreachmagic/lead-enrich](https://github.com/outreachmagic/lead-enrich) |

The companion repos are read-only mirrors published by CI. Development happens here.

## CRM Sync

Push contacts, deals, and event history to GoHighLevel and HubSpot from your pipeline. Salesforce planned. See [crm-sync/README.md](crm-sync/README.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Start with an issue tagged `good first issue` if you're new to the codebase.

PRs are welcome. One logical change per PR. Run the tests before you push.

## Related repos

| Repo | What |
|------|------|
| [outreachmagic/email-finder](https://github.com/outreachmagic/email-finder) | Companion mirror |
| [outreachmagic/lead-enrich](https://github.com/outreachmagic/lead-enrich) | Companion mirror |
| [outreachmagic/brand](https://github.com/outreachmagic/brand) | Logo assets |

Marketing site: [outreachmagic.io](https://outreachmagic.io). Portal: [app.outreachmagic.io](https://app.outreachmagic.io).

## License

MIT. [Outreach Magic](https://outreachmagic.io)
