# Install Outreach Magic

Get install commands for your platform at [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

All platforms install from one repo: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

After install, connect with **device authorization** (browser — no pasting keys into terminal or chat):

```bash
python3 <skill-path>/scripts/pipeline.py login
```

**Full suite install (all platforms):** see [install-companions.md](./install-companions.md).

## Hermes

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder --migrate \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

## Cursor

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform cursor \
  --with-lead-enrich --with-email-finder \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

## Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform claude \
  --with-lead-enrich --with-email-finder \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

## Local development (monorepo)

```bash
bash install.sh --platform hermes --local --with-lead-enrich --with-email-finder --migrate
```

## CI / automation

Run `login` once on a machine with a browser, then set `OUTREACHMAGIC_AGENT_KEY` in your CI secrets from local config (never commit the key).

## Legacy platform repos

`hermes-outreachmagic`, `cursor-outreachmagic`, and `claude-code-outreachmagic` are deprecated. Existing installs continue to update via fallback logic until you reinstall from `outreachmagic/outreachmagic`.
