# Install Outreach Magic

Get install commands for your platform at [dev.outreachmagic.io/dashboard/agent](https://dev.outreachmagic.io/dashboard/agent) or [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

After install, connect with **device authorization** (browser — no pasting keys into terminal or chat):

```bash
python3 <skill-path>/scripts/pipeline.py login
```

## Hermes

```bash
git clone https://github.com/outreachmagic/hermes-outreachmagic.git /tmp/om-hermes
mkdir -p ~/.hermes/skills/outreachmagic
cp -r /tmp/om-hermes/{SKILL.md,scripts,references} ~/.hermes/skills/outreachmagic/
rm -rf /tmp/om-hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
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
