[![Claude Code](https://img.shields.io/badge/Claude_Code-ready-black?style=flat-square&logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC?style=flat-square&logo=cursor)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6?style=flat-square)](https://hermes-agent.nousresearch.com/docs/skills)
[![Codex](https://img.shields.io/badge/Codex-ready-22C55E?style=flat-square)](https://github.com/openai/codex)
[![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen?style=flat-square)](LICENSE)

---

# Outreach Magic

## Your agent goes blind after send.

You write great emails in Cursor. Send them through Smartlead. Then... nothing. No way to know who replied without logging into each sequencer one at a time.

**Outreach Magic** syncs Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into **one local SQLite database your agent can query directly.** No CSV exports, no merged Sheets.

```
   Smartlead ──┐
   Instantly ──┤
   PlusVibe  ──┤  webhooks ──►  api.outreachmagic.io  ── pull ──►  local SQLite DB
   HeyReach  ──┤                                                      ▲
   EmailBison ─┤                                                      │
   Prosp     ──┤                                                your agent talks to it
   Calendly  ──┘
```

Every reply, bounce, stage change, and booked call. One database. One prompt.

---

## The skill suite

| Skill | What it does | Requires | Standalone? |
|-------|-------------|----------|:-----------:|
| [🔍 **email-finder**](https://github.com/outreachmagic/email-finder) | Waterfall email enrichment via trykitt + Icypeas | just API keys | ✅ |
| [🧠 **lead-enrich**](https://github.com/outreachmagic/lead-enrich) | Person research via Serper — LinkedIn, domain, website | just a Serper key | ✅ |
| [⚡ **outreachmagic**](https://github.com/outreachmagic/outreachmagic) | Pipeline DB, sequencer sync, agent queries | OM account | Needs login |

All three work together. The companions work standalone with just API keys.

---

## Try it yourself

Install the skill suite, then ask your agent:

```text
Tell me everything the Outreach Magic skill suite can do in natural language with example prompts.
```

Other examples that work after install:

```text
What happened in my campaigns this week?
Find me what copy is resulting in the most positive leads.
Show me which client has the most engaged leads right now.
```

---

## Install prompt

Just paste this in your preferred agent to start the install process:

```text
Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md
and follow the instructions to install the skill suite.
```

---

<p align="center">
  <a href="https://outreachmagic.io">outreachmagic.io</a> ·
  <a href="https://github.com/outreachmagic/outreachmagic/issues">Report an issue</a> ·
  <a href="https://app.outreachmagic.io">Portal</a>
</p>
