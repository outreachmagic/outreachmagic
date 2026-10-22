# Self-hosting Outreach Magic

Outreach Magic has two parts:

1. The open-source agent (this repo). A Python CLI, a local SQLite database, and a local dashboard.
2. The hosted relay ([app.outreachmagic.io](https://app.outreachmagic.io)). Always-on webhook ingest at `api.outreachmagic.io`, buffering, and restore when you `pull`.

Live sequencer webhooks go through the hosted relay. That is the supported ingest path. There is no `pipeline.py serve` and no official “deploy your own worker.”

You can still run the agent without a relay account: import CSVs and query the local database. That is offline use, not a second product.

## Local dashboard

```bash
python3 scripts/pipeline.py dashboard
```

Opens `http://127.0.0.1:8765` against the same SQLite file the agent uses. Pick a workspace, inspect pipeline/replies/bounces, and run pull/sync from the UI. The dashboard binds loopback only; do not expose it on a shared network.

Details: [references/command-reference.md](../skills/outreachmagic/references/command-reference.md#local-dashboard).

## CSV import (offline, no relay)

If you do not want live webhooks yet, export CSVs from your sequencers and import them:

```bash
# Export CSV from Smartlead, Instantly, HeyReach, or PlusVibe
pipeline.py import-profiles --file smartlead_export.csv --workspace my-campaign

pipeline.py show --workspace my-campaign
pipeline.py stats
pipeline.py dashboard
```

Every sequencer exports CSVs. Import them and the agent can query the local database. No relay account, no public URL, no process that has to stay running.

## Using the hosted relay

The hosted relay at `api.outreachmagic.io` is how live webhooks work. Sign up at [app.outreachmagic.io](https://app.outreachmagic.io), connect a sequencer, and paste the webhook URL from [Connections](https://app.outreachmagic.io/connections) into Smartlead, Instantly, HeyReach, PlusVibe, and the rest.

The relay accepts events while your laptop is asleep, buffers them, and delivers them when you sync.

```bash
# Sign in once (opens browser)
pipeline.py login

# Pull new events from the relay
pipeline.py pull

# Push local changes back (lead data, funnel stages)
pipeline.py sync
```

Your SQLite file is the working copy the agent queries. The relay is the remote buffer and restore path. New machine: `login` then `pull`.

Companion API keys (Serper, TryKitt, PlusVibe MCP, and the rest) live in the portal vault. `pipeline.py sync-secrets` writes them to `agent_secrets.env`.

## Offline vs hosted relay

| Feature | Offline (CSV + local dashboard) | Hosted relay |
|---------|---------------------------------|--------------|
| Setup | Clone / `npx skills add`, then import files | Paste webhook URL into sequencer |
| Live webhooks | No | Always-on at `api.outreachmagic.io` |
| Event buffering | No. Machine offline = nothing arrives. | Buffer while you are offline, delivered on `pull` |
| Multi-agent restore | Copy the SQLite file | `login` + `pull` on the new machine |
| Plan | Free | Free (1,000 webhook events/mo) or Pro ($9/mo for 50,000) |

Running campaigns that should not miss replies? Use the hosted relay. Trying the skill or working from exports? CSV import and the local dashboard are enough.

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
