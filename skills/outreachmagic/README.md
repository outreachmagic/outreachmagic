# Outreach Magic Skill Suite

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Your agent goes blind after send. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite database your agent can query directly. Every reply, click, bounce, stage change, and booked call lands there.

Every other GTM skill tells your agent what to write. This one tells your agent what's happening.

The install includes the full skill suite: pipeline sync, [lead enrichment](https://github.com/outreachmagic/lead-enrich/blob/main/README.md), and [email waterfall finder](https://github.com/outreachmagic/email-finder/blob/main/README.md). Add your API keys and your agent can research leads and run the email waterfall; results save to the same local database. See those READMEs for key setup and more prompt examples.

## How it fits

The problem: every Friday you export CSVs from Smartlead, Instantly, and HeyReach, then merge them in Sheets. Your agent wrote great emails but has no idea who replied. You're stitching spreadsheets just to answer "did we get any replies this week?"

Outreach Magic fixes that. Every sequencer sends webhooks to api.outreachmagic.io. Those events sync to your agent's local database. Every reply, bounce, click, booking, and stage change lands there. Your agent queries it directly. No CSV stitching, no blind spots and it syncs prefectly across multiple agents so nothing gets lost.

```
                         ┌──────────────────────────────────────────┐
     Smartlead ─────────►│        webhooks / agent events           │
     Instantly ─────────►│        land here and are synced          │
     HeyReach  ─────────►│        across all your agents            │
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

| What's included | What happens |
|------|-------------|
| Pipeline sync | Sequencer webhooks sync to your local database. Query replies, bounces, bookings, and campaign stats directly. |
| Lead enrichment | Research people by name and company (LinkedIn, domain, job title). Add a [Serper key](https://github.com/outreachmagic/lead-enrich/blob/main/README.md). |
| Email waterfall finder | Find and verify work emails via trykitt, Icypeas, or MillionVerifier. Add keys per the [email finder README](https://github.com/outreachmagic/email-finder/blob/main/README.md). |

Provide companion API keys when you want lead research or email finding, or set them in the portal and they flow through automatically. The suite skips lookups for leads you already have.

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

**Install the full suite using npx:**

```bash
npx skills add outreachmagic/outreachmagic
```

**Or install the full suite using this prompt:**

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

## License

MIT. [Outreach Magic](https://outreachmagic.io)
