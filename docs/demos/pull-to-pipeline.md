# Demo script — pull to pipeline (2 min)

Use for Loom / launch video. Terminal only for `login`; agent can run read commands.

## Setup (before recording)

- outreachmagic installed, `pipeline.py login` done  
- At least one sequencer connected or sample events in relay  
- Optional: lead-enrich installed with Serper key  

## Beat 1 — Problem (15s)

"Your AI can write outreach, but it can't see replies. Outreach Magic is the data layer — local SQLite your agent queries directly."

## Beat 2 — Pull (30s)

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
```

Call out: relay events imported; everything else stayed local.

## Beat 3 — Reply insight (45s)

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --email prospect@company.com
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py campaigns
```

"What's happening, not what to write."

## Beat 4 — Funnel (30s, optional)

```bash
python3 ~/.hermes/skills/lead-enrich/scripts/enrich.py check "Jane Doe" "Acme Corp"
```

"Zero Serper credits if Jane is already in the database."

## Close

- Free: 1,000 relay events/mo + unlimited local  
- Pro: $9/mo — [outreachmagic.io](https://outreachmagic.io)  
- Install: [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent)
