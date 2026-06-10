# Outreach Magic for Cursor

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

Get your Agent Key at [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding), then run:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/main/install.sh | bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

That's it. Restart Cursor and in Agent chat run:

> /outreachmagic

Or ask: "show me my pipeline"

## Update

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/main/install.sh | bash
```

(Re-running without a key updates the skill in place; your local database and config are preserved.)

## Manual install

If you'd rather not pipe a script to bash:

```bash
git clone https://github.com/outreachmagic/cursor-outreachmagic.git /tmp/om-cursor
mkdir -p ~/.cursor/skills/outreachmagic
cp -a /tmp/om-cursor/. ~/.cursor/skills/outreachmagic/
rm -rf /tmp/om-cursor
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

### Project-level rule (optional)

For a single repo only, copy the rule file into that project:

```bash
mkdir -p .cursor/rules
cp ~/.cursor/skills/outreachmagic/outreachmagic.mdc .cursor/rules/
```

## Usage

Open any project in Cursor. In Agent chat, run `/outreachmagic` or ask:

- "Show me my pipeline"
- "How is outreach going?"
- "Pull latest events and show stats"
- "Show my campaigns"

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 1 platform, 1,000 relay events/month
- **Pro ($9/mo):** 50,000 relay events/month, all platform connections, multi-workspace routing

Sign up at [outreachmagic.io](https://outreachmagic.io) · Upgrade at [app.outreachmagic.io](https://app.outreachmagic.io/settings/billing)

## License

MIT
