# OutreachMagic for Hermes

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

```bash
git clone https://github.com/outreachmagic/hermes-outreachmagic.git /tmp/om-hermes
mkdir -p ~/.hermes/skills/outreachmagic
cp -r /tmp/om-hermes/{SKILL.md,scripts,references} ~/.hermes/skills/outreachmagic/
rm -rf /tmp/om-hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
hermes -s outreachmagic
```

The agent will walk you through setup (getting an Agent Key at [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent)).

If you already have a key:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py setup --key om_agent_YOUR_KEY
```

## Quick Start

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py stats
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py history --id 1
```

## Update

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
```

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 100 relay events/month
- **Pro ($19/mo):** Unlimited relay events, multi-platform sequencer sync

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
