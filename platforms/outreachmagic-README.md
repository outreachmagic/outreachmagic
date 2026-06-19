# Outreach Magic — Your Pipeline, In Your Agent's Hands

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

```
  Smartlead ──┐
  Instantly ──┤
  HeyReach  ──┤  webhooks ──►  api.outreachmagic.io  ── sync ──►  local SQLite DB
  PlusVibe  ──┤                                                          ▲
  EmailBison ─┤                                                          │
  Prosp     ──┤                                                    your agent reads it
  Calendly  ──┘                                                        directly
```

Outreach Magic turns your outbound pipeline into a local SQLite database your AI agent can query directly. Every reply, click, bounce, and booked call from every sequencer lands in one place — on your machine, in real time.

**Works with Claude Code, Cursor, and Hermes.** No CSV exports, no merged sheets, no API pagination.

---

## How it works

| Step | What happens |
|------|-------------|
| You connect your sequencers | Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, Calendly |
| Webhooks flow to one endpoint | One webhook per platform — no per-client or per-campaign setup |
| Events sync to a local SQLite DB | Every lead move, reply, bounce, and booking lands on your machine |
| Your agent reads it directly | Your AI agent queries the database — no API, no dashboard, no round trip |

The result: your agent can answer questions like "who replied this week?" or "which campaign has the highest reply rate?" — without you exporting a CSV or opening a dashboard.

---

## How the skills fit together

```
  email-finder ──►  lead-enrich  ──►  outreachmagic
  (find emails)     (research)       (your pipeline DB)
       │                  │                 │
       └───── all local ──┴──── queries ────┘
```

| Skill | What it does | Works alone? |
|-------|-------------|-------------|
| **outreachmagic** | Pipeline DB — syncs sequencer data, stores replies + events, agent queries it directly | Needs relay account |
| **lead-enrich** | Find anyone by name + company — LinkedIn, domain, website | Yes, just a Serper key |
| **email-finder** | Waterfall email lookup through trykitt and Icypeas | Yes, just API keys |

All three install together. The companion skills check your pipeline before spending credits — if you already have someone's data, they skip the lookup and save you money.

---

## Getting started

**In your agent (Claude Code, Cursor, or Hermes), paste this:**

> Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md and follow its instructions exactly to install the Outreach Magic skill suite on this machine.

Or run the installer directly:

> Download `install.sh` from the latest release on this repo, verify the SHA256 checksum, then run `bash install.sh --platform <your-agent>` where platform is `hermes`, `cursor`, or `claude`.

After install, connect your account:

> Run `pipeline.py login` from your terminal — it opens a browser where you sign in. Then connect your sequencers in the portal at app.outreachmagic.io.

**Once connected, ask your agent things like:**

- "Show me leads who replied this week"
- "Which campaign has the highest reply rate?"
- "Pull the latest events and give me a pipeline briefing"
- "Are there leads still interested from yesterday?"
- "How many bounces did we get on the A/B test campaign?"
- "Export leads that haven't been contacted yet"

---

## What people use it for

| Who | Why it clicks |
|-----|---------------|
| **GTM engineers** | Stop writing n8n pipelines to merge sequencer data. Your agent reads a local SQLite DB instead. |
| **SaaS founders** | Know what's working without dashboard-hopping. $9/mo instead of $200/mo in half-solutions. |
| **Agencies** | One webhook per platform. Route by campaign name. Query across every client from one terminal. |
| **AI agent builders** | Give your agent pipeline awareness. Let it answer "who replied?" without API integration. |

---

## Supported platforms

Smartlead · Instantly · HeyReach · PlusVibe · EmailBison · Prosp · MasterInbox · Calendly

Don't see yours? [Open an issue](https://github.com/outreachmagic/outreachmagic/issues).

---

## Getting started

**In your agent (Claude Code, Cursor, or Hermes), paste this:**

> Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md and follow its instructions exactly to install the Outreach Magic skill suite on this machine.

---

## Updates

Skills update through the pipeline tool itself — no reinstall needed:

> Run `pipeline.py update` to pull the latest release. Rollback with `pipeline.py rollback` if something goes wrong.

---

## License

MIT — [Outreach Magic](https://outreachmagic.io)
