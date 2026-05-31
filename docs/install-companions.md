# Install companion skills (all platforms)

Canonical install commands. **Do not** use `platforms/hermes/install.sh` — that path exists only in the private monorepo. Published repos ship `install.sh` at the **repo root** (Hermes) or companion repos directly (Cursor/Claude).

Current release pins (update when tagging):

| Skill | Tag |
|-------|-----|
| outreachmagic (Hermes) | `v1.20.20` |
| lead-enrich | `v2.0.2` |
| email-finder | `v1.0.2` |

## Hermes

One script installs outreachmagic + optional companions into `~/.hermes/skills/`:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.20/install.sh | bash -s -- \
  --with-lead-enrich --with-email-finder --migrate \
  --tag v1.20.20 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

Profile symlinks: see [hermes-skills-layout.md](./hermes-skills-layout.md).

## Cursor

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/v1.20.20/install.sh | bash -s -- \
  --with-lead-enrich --with-email-finder \
  --tag v1.20.20 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.cursor/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/v1.20.20/install.sh | bash -s -- \
  --with-lead-enrich --with-email-finder \
  --tag v1.20.20 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.claude/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Monorepo developers

From a clone of `magic-creators/outreachmagic-skill`:

```bash
bash scripts/sync-local.sh
# or
bash platforms/hermes/install.sh --with-lead-enrich --with-email-finder --migrate
```

The `platforms/hermes/install.sh` path is **dev-only** — never copy it into published SKILL docs.
