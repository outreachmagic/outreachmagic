# Install Outreach Magic

## Hermes Agent (recommended)

Install from this repository with Hermes's built-in skill installer (includes security scan):

```bash
hermes skills inspect outreachmagic/hermes-agent/skills/outreachmagic
hermes skills install outreachmagic/hermes-agent/skills/outreachmagic
hermes -s outreachmagic
```

Initialize the local database:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
```

## Local development (from a git clone)

Copy the skill from your clone into `~/.hermes` without downloading from GitHub:

```bash
git clone https://github.com/outreachmagic/hermes-agent.git
cd hermes-agent
bash scripts/sync-local.sh
```

## Cursor / Claude Code / Codex CLI

Point your agent at the skill directory in this repo, or copy `skills/outreachmagic/` into your agent's skills folder:

- **Cursor:** `.cursor/skills/outreachmagic/` or `~/.cursor/skills/outreachmagic/`
- **Claude Code:** `.claude/skills/outreachmagic/`

Ensure `SKILL.md` and `scripts/` stay together. Run commands with:

```bash
python3 path/to/skills/outreachmagic/scripts/pipeline.py init
```

## Connect sequencer relay (optional, paid)

Get a token at [dev.outreachmagic.io](https://dev.outreachmagic.io) (production portal: app.outreachmagic.io when live).

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
```

## Updates

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
hermes skills update
```

Updates install from **GitHub release tags** only (user-triggered; no silent auto-update). See [SECURITY.md](../SECURITY.md) and [SKILL_REGISTRY_PLAN.md](./SKILL_REGISTRY_PLAN.md).
