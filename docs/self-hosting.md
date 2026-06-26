# Self-hosting Outreach Magic

Outreach Magic has two parts:

1. The open-source agent (this repo). A Python CLI with a local SQLite database.
2. The managed relay service ([app.outreachmagic.io](https://app.outreachmagic.io)). Handles webhook ingestion, buffering, and multi-agent sync.

This guide covers running the agent on your own machine. The managed relay is a paid service (Free tier available) that makes setup simpler — paste a webhook URL and it just works. You can use the agent without a relay account, but you will need to import data through files or run the built-in webhook receiver.

## Quick start — try it with sample data

```bash
git clone https://github.com/outreachmagic/outreachmagic
cd outreachmagic
pip install -e .
pipeline.py demo
```

Seeds a local SQLite database with 3 workspaces, 48 leads, and over 200 events. No signup, no API key. Just Python 3 and Git.

```bash
pipeline.py show           # explore the seeded pipeline
pipeline.py stats          # quick stats summary
pipeline.py demo           # re-run to start fresh
```

## Self-hosting the webhook receiver

The agent includes a built-in webhook receiver. Start it on your machine:

```bash
pipeline.py serve --port 8080
```

This starts a lightweight HTTP server. Sequencer webhooks sent to `http://localhost:8080/webhook/{platform}` will be written to your local SQLite database.

To receive webhooks from external sequencers, create a public tunnel:

```bash
ngrok http 8080
# or use any other tunneling service

# Then point your sequencers at:
# https://your-tunnel-url/webhook/smartlead
# https://your-tunnel-url/webhook/instantly
```

This approach is community-supported. Events only arrive while the process is running. Your machine goes to sleep, events are lost.

## CSV import (no relay, no serve needed)

If live webhooks are not required, import CSVs from your sequencers:

```bash
# Export CSV from Smartlead, Instantly, or HeyReach
# Import to Outreach Magic
pipeline.py import-profiles --file smartlead_export.csv --workspace my-campaign

# Check what came in
pipeline.py show --workspace my-campaign
pipeline.py stats
```

Every sequencer exports CSVs. Import them directly and your agent can query the local database. No relay account, no tunnel, no running server.

## Using the managed relay

The managed relay at `api.outreachmagic.io` handles webhook ingestion for you. Point Smartlead, Instantly, HeyReach, and the rest at a webhook URL from [app.outreachmagic.io/connections](https://app.outreachmagic.io/connections). The relay buffers events, keeps them when your machine is offline, and delivers them when you sync.

```bash
# Sign in once (opens browser)
pipeline.py login

# Pull new events from the relay
pipeline.py pull

# Push local changes back (lead data, funnel stages)
pipeline.py sync
```

The relay does not store your full database. It buffers webhooks briefly and routes data between your agents. Your SQLite file is the source of truth.

## Limitations compared to the managed relay

| Feature | Self-host | Managed relay |
|---------|:---------:|:-------------:|
| Setup | `git clone` + `pip install` + tunnel | Paste webhook URL into sequencer |
| Webhook ingestion | Manual CSV import or `serve` + tunnel | Automatic, always-on |
| Event buffering | No. Machine offline = events lost. | 30-day buffer, auto-retry |
| Multi-agent sync | No. Each agent has its own DB. | Yes. Events route to all agents. |
| Plan | Free | Free (1,000 webhook events/mo) or Pro ($9/mo for 50,000) |

Running real campaigns that matter? Use the managed relay. Testing, evaluating, or just browsing? Self-host works fine.

## Data portability

Your database is a SQLite file. Back it up, move it, or inspect it with any SQLite tool.

```bash
# Find your database
pipeline.py paths
# Shows: ~/.hermes/skills/outreachmagic/databases/outreachmagic.db

# Backup
cp ~/.hermes/skills/outreachmagic/databases/outreachmagic.db ~/backups/outreachmagic-$(date +%F).db

# Restore
cp ~/backups/outreachmagic-2026-07-01.db ~/.hermes/skills/outreachmagic/databases/outreachmagic.db
```

No lock-in. Your data lives on your machine.
