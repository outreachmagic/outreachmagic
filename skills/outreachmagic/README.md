# Outreach Magic Skill Suite

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Your agent goes blind after send. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite database your agent can query directly. Every reply, click, bounce, stage change, and booked call lands there.

Every other GTM skill tells your agent what to write. This one tells your agent what's happening.

| Capability | What it does | API Keys |
|------------|-------------|----------|
| **Pipeline sync** | Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into one local SQLite DB | OM account |
| **Person research** | Find LinkedIn, job title, company domain by name + company. Uses Serper. | Serper key |
| **Email finding and verification** | Waterfall find (trykitt to Icypeas). Verify (MillionVerifier). Deep verify catch-all/unknown (Scrubby). | trykitt, Icypeas, MV, Scrubby |

## How it fits

The problem: every Friday you export CSVs from Smartlead, Instantly, and HeyReach, then merge them in Sheets. Your agent wrote great emails but has no idea who replied. You're stitching spreadsheets just to answer "did we get any replies this week?"

Outreach Magic fixes that. Every sequencer sends webhooks to api.outreachmagic.io. Those events sync to your agent's local database. Every reply, bounce, click, booking, and stage change lands there. Your agent queries it directly. No CSV stitching, no blind spots and it syncs prefectly across multiple agents so nothing gets lost.

```

                                  outreachmagic.io relay
                                  _____________________
Smartlead ______________________|
Instantly _____________________||
HeyReach  ____________________|||
PlusVibe  ___________________||||
EmailBison _________________|||||
Prosp     _________________||||||
Calendly  _________________vvvvvv
                    +----------------------+
                    |  api.outreachmagic.io |--- local SQLite database
                    |  webhook sync + pull  |    (replies, bounces, bookings...)
                    +----------------------+          |
                                                      |
                +-------------------------------------+
                v                                     v
     +------------------------+        +--------------------------+
     |    PERSON RESEARCH     |        |  EMAIL FIND and VERIFY   |
     |                        |        |                          |
     |  enrich.py check       |        |  email_finder.py         |
     |  "Jane Doe" "Acme"     |        |  find --name --domain     |
     |        |               |        |        |                 |
     |        v               |        |        v                 |
     |  Serper.dev            |        |  waterfall.py            |
     |  (Google search)       |        |  trykitt --- hit?        |
     |        |               |        |     | miss               |
     |        v               |        |     v                   |
     |  agent extracts        |        |  Icypeas --- hit?        |
     |  LinkedIn, domain      |        |     | miss               |
     |        |               |        |     v                   |
     |        v               |        |  not found               |
     |  --- saved to DB       |        |                          |
     |  (if OM connected)     |        |  Optional extras:        |
     |  --- stdout            |        |  MillionVerifier         |
     |  (standalone)          |        |    (instant check)       |
     +------------------------+        |  Scrubby                 |
                                       |    (deep verify 72h)     |
                                       |                          |
                                       |  --- saved to DB         |
                                       |  (if OM connected)       |
                                       |  --- stdout              |
                                       |  (standalone)            |
                                       +--------------------------+
```

All three capabilities are included in a single install. Add API keys per the table below and your agent can research leads and run the email waterfall; results save to the same local database.

## Quick start

Once it's installed, try prompts like these:

| Prompt | What happens |
|--------|-------------|
| "Show me my pipeline" | Pulls latest sequencer events, shows reply rates, bounces, bookings |
| "Research Jane Doe at Acme Corp" | Serper search, extracts LinkedIn + domain, saves to DB |
| "Find the email for Bill Smith at acme.com" | Hits trykitt to Icypeas waterfall, saves to DB |
| "Verify these emails: bill@acme.com, jane@xyz.io" | MillionVerifier deliverability check |
| "Deep verify catch-all emails in my workspace" | Submits catch-all/unknown emails to Scrubby (24-72h) |
| "Export my best performing copy to Sheets" | Pulls campaign stats, exports to Google Sheets |
| "Analyse my most recent bounces" | `query bounced` — filter by bounce reason, domain, etc. |
| "Find emails for everyone in leads.csv" | `batch-find` with OM dedup — skips leads already found |

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
| OM agent key | Pipeline sync, portal access | Yes for pipeline sync |
| `SERPER_API_KEY` | Person research via Serper | Only for research |
| `TRYKITT_API_KEY` | Email finding (first in waterfall) | Only for email finding |
| `ICYPEAS_API_KEY` | Email finding (fallback) | Only for email finding |
| `MILLIONVERIFIER_API_KEY` | Email verification | Only for verification |
| `SCRUBBY_API_KEY` | Deep verification (catch-all) | Only for verification |

Set keys in your agent's environment config or in the portal. They sync through automatically.

## License

MIT. [Outreach Magic](https://outreachmagic.io)
