# Install Outreach Magic

Get install commands for your platform at [app.outreachmagic.io/setup/agent](https://app.outreachmagic.io/setup/agent).

All platforms install from one repo: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

After install, connect with **device authorization** (browser — no pasting keys into terminal or chat):

```bash
python3 <skill-path>/scripts/pipeline.py login
```

**Full suite install (all platforms):** see [install-companions.md](./install-companions.md).

## Hermes

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes \
  --migrate
```

With optional companions (lead-enrich + email-finder):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder --migrate
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

## Cursor

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform cursor
```

With companions: add `--with-lead-enrich --with-email-finder`.

## Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform claude
```

With companions: add `--with-lead-enrich --with-email-finder`.

## Local development (monorepo)

```bash
bash install.sh --platform hermes --local --with-lead-enrich --with-email-finder --migrate
```

## CI / automation

Run `pipeline.py login` once on a machine with a browser, then set `OUTREACHMAGIC_AGENT_KEY` in your CI secrets from local config (never commit the key).
