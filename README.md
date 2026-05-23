# Outreach Magic — Pipeline Visibility for Hermes

Your Hermes agent does the outreach. We show you what's working.

One SQLite file. No cloud. Your pipeline, visible.

## Quick Start

```bash
hermes skills install outreach-magic
hermes -s outreach-magic
# Your agent now auto-logs every outreach action
python3 ~/.hermes/skills/sales/outreach-magic/scripts/pipeline.py show
```

```
Lead                    Company    Stage        Last      Next Action
────────────────────────────────────────────────────────────────────────
Sarah Chen              Stripe     replied      just now  Send case study
Marcus Rivera           Notion     contacted    just now
Emily Park              Vercel     contacted    1d ago

Pipeline: 3 active | 0 won | 0 lost | 3 total leads
```

## What It Does

- **Auto-logs every outreach action** — emails sent, replies received, LinkedIn messages, calls, meetings booked
- **Pipeline stages update automatically** — prospecting → contacted → replied → interested → proposal → won/lost
- **CLI + web dashboard** — terminal pipeline view or dark-themed UI at http://localhost:3100
- **Zero infrastructure** — one SQLite file in your Hermes container, no cloud, no API keys

## Connect Your Sequencers (Paid)

Upgrade to sync Smartlead, Heyreach, Instantly, and more into your pipeline. Free tier includes 100 relay events/month.

```bash
pipeline.py connect --key YOUR_TOKEN
```

[Sign up for a token →](https://outreachmagic.io)

## Commands

| Command | What it does |
|---------|-------------|
| `pipeline.py init` | Create database |
| `pipeline.py show [--stage X]` | View pipeline |
| `pipeline.py stats` | Quick stats |
| `pipeline.py add-lead --name "..."` | Add a lead |
| `pipeline.py log-event --lead-id 1 --type ...` | Log outreach |
| `pipeline.py update-stage --id 1 --stage ...` | Move deal forward |
| `pipeline.py connect --key TOKEN` | Connect sequencers (paid) |
| `pipeline.py pull` | Sync events from relay |
| `pipeline.py webhook-url` | Show webhook URLs |

## License

MIT — [Outreach Magic](https://outreachmagic.io)