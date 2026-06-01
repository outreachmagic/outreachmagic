# Outreach Magic — Hub & marketplace copy

> Canonical positioning line (all listings):
>
> **Every other GTM skill tells your agent what to write. Outreach Magic tells your agent what's happening.**

Pricing aligned with [pricing.md](./pricing.md): **1,000 free relay events/mo**, **Pro $9/mo** (50k relay cap).

---

## outreachmagic — short blurb

> The data layer your AI agent has been missing. Sync leads, replies, and campaign events from Smartlead, Instantly, Heyreach, PlusVibe, and EmailBison into a local SQLite database your agent can query directly. Free forever for local tracking and 1,000 relay events/mo. Pro $9/mo for sequencer sync. One `pipeline.py pull` and your agent sees your entire pipeline.

**Tags:** `sales` `outreach` `crm` `pipeline` `leads` `email` `linkedin` `webhooks` `smartlead` `instantly` `sqlite` `gtm`

**Related skills:** `lead-enrich`, `email-finder`

**Category (Hub metadata only):** `productivity` — filesystem stays `~/.hermes/skills/outreachmagic/`

---

## lead-enrich — short blurb

> Research people with Serper.dev before you burn API credits. Checks Outreach Magic first — if the lead already has LinkedIn + email at the same company, **zero Serper credits**. Built-in model extraction; saves via outreachmagic. Top of the Outreach Magic suite funnel.

**Tags:** `sales` `enrichment` `research` `linkedin` `serper` `leads` `pipeline`

**Related skills:** `outreachmagic`, `email-finder`

**External domains:** `google.serper.dev` (required), `api.outreachmagic.io` (via outreachmagic save)

---

## email-finder — short blurb

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

Each listing: one link to [outreachmagic.io](https://outreachmagic.io) + setup URL [app.outreachmagic.io/setup/agent](https://app.outreachmagic.io/setup/agent).

---

## Website hero

**Headline:** Your AI Agent Has a Blind Spot. Fix It.

**Subheadline:** Claude Code and Cursor can write brilliant cold outreach. But after they hit send, they go blind. Outreach Magic gives your AI agent a persistent memory of every lead, every reply, every bounce — across every sequencer you use.

**CTA:** [Start Free] [See How It Works]

**Social proof:** "From zero to full pipeline visibility in under 2 minutes. One command."

---

## Website feature blocks

**Stop Stitching CSVs** — Before: export from Smartlead, Heyreach, Instantly, merge in Sheets. After: `pipeline.py pull` → done.

**Local-First by Design** — SQLite on your machine. Push/pull relay to move between Claude Code, Cursor, Hermes.

**Actually Free to Start** — Unlimited local tracking + 1,000 relay events/mo. Pro $9/mo for sequencer sync.

**Built for AI Agents** — Structured CLI output. Every workflow assumes an LLM is on the other end.

**Cross-Platform. One Pipeline.** — Push from one machine, pull on another.

---

## Short copy snippets (social / directories)

**Ultra-short:** The data layer for AI SDRs. Sync Smartlead, Instantly, Heyreach into local SQLite.

**Pain-point hook:** Tired of exporting CSVs from Smartlead and Heyreach just to answer "did we get any replies?"

**Differentiation:** Every GTM skill tells your agent what to write. This one tells your agent what's happening.

---

## FAQ (website / pricing page)

**What counts as an event?** Relay-synced webhook events from connected sequencers. Local commands (`add-lead`, `show`, `stats`, dedup checks) are free and unlimited.

**What happens if I exceed my limit?** Free: friendly upgrade prompt; relay returns HTTP 429 when hard limit hit. Pro: 50k cap — we reach out personally if you're approaching it.

**Can I switch platforms?** `pipeline.py sync` on machine A, `pipeline.py pull --full` on machine B.

**Is it really free?** Yes for local tracking and 1,000 relay events/mo. Pro ($9/mo) unlocks full sequencer sync.

**Install:** [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) — `install.sh --platform hermes|cursor|claude`

