# Hermes skills layout

Skills live in `~/.hermes/skills/<name>/` — Hermes’s built-in skills directory. Each profile only holds symlinks to those trees.

```
~/.hermes/
├── skills/
│   ├── lead-enrich/          ← real files (install & update here)
│   └── outreachmagic/        ← real files, DB, config
└── profiles/<name>/skills/
    ├── lead-enrich    → ../../../skills/lead-enrich
    └── outreachmagic  → ../../../skills/outreachmagic
```

## Why

1. **One copy** — `pipeline.py update` and `enrich.py update` write under `~/.hermes/skills/`. Every profile sees the same version through its symlink.
2. **Profile visibility** — `skill_view` scans `profiles/<name>/skills/`, follows the symlink, and loads the global install.
3. **No extra config** — `~/.hermes/skills/` is the default Hermes scan path. No `external_dirs` required.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.15/install.sh | bash -s -- \
  --with-lead-enrich --migrate --tag v1.20.15 --lead-enrich-tag v1.2.2
```

When `~/.hermes/profiles/` exists, `install.sh` symlinks every profile by default (`--no-profiles` to skip).

Or from a clone:

```bash
bash platforms/hermes/install.sh --with-lead-enrich --profile popcam --migrate
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
| `SERPER_API_KEY`, `OUTREACHMAGIC_AGENT_KEY` | `~/.hermes/.env` |
| outreachmagic config & SQLite | `~/.hermes/skills/outreachmagic/` |
| Per-profile env overrides | `~/.hermes/profiles/<name>/.env` |

## Verify

```bash
readlink ~/.hermes/profiles/<name>/skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

## Do not

- Copy full skill trees into `profiles/<name>/skills/` (they go stale).
- Query SQLite under `profiles/.../databases/` — use `pipeline.py paths` for the real DB path.
