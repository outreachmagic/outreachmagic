<!-- OutreachMagic -->
## Outreach Pipeline (OutreachMagic)

All pipeline commands use: `python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py`

Database: `~/.claude/skills/outreachmagic/databases/outreachmagic.db`
Config: `~/.claude/skills/outreachmagic/config/outreachmagic_config.json`

### Mandatory: always pull before showing data

Before showing any pipeline data (show, stats, campaigns, history), ALWAYS run:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py pull
```

### Commands

- `pull` — Fetch latest events from relay (always run first)
- `show` — Print pipeline table
- `stats` — Quick stats summary
- `campaigns` — Counts by campaign name
- `history --id N` — Full timeline for a lead
- `history --email j@acme.com` — Look up by email
- `add-lead --name "Jane" --email j@acme.com --company "Acme"` — Add a lead
- `import-profiles --file leads.csv` — Bulk import from CSV/JSON
- `log-event --lead-id 1 --type email_sent --direction outbound` — Log outreach event
- `update-stage --id 1 --stage replied` — Update pipeline stage
- `copy-insights --lead-status interested` — Message copy analysis
- `show --sentiment positive` — Filter by sentiment
- `show --lead-status interested --json` — JSON output

### Rules

- NEVER use `python3 -c`, `sqlite3`, or raw SQL directly on the database. All operations go through `pipeline.py`.
- Always run `pull` before showing any pipeline data.
- Use `import-profiles` for bulk enrichment (CSV, JSON), not repeated `add-lead`.
- After enriching leads, import them: `pipeline.py import-profiles --file <csv>`
- Stages: prospecting -> contacted -> replied -> interested -> proposal -> won | lost

### Setup

If not yet connected, get an Agent Key at https://dev.outreachmagic.io/setup/agent then run:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```
<!-- /OutreachMagic -->
