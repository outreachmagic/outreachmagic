# Install companion skills (all platforms)

Canonical install from [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

One `install.sh` installs **outreachmagic**, **lead-enrich**, and **email-finder** together.
See [AGENTS-INSTALL.md](../AGENTS-INSTALL.md) for the full agent guide.

## Install

```bash
OM_VERSION=v1.38.0
INSTALL_DIR=$(mktemp -d)

curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)

bash "${INSTALL_DIR}/install.sh" --platform <PLATFORM> --tag "${OM_VERSION}"
```

| Platform | Flag | Skills directory |
|----------|------|------------------|
| Hermes | `hermes` | `~/.hermes/skills/` |
| Cursor | `cursor` | `~/.cursor/skills/` |
| Claude Code | `claude` | `~/.claude/skills/` |

## Hermes profiles

When `~/.hermes/profiles/` exists, install symlinks all profiles by default.
Link one profile: `bash install.sh --platform hermes --profile <name>`

Layout: [hermes-skills-layout.md](./hermes-skills-layout.md)

## Local dev (monorepo)

```bash
bash install.sh --platform hermes --local
```

Updates after install: `pipeline.py update` (GitHub releases). See [RELEASING.md](./RELEASING.md).
