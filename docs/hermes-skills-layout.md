# Hermes skills layout

Skills live in `~/.hermes/skills/<name>/` — Hermes’s built-in skills directory. Each profile only holds symlinks to those trees.

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

1. **One copy** — `pipeline.py update` and `enrich.py update` write under `~/.hermes/skills/`. Every profile sees the same version through its symlink.
2. **Profile visibility** — `skill_view` scans `profiles/<name>/skills/`, follows the symlink, and loads the global install.
3. **No extra config** — `~/.hermes/skills/` is the default Hermes scan path. No `external_dirs` required.

## Install

```bash
OM_VERSION=v1.34.1
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o /tmp/om_install.sh
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o /tmp/om_SHA256SUMS
(cd /tmp && grep ' install.sh$' om_SHA256SUMS | shasum -a 256 --check)
bash /tmp/om_install.sh --platform hermes --tag "${OM_VERSION}" \
  --with-lead-enrich --with-email-finder --migrate-hermes-profiles
```

When `~/.hermes/profiles/` exists, `install.sh` symlinks every profile by default (`--no-profiles` to skip).

Or from a monorepo clone (dev only):

```bash
bash platforms/hermes/install.sh --with-lead-enrich --with-email-finder --profile popcam --migrate
```

## New profile

```bash
mkdir -p ~/.hermes/profiles/<name>/skills
ln -sf ../../../skills/lead-enrich ~/.hermes/profiles/<name>/skills/lead-enrich
ln -sf ../../../skills/outreachmagic ~/.hermes/profiles/<name>/skills/outreachmagic
```

## Secrets & config

| What | Where |
|------|--------|
| Portal-synced API keys (Serper, TryKitt, Icypeas, etc.) | `~/.hermes/skills/outreachmagic/config/agent_secrets.env` (via `pipeline.py sync-secrets`) |
| Agent key (after `pipeline.py login`) | `~/.hermes/skills/outreachmagic/config/outreachmagic_config.json` |
| outreachmagic config & SQLite | `~/.hermes/skills/outreachmagic/` |

## Verify

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

## Do not

- Copy full skill trees into `profiles/<name>/skills/` (they go stale).
- Query SQLite under `profiles/.../databases/` — use `pipeline.py paths` for the real DB path.
