# Outreach Magic — Pipeline Visibility for Hermes

Your Hermes agent does the outreach. We show you what's working.

One SQLite file. No cloud. Your pipeline, visible.

Config lives in `~/.hermes/skills/outreachmagic/config/outreachmagic_config.json` (relay token, pull cursor).
**Pipeline workspace routing** (single vs multi, workspaces, campaign maps) is stored in the Outreach Magic portal when connected; the dashboard and CLI both edit the same config. Default portal API is [dev.outreachmagic.io](https://dev.outreachmagic.io); override with `"api_base_url"` in config (e.g. `http://localhost:3000` for local dev).

## Quick Start

```bash
hermes skills inspect outreachmagic/hermes-agent/skills/outreachmagic
hermes skills install outreachmagic/hermes-agent/skills/outreachmagic
hermes -s outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
```

See [docs/install.md](docs/install.md) for Cursor, Claude Code, and local development setup.

```
Lead                    Company    Stage        Last      Next Action
────────────────────────────────────────────────────────────────────────
Sarah Chen              Stripe     replied      just now  Send case study
Marcus Rivera           Notion     contacted    just now
Emily Park              Vercel     contacted    1d ago

Pipeline: 3 active | 0 won | 0 lost | 3 total leads
```

## Repository layout

This repo follows the [agentskills.io](https://agentskills.io) skill layout:

```
skills/outreachmagic/
├── SKILL.md           # Agent instructions
├── scripts/           # pipeline.py CLI and helpers
└── references/        # Schema and reference docs
```

Install path for Hermes: `outreachmagic/hermes-agent/skills/outreachmagic`

## What It Does

- **Auto-logs every outreach action** — emails sent, replies received, LinkedIn messages, calls, meetings booked
- **Pipeline stages update automatically** — prospecting → contacted → replied → interested → proposal → won/lost
- **CLI + web dashboard** — terminal pipeline view or dark-themed UI at http://localhost:3100
- **Zero infrastructure** — one SQLite file in your Hermes container, no cloud API keys required for free tier

## Connect Your Sequencers (Paid)

Upgrade to sync Smartlead, Heyreach, Instantly, and more into your pipeline. Free tier includes 100 relay events/month.

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
```

On first connect, Outreach Magic will automatically pull any existing events and show your pipeline.

[Sign up for a token →](https://dev.outreachmagic.io)

Relay API: `api.outreachmagic.io` (webhook pass-through; payloads not stored server-side).

## Commands

| Command | What it does |
|---------|-------------|
| `pipeline.py init` | Create database |
| `pipeline.py show [--stage X]` | View pipeline |
| `pipeline.py history --id 1` | Lead event timeline |
| `pipeline.py history --email j@a.com` | Look up + timeline by email |
| `pipeline.py history --name "Jane"` | Look up + timeline by name |
| `pipeline.py stats` | Quick stats |
| `pipeline.py segment-insights` | Rank best converting titles/industries/headcount from sent vs positive leads |
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

To source new leads from what already converts:

```bash
# 1) Analyze what converts best (positive / sent) by title, industry, headcount
pipeline.py segment-insights --positive-lead-status interested --min-sent 2 --top 10

# 2) Pull full copy from current positives to reuse winning messaging
pipeline.py copy-insights --lead-status interested
```

## Keeping the skill up to date

Updates are **user-triggered** — the CLI never silently replaces scripts. It may print a notice when a newer GitHub release is available.

```bash
# Check for updates
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check

# Install latest release
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update

# Or reinstall from Hermes hub
hermes skills update

# Local development — sync from clone (no GitHub download)
bash scripts/sync-local.sh
```

Set `"dev_repo": "/path/to/outreachmagic-skill"` in config to update from a local clone instead of GitHub.

Each release must be tagged on GitHub (e.g. `v1.4.5`) with `update-manifest.json` generated via:

```bash
python3 scripts/generate-update-manifest.py
```

Reload the Hermes skill (or start a new session) after updating so instructions refresh.

**Version:** Source of truth is `skills/outreachmagic/scripts/VERSION`. Check installed copy:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

Bump `skills/outreachmagic/scripts/VERSION`, regenerate the manifest, tag `vX.Y.Z`, and push — CI publishes the GitHub Release automatically.

```bash
python3 scripts/generate-update-manifest.py
git tag vX.Y.Z && git push origin main --tags
```

See `.github/workflows/release.yml` and [docs/SKILL_REGISTRY_PLAN.md](docs/SKILL_REGISTRY_PLAN.md).

## Security

See [SECURITY.md](SECURITY.md) for data boundaries, external domains, and vulnerability reporting.

## License

MIT — [Outreach Magic](https://outreachmagic.io)
