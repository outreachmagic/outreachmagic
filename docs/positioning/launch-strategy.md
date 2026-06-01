# Outreach Magic — Launch Strategy & Cross-Platform Plan
## May 2026

> Repo layout: private dev monorepo `magic-creators/outreachmagic-skill`, public install repo `outreachmagic/outreachmagic`. See [../RELEASING.md](../RELEASING.md).

---

## 1. Should You Build a Suite of Skills?

**Yes, but not yet.** The strategy is sound — data-aware skills that plug into the Outreach Magic database compound the value. But you don't need a suite to launch. You have more differentiation with 3 skills than ColdIQ has with 52.

| Phase | What | Timeline |
|---|---|---|
| **Launch now** | outreachmagic + lead-enrich + email-finder | Ship immediately |
| **Week 2-3** | One flagship "wow" skill | Campaign intelligence |
| **Post-launch** | 2-3 more data-aware skills | Grow organically |

### The difference between their suite and yours

| ColdIQ / Extruct / etc | Outreach Magic suite |
|---|---|
| Strategy & content skills | Data-aware skills |
| "Write a cold email template" | "Which of my templates is winning? Show me the data." |
| Stateless — agent's working memory | Stateful — reads pipeline DB |
| Substitutable by any LLM | Non-substitutable — no LLM can fabricate Smartlead reply data |

### The flagship skill to build next

**Campaign Intelligence Skill** — Give the agent a prompt like "which of my templates is winning?" and it:
1. Queries the Outreach Magic DB
2. Groups replies by campaign/subject line
3. Analyzes positive reply rate, sentiment, bounce rate
4. Returns the winning template + the data behind it

Estimated build time: a weekend. Value: undeniable. Impossible for stateless skills to replicate.

### MVP launch checklist
- [x] Core pipeline DB + 5 sequencer integrations
- [x] Lead enrichment (credit-saving dedup)
- [x] Email finder (trykitt + dedup)
- [ ] Hermes Hub submission (copy in [hub-copy.md](./hub-copy.md))
- [ ] skills.sh listing
- [ ] One good demo video / Loom — see [../demos/pull-to-pipeline.md](../demos/pull-to-pipeline.md)

That's enough. Ship it. Grow post-launch.

---

## 2. Cross-Platform Strategy: One Canonical Skill, Multiple Distribution Channels

**Do NOT maintain separate versions per platform.** SKILL.md is portable. Python + SQLite work everywhere. Cross-platform is mostly install-path documentation.

### The plan

```
Private monorepo: magic-creators/outreachmagic-skill
    skills/outreachmagic/     ← canonical source
    skills/lead-enrich/
    skills/email-finder/
    install.sh                ← --platform hermes|cursor|claude

Public install repo: outreachmagic/outreachmagic
    (CI-published from monorepo on v* tag)
```

**Distribution order:**

| # | Platform | Why |
|---|---|---|
| 1 | **Hermes Hub** | Uncontested beachhead. Zero GTM data skills. Be *the* GTM skill on Hermes. |
| 2 | **skills.sh** | Captures Claude Code + Cursor users through distribution |
| 3 | **Agensi** | Supports paid listings |
| 4 | **MCP Market** | Developer mindshare |
| 5 | **ColdIQ directory** | Your exact audience (GTM practitioners) |

### Why Hermes first (not Claude Code first)

- Claude Code GTM ecosystem is crowded — 7 major repos, ColdIQ dominates mindshare
- Hermes Hub is empty in your category — zero competition
- You can be *the* GTM data skill on Hermes, versus *one of many* on Claude Code
- Then skills.sh captures the Claude/Cursor audience through a distribution channel

---

## 3. The Platform Positioning

Outreach Magic isn't just a skill. It's a **platform** that other skills build on.

> "Outreach Magic is the data layer that makes every other GTM skill smarter. Cold email skills can read reply history. Enrichment skills can skip duplicates. Campaign analyzers can report actual performance instead of guessing."

| Framing | Perception |
|---|---|
| "Another GTM skill" | Commodity. One of many. Competing with ColdIQ. |
| "The data layer for GTM skills" | Infrastructure. Foundation. Other skills integrate with *you*. |

---

## 4. Competitive Moat Summary

| Moat | Why It Matters |
|---|---|
| **Persistent SQLite** | Pipeline survives reboots and platform switches. Competitors are stateless. |
| **5 sequencer integrations** | Smartlead, Instantly, Heyreach, PlusVibe, EmailBison |
| **Cross-platform sync** | Push/pull relay = data follows you across Claude Code, Cursor, Hermes |
| **Credit-saving dedup** | lead-enrich checks local DB before burning API credits |
| **Genuine freemium** | 1,000 relay events/mo free — see [pricing.md](./pricing.md) |
| **Hermes Hub first-mover** | Zero in GTM/sales data infrastructure category |
| **Platform positioning** | Infrastructure, not commodity |

---

## 5. One-Liners (Internal Alignment)

- **Positioning:** "Every other GTM skill tells your agent what to write. Outreach Magic tells your agent what's happening."
- **Suite strategy:** "We don't need 52 skills. We need one database and a few skills that prove why it matters."
- **Cross-platform:** "One canonical skill. Multiple distribution channels. No platform-specific forks."
- **Platform play:** "Outreach Magic isn't a skill you use. It's the data layer every other GTM skill wishes it had."

---

## 6. Immediate Next Actions

1. Submit to Hermes Hub using [hub-copy.md](./hub-copy.md)
2. List on skills.sh
3. Record a 2-minute Loom — [pull-to-pipeline demo](../demos/pull-to-pipeline.md)
4. Post on r/coldemail, r/hermesagent, r/claude referencing the CSV-stitching pain point
5. Start the "campaign intelligence" skill as the first data-aware companion
