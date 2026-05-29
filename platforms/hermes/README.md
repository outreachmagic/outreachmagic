# OutreachMagic for Hermes

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

Skills live in `~/.hermes/skills/` (real files). Each Hermes profile gets symlinks — not copies.

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.13/install.sh | bash -s -- \
  --with-lead-enrich --migrate --tag v1.20.13 --lead-enrich-tag v1.2.2
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

Layout details: [hermes-skills-layout.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/hermes-skills-layout.md)

## Quick Start

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py stats
```

## Update

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
python3 ~/.hermes/skills/lead-enrich/scripts/enrich.py update
```

## Verify install

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

## Pricing

- **Free:** Unlimited agent-originated tracking, CLI pipeline view, 100 relay events/month
- **Pro ($19/mo):** Unlimited relay events, multi-platform sequencer sync

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
