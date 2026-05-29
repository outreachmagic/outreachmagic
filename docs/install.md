# Install Outreach Magic

Get install commands for your platform at [dev.outreachmagic.io/dashboard/agent](https://dev.outreachmagic.io/setup/agent) or [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

After install, connect with **device authorization** (browser — no pasting keys into terminal or chat):

```bash
python3 <skill-path>/scripts/pipeline.py login
```

## Hermes

Skills install to `~/.hermes/skills/` (real files). Hermes profiles use symlinks — see [hermes-skills-layout.md](hermes-skills-layout.md).

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.12/install.sh | bash -s -- \
  --with-lead-enrich --all-profiles --migrate --tag v1.20.12 --lead-enrich-tag v1.2.1
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

New profile only (skills already installed):

```bash
ln -sf ../../../skills/outreachmagic ~/.hermes/profiles/<name>/skills/outreachmagic
ln -sf ../../../skills/lead-enrich ~/.hermes/profiles/<name>/skills/lead-enrich
```

## Cursor

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/main/install.sh | bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

Or manual copy from [cursor-outreachmagic](https://github.com/outreachmagic/cursor-outreachmagic).

## Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/main/install.sh | bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

## CI / automation

Run `login` once on a machine with a browser, then set `OUTREACHMAGIC_AGENT_KEY` in your CI secrets from local config (never commit the key).
