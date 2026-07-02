# Outreach Magic for Hermes

Your pipeline, in your agent's hands. Sync Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, and Calendly into a local SQLite database your Hermes agent can query directly. Every reply, click, bounce, and booking lands on your machine — no CSV exports, no merged sheets, no API round trips.

Installs **outreachmagic** — the unified skill with pipeline sync, person research, and email finding/verification. Skills live in `~/.hermes/skills/`; Hermes profiles get symlinks only.

## Install

```bash
OM_VERSION=v1.3.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
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
```

## Verify

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

Sign up at [outreachmagic.io](https://outreachmagic.io) to see plans and limits.

## License

MIT
