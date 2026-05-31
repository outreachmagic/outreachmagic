<!-- OutreachMagic -->
<!-- Deprecated: append this block to CLAUDE.md only if you installed before SKILL.md auto-discovery. New installs use install.sh + SKILL.md; you can remove this block from CLAUDE.md after updating. -->
## Outreach Pipeline (OutreachMagic)

All pipeline commands use: `python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py`

Database: `~/.claude/skills/outreachmagic/databases/outreachmagic.db`
Config: `~/.claude/skills/outreachmagic/config/outreachmagic_config.json`

### Mandatory: always pull before showing data

Before showing any pipeline data (show, stats, campaigns, history), ALWAYS run:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py pull
```

### Org-wide vs workspace-scoped

In multi-workspace mode, workspace-scoped commands require `--workspace SLUG`.

**Org-wide (no workspace needed):** `add-lead` (creating/looking up a lead).
**Workspace-scoped (workspace required):** `log-event`, `update-stage`.

`add-lead` accepts an optional `--workspace` to also associate the lead with a workspace at creation time.

### Commands

- `pull` — Fetch latest events from relay (always run first)
- `show` / `show --workspace SLUG` — Print pipeline table
- `stats` — Quick stats summary
- `campaigns` — Counts by campaign name
- `history --id N` — Full timeline for a lead
- `history --email j@acme.com` — Look up by email
- `add-lead --name "Jane" --email j@acme.com --company "Acme"` — Add a lead (org-wide)
- `add-lead ... --workspace SLUG` — Add a lead and associate with workspace
- `import-profiles --file leads.csv` — Bulk import from CSV/JSON
- `log-event --lead-id 1 --type email_sent --workspace SLUG` — Log outreach event (workspace required)
- `update-stage --id 1 --stage replied --workspace SLUG` — Update pipeline stage (workspace required)
- `copy-insights --lead-status interested` — Message copy analysis
- `show --sentiment positive` — Filter by sentiment
- `show --lead-status interested --json` — JSON output
- `show --since today` — Filter by date (YYYY-MM-DD or 'today')
- `lead-table --workspace acme_corp --since today --json` — Today's leads for a workspace
- `workspace list` — List available workspaces
- `personalize-pending --json` — List leads needing personalization (default: first_name, company_name)
- `personalize-set --lead-id N --field F --value V` — Write a personalization value
- `personalize-set --batch --json '[...]'` — Bulk write personalization values
- `personalize-get --lead-id N --json` — Read personalization for a lead
- `personalize-status` — Personalization summary counts
- `cleanup-rules` — Remove invalid campaign mapping rules

### Rules

- NEVER use `python3 -c`, `sqlite3`, or raw SQL directly on the database. All operations go through `pipeline.py`.
- Always run `pull` before showing any pipeline data.
- Use `import-profiles` for bulk enrichment (CSV, JSON), not repeated `add-lead`.
- Always pass `--workspace SLUG` on `log-event` and `update-stage` in multi-workspace mode.
- Never guess email addresses — ask the user or check source material.
- Stages: prospecting -> contacted -> replied -> interested -> proposal -> won | lost

### Setup

If not yet connected, get an Agent Key at https://dev.outreachmagic.io/setup/agent then run:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```
<!-- /OutreachMagic -->
