# Outreach Magic — Pipeline Visibility for Hermes

Your Hermes agent does the outreach. We show you what's working.

One SQLite file. No cloud. Your pipeline, visible.

## Quick Start

```bash
hermes skills install outreachmagic
hermes -s outreachmagic
# Your agent now auto-logs every outreach action
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py show
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

On first connect, Outreach Magic will automatically pull any existing events and show your pipeline.

[Sign up for a token →](https://outreachmagic.io)

## Commands

| Command | What it does |
|---------|-------------|
| `pipeline.py init` | Create database |
| `pipeline.py show [--stage X]` | View pipeline |
| `pipeline.py history --id 1` | Lead event timeline |
| `pipeline.py history --email j@a.com` | Look up + timeline by email |
| `pipeline.py history --name "Jane"` | Look up + timeline by name |
| `pipeline.py stats` | Quick stats |
| `pipeline.py add-lead --name "..."` | Add a lead |
| `pipeline.py log-event --lead-id 1 --type ...` | Log outreach |
| `pipeline.py update-stage --id 1 --stage ...` | Move deal forward |
| `pipeline.py connect --key TOKEN` | Connect to relay + auto-pull on first run |
| `pipeline.py pull` | Sync new events from relay (relay keeps archive; local dedupe) |
| `pipeline.py workspace list` | List workspaces (org-wide leads, workspace-scoped status/events) |
| `pipeline.py workspace create --name "Sales"` | Create a workspace |
| `pipeline.py campaign-map add --platform smartlead --workspace default --campaign-id ID` | Route platform campaign ID to a workspace |
| `pipeline.py quarantine list` | Events blocked until campaign is mapped to a workspace |
| `pipeline.py quarantine assign --id QID --workspace default` | Assign workspace and replay quarantined event |
| `pipeline.py pull --full` | Re-import all relay events after DB reset |
| `pipeline.py update` | Download latest skill scripts (checks remote `VERSION`) |
| `pipeline.py webhook-url` | Show webhook URLs |

## Keeping the skill up to date

Hermes runs scripts from `~/.hermes/skills/sales/outreachmagic/scripts/`, not your git clone. After pulling repo changes:

```bash
# From a clone (fastest while developing)
bash scripts/install.sh

# Or from the installed skill (after you push to GitHub)
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py update
```

`pull` warns when a newer version is on GitHub. Set `OUTREACHMAGIC_DEV_REPO=/path/to/hermes-agent` to update from a local clone instead of GitHub.

Reload the Hermes skill (or start a new session) after updating so instructions refresh; `pipeline.py update` replaces the Python files immediately.

**Version:** One number everywhere — source of truth is `pipeline/VERSION`. Check installed copy:

```bash
python3 ~/.hermes/skills/sales/outreachmagic/scripts/pipeline.py version
```

Bump `pipeline/VERSION` on each release (e.g. `1.3.1` → `1.3.2`) and push to `main`. Installed skills **auto-update from GitHub** on the next CLI run (checked at most once per hour). `install.sh` is only needed for first install.

## License

MIT — [Outreach Magic](https://outreachmagic.io)