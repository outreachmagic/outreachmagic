# Outreach Magic — Competitive Landscape & Gap Analysis
## May 2026

> Pricing and limits: [pricing.md](./pricing.md) — **1,000 free relay events/mo**, **Pro $9/mo** (50k cap).

---

## Market Overview

The AI agent skills ecosystem for GTM/SDR/lead generation is dominated by **Claude Code**. The ecosystem exploded in Q1 2026 with 7 major repos and multiple marketplaces. Hermes Hub has 652 skills total but essentially **zero** in the GTM/sales data infrastructure category.

---

## Competitive Landscape: Existing GTM Skills

### 1. ColdIQ GTM Skills
- **Size:** 7 master skills, 52 sub-skills
- **Focus:** Strategy + content generation
- **Gap:** No persistent data. No sequencer integration. No pipeline visibility.

### 2. Extruct GTM Skills
- **Focus:** End-to-end outbound campaigns — research, enrichment, email generation, sending
- **Gap:** Campaign execution, not pipeline tracking. No reply visibility after send.

### 3. GTM Flywheel
- **Focus:** Compounding framework — ICP research → signal scoring → cold email
- **Gap:** Process framework. No state, no sequencer sync, no persistence.

### 4. Claude GTM Plugin
- **Size:** 166 skills
- **Gap:** Massive but shallow. CRM skills talk *to* external CRMs; no local-first alternative.

### 5. GTM Agents
- **Size:** 92 agents + 52 skills
- **Gap:** Multi-agent orchestration. No data persistence layer between agents.

### 6. sales-skills/sales
- **Focus:** Prospecting, outbound, deals, proposals
- **Gap:** Router-based, stateless. No persistent pipeline.

### 7. Lead Gen Jay
- **Model:** Paid cohort ($2k-$3.5k)
- **Gap:** Paid course, not a product.

### 8. Marketing Skills
- **Size:** 32 skills, 12,800+ GitHub stars
- **Gap:** Marketing execution. No sales pipeline data.

---

## The Unified Gap: No Data Infrastructure Layer

| Category | Examples | What they do | What they DON'T do |
|---|---|---|---|
| **Strategy** | ColdIQ, GTM Flywheel | Tell agent *how* to think about GTM | Store data, track state |
| **Content** | ColdIQ email, Extruct | Generate copy, sequences, research | See replies, track pipeline |
| **Execution** | Extruct sending, sales-skills | Execute campaigns | Persist results across sessions |

**Outreach Magic is the only skill in a fourth category: Data Infrastructure.**

---

## Outreach Magic's Unique Positioning

1. **Persistent State** — SQLite survives reboots, session changes, platform switches
2. **Sequencer Integration** — Smartlead, Instantly, Heyreach, PlusVibe, EmailBison
3. **Cross-Platform Sync** — push/pull relay across Claude Code, Cursor, Hermes
4. **Credit-Saving Dedup** — lead-enrich checks local DB before Serper credits
5. **Free Tier With Real Value** — 1,000 relay events/mo + unlimited local tracking
6. **Hermes Hub First-Mover** — zero in GTM/sales data infrastructure

---

## Threats & Watch List

| Threat | Risk | Mitigation |
|---|---|---|
| Deepline (CLI + skills.sh GTM meta skill) | Medium | See [deepline-competitor-analysis.md](./deepline-competitor-analysis.md) — differentiate on pipeline data layer; match onboarding (API Keys, skills.sh) |
| ColdIQ or Extruct adds database/persistence | Medium | Speed to market + sequencer integrations |
| Sequencers build AI agent integrations | Low | Cross-sequencer unification is the value |
| Claude Code adds native state persistence | Low | Domain-specific schema beats generic state |
| Large CRM adds AI agent SDK | Medium | Local-first is a philosophical differentiator |

---

## Recommended Go-to-Market Order

1. **Hermes Hub** — uncontested category
2. **skills.sh** — cross-platform registry
3. **Agensi** — paid listings support
4. **MCP Market** — developer mindshare
5. **ColdIQ directory** — GTM practitioners
6. **Product Hunt** — broader launch
7. **Reddit** — r/coldemail, r/sales, r/claude, r/hermesagent

See [launch-strategy.md](./launch-strategy.md) and [../registry-publish.md](../registry-publish.md) for submission checklists.
