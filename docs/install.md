# Install Outreach Magic

**Agent install (canonical):** [AGENTS-INSTALL.md](../AGENTS-INSTALL.md) — published to
[raw.githubusercontent.com/.../main/AGENTS-INSTALL.md](https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md)
on the public repo.

Get install commands for your platform at [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding).

All platforms install from one repo: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

After install, connect with **device authorization** (browser — no pasting keys into terminal or chat):

```bash
python3 <skill-path>/scripts/pipeline.py login
```

**Full suite install (all platforms):** see [install-companions.md](./install-companions.md).

## Secure install (all platforms)

Pin a **release tag** (check latest: `pipeline.py update --check` or [GitHub releases](https://github.com/outreachmagic/outreachmagic/releases)). Download first — do not pipe remote scripts directly into `bash`.

```bash
OM_VERSION=v1.34.1

# Step 1 — download (does not execute)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o /tmp/om_install.sh

# Step 2 — verify integrity (recommended)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o /tmp/om_SHA256SUMS
(cd /tmp && grep ' install.sh$' om_SHA256SUMS | shasum -a 256 --check)

# Step 3 — optional: inspect before running
less /tmp/om_install.sh

# Step 4 — run from local copy
bash /tmp/om_install.sh --platform <PLATFORM> --tag "${OM_VERSION}"
```

Add `--with-lead-enrich --with-email-finder` for companions. On Hermes, add `--migrate-hermes-profiles` when fixing profile copies.

Preview without writing: `bash /tmp/om_install.sh --dry-run ...`

## Hermes

```bash
bash /tmp/om_install.sh --platform hermes --tag v1.34.1 --migrate-hermes-profiles
```

With companions:

```bash
bash /tmp/om_install.sh --platform hermes --tag v1.34.1 \
  --with-lead-enrich --with-email-finder --migrate-hermes-profiles
```

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

## Cursor

```bash
bash /tmp/om_install.sh --platform cursor --tag v1.34.1
```

With companions: add `--with-lead-enrich --with-email-finder`.

## Claude Code

```bash
bash /tmp/om_install.sh --platform claude --tag v1.34.1
```

With companions: add `--with-lead-enrich --with-email-finder`.

## Local development (monorepo)

```bash
bash install.sh --platform hermes --local --with-lead-enrich --with-email-finder --migrate
```

## CI / automation

Run `pipeline.py login` once on a machine with a browser, then set `OUTREACHMAGIC_AGENT_KEY` in your CI secrets from local config (never commit the key).
