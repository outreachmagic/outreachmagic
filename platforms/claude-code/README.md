# Outreach Magic for Claude Code

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

Get your Agent Key at [app.outreachmagic.io/setup/agent](https://app.outreachmagic.io/setup/agent), then run:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/main/install.sh | bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

That's it. Restart Claude Code and ask:

> "show me my pipeline"

## Update

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/main/install.sh | bash
```

(Re-running without a key updates the skill in place; your local database and config are preserved.)

## Manual install

If you'd rather not pipe a script to bash:

```bash
git clone https://github.com/outreachmagic/claude-code-outreachmagic.git /tmp/om-claude
mkdir -p ~/.claude/skills/outreachmagic
cp -a /tmp/om-claude/. ~/.claude/skills/outreachmagic/
rm -rf /tmp/om-claude
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

## Usage

Start Claude Code in your project directory and ask:

- "Show me my pipeline"
- "How is outreach going?"
- "Pull latest events and show stats"
- "Show my campaigns"

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 1 platform, 1,000 relay events/month
- **Pro ($9/mo):** 50,000 relay events/month, all platform connections, multi-workspace routing

Sign up at [outreachmagic.io](https://outreachmagic.io) · Upgrade at [app.outreachmagic.io](https://app.outreachmagic.io/dashboard/billing)

## License

MIT
