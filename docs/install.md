# Install Outreach Magic

**Agent install (canonical):** [AGENTS-INSTALL.md](../AGENTS-INSTALL.md) — published on
[outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

Portal: [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding)

All platforms install the **full skill suite** (outreachmagic + lead-enrich + email-finder)
from [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

## Secure install (all platforms)

Pin a release tag. Download first — do not pipe remote scripts directly into `bash`.

```bash
OM_VERSION=v1.38.4
INSTALL_DIR=$(mktemp -d)

curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)

bash "${INSTALL_DIR}/install.sh" --platform <PLATFORM> --tag "${OM_VERSION}"
```

Preview: `bash "${INSTALL_DIR}/install.sh" --dry-run --platform <PLATFORM> --tag "${OM_VERSION}"`

## Platform examples

**Hermes:**

```bash
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

**Cursor:**

```bash
bash "${INSTALL_DIR}/install.sh" --platform cursor --tag "${OM_VERSION}"
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

**Claude Code:**

```bash
bash "${INSTALL_DIR}/install.sh" --platform claude --tag "${OM_VERSION}"
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

## Local development (monorepo)

```bash
bash install.sh --platform hermes --local
```

## CI / automation

Run `pipeline.py login` once on a machine with a browser, then set `OUTREACHMAGIC_AGENT_KEY` in CI secrets (never commit the key).
