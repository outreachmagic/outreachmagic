# Outreach Magic for Claude Code

Your pipeline, in your agent's hands. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into a local SQLite database your Claude Code agent can query directly. Every reply, click, bounce, and booking lands on your machine — no CSV exports, no merged sheets, no API round trips.

Installs **outreachmagic**, **lead-enrich**, and **email-finder** from the unified repo.

## Install

Get started at [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding).

```bash
OM_VERSION=v1.2.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform claude --tag "${OM_VERSION}"
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

Restart Claude Code and ask: "show me my pipeline"

Full agent guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

## Update

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update
python3 ~/.claude/skills/lead-enrich/scripts/enrich.py update
python3 ~/.claude/skills/email-finder/scripts/email_finder.py update
```

Sign up at [outreachmagic.io](https://outreachmagic.io) · Billing at [app.outreachmagic.io](https://app.outreachmagic.io/settings/billing) to see plans and limits.

## License

MIT
