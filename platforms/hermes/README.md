# Outreach Magic for Hermes

The simplest pipeline tracker for AI agents. Auto-logs every outreach action to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

## Install

Skills live in `~/.hermes/skills/` (real files). Each Hermes profile gets symlinks — not copies.

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.20/install.sh | bash -s -- \
  --with-lead-enrich --with-email-finder --migrate \
  --tag v1.20.20 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

Full suite install docs: [install-companions.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/install-companions.md)

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

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
python3 ~/.hermes/skills/email-finder/scripts/email_finder.py update
```

## Verify install

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

## Pricing

- **Free:** Local tracking + CLI pipeline view + **1,000 relay events/month**
- **Pro ($9/mo):** Sequencer sync (50k relay events/month cap)

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
