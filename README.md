# Outreach Magic

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Your agent goes blind after send. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite database your agent can query directly. Every reply, bounce, stage change, and booked call lands there.

Every other GTM skill tells your agent what to write. This one tells your agent what's happening.

This repo is the single source for everything: pipeline sync, email waterfall finding, lead enrichment, the install script, CI, tests, and docs. If you just want to install and use the skill, head to [outreachmagic.io](https://outreachmagic.io). If you want to contribute or understand how it works, you're in the right place.

## How it fits

The problem: every Friday you export CSVs from Smartlead, Instantly, and HeyReach, then merge them in Sheets. Your agent wrote great emails but has no idea who replied. You're stitching spreadsheets just to answer "did we get any replies this week?"

Outreach Magic fixes that. Every sequencer sends webhooks to api.outreachmagic.io. Those events sync to your agent's local database, and your enrichment results, email finds, and pipeline state are backed up to the cloud in return. Every reply, bounce, booking, and stage change lands there. Your agent queries it directly. No CSV stitching, no blind spots, and it syncs across multiple agents so nothing gets lost.

```
                         outreachmagic.io relay
                         _____________________
Smartlead _______________|
Instantly ______________||
HeyReach  _____________|||
PlusVibe  ____________||||
EmailBison __________|||||
Prosp     __________||||||
MasterInbox _______|||||||
Calendly  ________vvvvvvvv
                ┌──────────────────────────────────────┐
                │        api.outreachmagic.io          │
                │    cloud persistence · multi sync    │◄──────┐
                └─────────────┬────────────────────────┘       │
                              │  ▲                             │
                     events   │  │  backup / restore           │
                              │  │                             │
                              ▼  │                             │
                ┌──────────────────────────────────────┐       │
                │      Local SQLite database           │───────┘
                │  pipeline · research · emails ·      │
                │  verification · CRM                  │
                └────────────┬─────────────────────────┘
                             │
    ┌────────────────────────┼────────────────────────┐
    │                        │                        │
    ▼                        ▼                        ▼
┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│   PIPELINE SYNC    │ │  PERSON RESEARCH   │ │ EMAIL FIND & VER   │
│                    │ │                    │ │                    │
│ "show me my        │ │ "research Jane     │ │ "find Bill at      │
│  pipeline"         │ │  Doe, Acme Corp"   │ │  acme.com"         │
│                    │ │                    │ │                    │
│ replies, bounces,  │ │ Serper search      │ │ trykitt ── hit?    │
│ bookings, copy     │ │  → extract         │ │   │ miss           │
│ per campaign,      │ │    LinkedIn        │ │   ▼                │
│ all queryable      │ │    company domain  │ │ Icypeas ── hit?    │
│                    │ │    job title       │ │   │ miss           │
│ 8 sequencers       │ │  → saved to DB     │ │   ▼                │
│ CRM: GHL, HubSpot  │ │  → returned        │ │ not found          │
│                    │ │                    │ │                    │
│                    │ │ standalone: stdout │ │ Verify: MV,        │
│                    │ │ without OM account │ │ Scrubby (deep)     │
│                    │ │                    │ │ saved to DB        │
└────────────────────┘ └────────────────────┘ └────────────────────┘
```

## What's included

| Capability | What it does | Keys |
|------------|-------------|------|
| **Pipeline sync** | Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite DB | OM account |
| **Person research** | Find LinkedIn, job title, company domain by name + company via Serper | Serper key |
| **Email finding and verification** | Waterfall find (trykitt → Icypeas). Verify via MillionVerifier. Deep verify catch-all/unknown via Scrubby. | trykitt, Icypeas, MV, Scrubby |

## Quick start

```bash
npx skills add outreachmagic/outreachmagic
```

Or follow the agent install guide: [AGENTS-INSTALL.md](AGENTS-INSTALL.md).

Try prompts like:

- *"Show me my pipeline"*
- *"Find the email for Bill Smith at acme.com"*
- *"Research Jane Doe at Acme Corp"*
- *"Verify these emails: bill@acme.com, jane@xyz.io"*

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
```

Build the manifests:

```bash
make manifests
make release-check
```

## Repo layout

```
skills/outreachmagic/scripts/   # All 48+ scripts — pipeline, enrich, email_finder, etc.
install.sh                      # Cross-platform installer (Hermes, Cursor, Claude Code)
platforms/                      # Platform overlays and install wrappers
brand/                          # Logo SVGs
scripts/                        # Dev scripts — tests, manifests, release check
tests/                          # pytest suite
docs/                           # Dev docs — releasing, skill suite
```

## CRM Sync

Push contacts, deals, and event history to GoHighLevel and HubSpot from your pipeline. Salesforce planned. Run `pipeline.py --help` and see the CRM driver scripts in `skills/outreachmagic/scripts/crm/`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Start with an issue tagged `good first issue` if you're new to the codebase.

PRs are welcome. One logical change per PR. Run the tests before you push.

## Related repos

Marketing site: [outreachmagic.io](https://outreachmagic.io). Portal: [app.outreachmagic.io](https://app.outreachmagic.io).

## License

MIT. [Outreach Magic](https://outreachmagic.io)
