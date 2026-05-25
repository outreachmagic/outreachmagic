# Install Outreach Magic

## New Hermes setup (3 commands)

```bash
hermes skills install outreachmagic/hermes-agent/skills/outreachmagic
hermes -s outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup
```

`setup` initializes the database, opens your browser to create an account (or log in),
and connects your agent with org-wide access. You're done.

If the security scan blocks install, review [SECURITY.md](../SECURITY.md) then retry with `--force`:

```bash
hermes skills install outreachmagic/hermes-agent/skills/outreachmagic --force
```

## Already have a token?

Skip the browser flow and pass it directly:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

Or connect a legacy per-platform token:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
```

## Local install (no GitHub API)

Use this when rate-limited or developing from a local clone:

```bash
git clone https://github.com/outreachmagic/outreachmagic-skill.git
cd outreachmagic-skill
bash scripts/sync-local.sh
hermes -s outreachmagic
```

If you already have the repo cloned, just run `bash scripts/sync-local.sh` from the repo root.

## Cursor / Claude Code

Copy `skills/outreachmagic/` into your agent's skills folder:

- **Cursor:** `~/.cursor/skills/outreachmagic/`
- **Claude Code:** `~/.claude/skills/outreachmagic/`

Then init and set up:

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py setup
```

## Updates

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
```

Check without installing: `pipeline.py update --check`.

## Troubleshooting

| Error | Fix |
|-------|-----|
| GitHub API rate limit | Run `gh auth login` or set `GITHUB_TOKEN=ghp_...` in `~/.hermes/.env` |
| Security scan blocked | Use `--force` after reviewing [SECURITY.md](../SECURITY.md) |
| Skill folder missing after install | Use the [local install](#local-install-no-github-api) path instead |
