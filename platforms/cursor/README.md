# Outreach Magic for Cursor

Pipeline visibility for AI agents. Auto-logs outreach to a local SQLite database. Connect Smartlead, Heyreach, Instantly, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly via paid relay.

Installs **outreachmagic**, **lead-enrich**, and **email-finder** from the unified repo.

## Install

Get started at [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding).

```bash
OM_VERSION=v1.38.3
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform cursor --tag "${OM_VERSION}"
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

Restart Cursor. In Agent chat run `/outreachmagic` or ask: "show me my pipeline"

Full agent guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

## Update

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update
python3 ~/.cursor/skills/lead-enrich/scripts/enrich.py update
python3 ~/.cursor/skills/email-finder/scripts/email_finder.py update
```

## Optional project rule

For a single repo only:

```bash
mkdir -p .cursor/rules
cp ~/.cursor/skills/outreachmagic/outreachmagic.mdc .cursor/rules/
```

## Pricing

- **Free:** Local tracking + CLI pipeline view + 1,000 webhook events/month
- **Pro ($9/mo):** Sequencer sync (50k webhook and sync events/month cap)

Sign up at [outreachmagic.io](https://outreachmagic.io) · Billing at [app.outreachmagic.io](https://app.outreachmagic.io/settings/billing)

## License

MIT
