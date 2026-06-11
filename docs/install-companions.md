# Install companion skills (all platforms)

Canonical install commands from the unified public repo [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

**Agent-readable guide:** [AGENTS-INSTALL.md](../AGENTS-INSTALL.md). Install from a **pinned release tag** (not the moving `main` branch). Updates after install use `pipeline.py update` (GitHub Releases).

## Secure install pattern

```bash
OM_VERSION=v1.34.1

curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o /tmp/om_install.sh
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o /tmp/om_SHA256SUMS
(cd /tmp && grep ' install.sh$' om_SHA256SUMS | shasum -a 256 --check)
```

## Hermes

One script installs outreachmagic + optional companions into `~/.hermes/skills/`:

```bash
bash /tmp/om_install.sh --platform hermes --tag v1.34.1 \
  --with-lead-enrich --with-email-finder \
  --migrate-hermes-profiles
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

Profile symlinks: see [hermes-skills-layout.md](./hermes-skills-layout.md).

## Cursor

```bash
bash /tmp/om_install.sh --platform cursor --tag v1.34.1 \
  --with-lead-enrich --with-email-finder
```

```bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.cursor/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Claude Code

```bash
bash /tmp/om_install.sh --platform claude --tag v1.34.1 \
  --with-lead-enrich --with-email-finder
```

```bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

Skills live under `~/.claude/skills/{outreachmagic,lead-enrich,email-finder}/`.

## Pin companion tags (optional)

Companion versions are pinned automatically from `skill-suite.json`. Override explicitly:

```bash
bash /tmp/om_install.sh --platform hermes --tag v1.34.1 \
  --with-lead-enrich --with-email-finder \
  --lead-enrich-tag lead-enrich-v2.1.9 \
  --email-finder-tag email-finder-v2.2.22 \
  --migrate-hermes-profiles
```

## Local dev (monorepo checkout)

```bash
bash install.sh --platform hermes --local --migrate --with-lead-enrich --with-email-finder
```

See [RELEASING.md](./RELEASING.md) for release tags and CI.
