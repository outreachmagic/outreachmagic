# Install companion skills (all platforms)

Canonical install commands from the unified public repo [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

Install uses `main` (latest). Updates after install use `pipeline.py update` (GitHub Releases). See [install.md](./install.md).

## Hermes

One script installs outreachmagic + optional companions into `~/.hermes/skills/`:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder \
  --migrate
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

Profile symlinks: see [hermes-skills-layout.md](./hermes-skills-layout.md).

## Cursor

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform cursor \
  --with-lead-enrich --with-email-finder
```

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.cursor/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform claude \
  --with-lead-enrich --with-email-finder
```

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.claude/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Pin a release tag (optional)

For reproducible installs, pass `--tag`, `--lead-enrich-tag`, and `--email-finder-tag`:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder \
  --tag v1.20.20 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2 \
  --migrate
```

## Local dev (monorepo checkout)

```bash
bash install.sh --platform hermes --local --migrate --with-lead-enrich --with-email-finder
```

See [RELEASING.md](./RELEASING.md) for release tags and CI.
