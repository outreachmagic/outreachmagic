# Install Outreach Magic

## Hermes

```bash
hermes skills install outreachmagic/hermes-skill
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
hermes -s outreachmagic
```

The agent will walk you through setup. If you already have a key:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

## Cursor

Copy the skill into Cursor's skills directory:

```bash
git clone https://github.com/outreachmagic/cursor-skill.git /tmp/om-cursor
mkdir -p ~/.cursor/skills/outreachmagic
cp -r /tmp/om-cursor/{SKILL.md,scripts,references} ~/.cursor/skills/outreachmagic/
rm -rf /tmp/om-cursor
```

Initialize and connect:

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

For project-level only, copy `outreachmagic.mdc` to `.cursor/rules/` instead.

## Claude Code

Copy the skill scripts:

```bash
git clone https://github.com/outreachmagic/claude-code-skill.git /tmp/om-claude
mkdir -p ~/.claude/skills/outreachmagic
cp -r /tmp/om-claude/{scripts,references} ~/.claude/skills/outreachmagic/
rm -rf /tmp/om-claude
```

Initialize and connect:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

Add pipeline instructions to your project:

```bash
cat ~/.claude/skills/outreachmagic/../../../outreachmagic/claude-code-skill/CLAUDE_SNIPPET.md >> CLAUDE.md
```

Or manually copy the contents of `CLAUDE_SNIPPET.md` into your project's `CLAUDE.md`.

## Get an Agent Key

1. Go to [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent)
2. Sign up or log in
3. Click "Create Agent Key"
4. Copy the key (starts with `om_agent_`)

## Updates

```bash
python3 <skill_path>/scripts/pipeline.py update
```

Check without installing: `pipeline.py update --check`.

## Troubleshooting

| Error | Fix |
|-------|-----|
| GitHub API rate limit | Run `gh auth login` or set `GITHUB_TOKEN` env var |
| Security scan blocked (Hermes) | Use `--force` after reviewing SECURITY.md |
| Skill folder missing after install | Use the git clone install path instead |
