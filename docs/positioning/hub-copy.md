# Outreach Magic — Hub & marketplace copy

> Canonical positioning line (all listings):
>
> **Every other GTM skill tells your agent what to write. Outreach Magic tells your agent what's happening.**

Pricing aligned with [pricing.md](./pricing.md): **1,000 free relay events/mo**, **Pro $9/mo** (50k relay cap).

---

## outreachmagic — short blurb

> The data layer your AI agent has been missing. Sync leads, replies, and campaign events from Smartlead, Instantly, Heyreach, PlusVibe, and EmailBison into a local SQLite database your agent can query directly. Free forever for local tracking and 1,000 relay events/mo. Pro $9/mo for sequencer sync. One `pipeline.py pull` and your agent sees your entire pipeline.

**Tags:** `sales` `outreach` `crm` `pipeline` `leads` `email` `linkedin` `webhooks` `smartlead` `instantly` `sqlite` `gtm`

**Related skills:** `lead-enrich`, `lead-email`

**Category (Hub metadata only):** `productivity` — filesystem stays `~/.hermes/skills/outreachmagic/`

---

## lead-enrich — short blurb

> Research people with Serper.dev before you burn API credits. Checks Outreach Magic first — if the lead already has LinkedIn + email at the same company, **zero Serper credits**. Built-in model extraction; saves via outreachmagic. Top of the Outreach Magic suite funnel.

**Tags:** `sales` `enrichment` `research` `linkedin` `serper` `leads` `pipeline`

**Related skills:** `outreachmagic`, `lead-email`

**External domains:** `google.serper.dev` (required), `api.outreachmagic.io` (via outreachmagic save)

---

## lead-email — short blurb

> Find work emails with trykitt.ai after you have name + company domain (from lead-enrich or your CRM). Checks Outreach Magic first so you never pay twice. Saves email + verification status via outreachmagic. Requires outreachmagic + `TRYKITT_API_KEY`.

**Tags:** `sales` `email` `enrichment` `trykitt` `leads` `pipeline`

**Related skills:** `outreachmagic`, `lead-enrich`

**External domains:** `api.trykitt.ai` (required for find)

---

## Registry order

1. Hermes Hub  
2. skills.sh  
3. Agensi / MCP directories (as bandwidth allows)  
4. ClawHub last  

Each listing: one link to [outreachmagic.io](https://outreachmagic.io) + setup URL above.
