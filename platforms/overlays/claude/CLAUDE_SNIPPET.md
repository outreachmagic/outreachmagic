<!-- Outreach Magic -->
<!-- Deprecated: append this block to CLAUDE.md only if you installed before SKILL.md auto-discovery. New installs use install.sh + SKILL.md; you can remove this block from CLAUDE.md after updating. -->
## Outreach Pipeline (Outreach Magic)

All pipeline commands use: `python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py`

### Reads vs refresh

- **Analytics (by campaign, time windows):** `query engagement --workspace SLUG --since 48h --json` — no pull required.
- **Latest activity / timelines:** run `pull` first.

### Commands

- `query engagement|replies|interested` — read-only analytics (preferred for counts)
- `pull` — refresh from relay when needed
- `show`, `history`, `stats`, `campaigns`, `import-profiles`, `log-event`, `update-stage`
- `workspace summary --workspace SLUG --json` — tags / LinkedIn sender stats

### Rules

- **Reads:** `pipeline.py query` or read-only `query --sql` (SELECT only). Not `python3 -c` / raw `sqlite3`.
- **Writes:** only `pipeline.py` mutation commands.
- Use **`events`** for volume analytics, not `workspace_lead_events`.
- `sync` only when the user asked.

Reference: `~/.claude/skills/outreachmagic/references/query-guide.md`

### Setup

If not connected: `pipeline.py login` — https://app.outreachmagic.io/setup/agent
<!-- /Outreach Magic -->
