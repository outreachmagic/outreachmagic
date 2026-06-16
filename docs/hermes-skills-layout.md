# Hermes skills layout

Skills live in `~/.hermes/skills/<name>/` — Hermes's built-in skills directory. Each profile only holds symlinks to those trees.

```
~/.hermes/
├── skills/
│   ├── lead-enrich/          ← real files (install & update here)
│   ├── email-finder/         ← real files
│   └── outreachmagic/        ← real files, DB, config
└── profiles/<name>/skills/
    ├── email-finder   → ../../../skills/email-finder
    ├── lead-enrich    → ../../../skills/lead-enrich
    └── outreachmagic  → ../../../skills/outreachmagic
```

## Why

1. **One copy** — `pipeline.py update` writes under `~/.hermes/skills/`. Every profile sees the same version through its symlink.
2. **Profile visibility** — `skill_view` scans `profiles/<name>/skills/`, follows the symlink, and loads the global install.
3. **No extra config** — `~/.hermes/skills/` is the default Hermes scan path.

## Install

```bash
OM_VERSION=v1.38.3
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

When `~/.hermes/profiles/` exists, `install.sh` symlinks every profile by default (`--no-profiles` to skip).

## New profile

```bash
bash install.sh --platform hermes --profile <name>
```

Or manually:

```bash
mkdir -p ~/.hermes/profiles/<name>/skills
ln -sf ../../../skills/lead-enrich ~/.hermes/profiles/<name>/skills/lead-enrich
ln -sf ../../../skills/email-finder ~/.hermes/profiles/<name>/skills/email-finder
ln -sf ../../../skills/outreachmagic ~/.hermes/profiles/<name>/skills/outreachmagic
```

## Secrets & config

| What | Where |
|------|--------|
| Portal-synced API keys | `~/.hermes/skills/outreachmagic/config/agent_secrets.env` (via `pipeline.py sync-secrets`) |
| Agent key (after login) | `~/.hermes/skills/outreachmagic/config/outreachmagic_config.json` |
| outreachmagic config & SQLite | `~/.hermes/skills/outreachmagic/` |

## Verify

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```
