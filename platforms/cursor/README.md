# OutreachMagic for Cursor

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

Copy the skill to your Cursor skills directory:

```bash
git clone https://github.com/outreachmagic/cursor-outreachmagic.git /tmp/om-cursor
mkdir -p ~/.cursor/skills/outreachmagic
cp -r /tmp/om-cursor/{SKILL.md,scripts,references} ~/.cursor/skills/outreachmagic/
rm -rf /tmp/om-cursor
```

Then initialize and connect:

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

Get your Agent Key at [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

### Project-level rule (alternative)

If you only want the skill in a specific project, copy the `.mdc` rule file:

```bash
mkdir -p .cursor/rules
cp /tmp/om-cursor/outreachmagic.mdc .cursor/rules/
```

## Quick Start

Open any project in Cursor and ask:

- "Show me my pipeline"
- "How is outreach going?"
- "Pull latest events and show stats"
- "Show my campaigns"

The skill auto-loads when Cursor detects relevant queries.

## Update

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update
```

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 100 relay events/month
- **Pro ($19/mo):** Unlimited relay events, multi-platform sequencer sync

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
