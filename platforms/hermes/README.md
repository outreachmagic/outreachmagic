# Outreach Magic for Hermes

Pipeline visibility for AI agents. Auto-logs outreach to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe via paid relay.

Installs **outreachmagic**, **lead-enrich**, and **email-finder** from the unified repo. Skills live in `~/.hermes/skills/`; Hermes profiles get symlinks only.

## Install

```bash
OM_VERSION=v1.35.0
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o /tmp/om_install.sh
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o /tmp/om_SHA256SUMS
(cd /tmp && grep ' install.sh$' om_SHA256SUMS | shasum -a 256 --check)
bash /tmp/om_install.sh --platform hermes --tag "${OM_VERSION}"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

When `~/.hermes/profiles/` exists, install symlinks every profile by default (`--no-profiles` to skip).

Full guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md) · Layout: [hermes-skills-layout.md](https://github.com/outreachmagic/outreachmagic/blob/main/docs/hermes-skills-layout.md)

## Quick start

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

## Verify

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

## Pricing

- **Free:** Local tracking + CLI pipeline view + 1,000 webhook events/month
- **Pro ($9/mo):** Sequencer sync (50k webhook and sync events/month cap)

Sign up at [outreachmagic.io](https://outreachmagic.io)

## License

MIT
