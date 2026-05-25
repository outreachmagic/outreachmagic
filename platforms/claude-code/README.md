# OutreachMagic for Claude Code

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

```bash
git clone https://github.com/outreachmagic/claude-code-skill.git /tmp/om-claude
mkdir -p ~/.claude/skills/outreachmagic
cp -r /tmp/om-claude/{scripts,references} ~/.claude/skills/outreachmagic/
rm -rf /tmp/om-claude
```

Then initialize and connect:

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

Get your Agent Key at [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

### Add to your project

Append the OutreachMagic instructions to your project's `CLAUDE.md`:

```bash
cat /tmp/om-claude/CLAUDE_SNIPPET.md >> CLAUDE.md
```

Or copy the snippet manually from `CLAUDE_SNIPPET.md` into your existing `CLAUDE.md`.

## Usage

Start Claude Code in your project directory and ask:

- "Show me my pipeline"
- "How is outreach going?"
- "Pull latest events and show stats"
- "Show my campaigns"

## Update

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update
```

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 100 relay events/month
- **Pro ($19/mo):** Unlimited relay events, multi-platform sequencer sync

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
