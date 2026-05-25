# Security Policy

Outreach Magic takes the security of this Hermes skill seriously. This document describes what data stays local, what leaves your machine, and how to report vulnerabilities.

## Supported versions

| Version | Supported |
|---------|-----------|
| Latest release on `main` | Yes |
| Older installed copies | Update via `pipeline.py update` or `hermes skills update` |

Check your installed version:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

## Architecture & data boundaries

### Stays on your machine (local only)

- **SQLite database** — `~/.hermes/skills/outreachmagic/databases/outreachmagic.db`
  - Leads, companies, events, campaigns, workspace routing state mirrored locally
- **Config file** — `~/.hermes/skills/outreachmagic/config/outreachmagic_config.json`
  - Relay token, pull cursor, optional API overrides
  - Created with restrictive permissions by `init` / `sync-local.sh` (config dir `700`, config file `600` when supported by the OS)
- **Hermes-originated outreach logs** — written locally when the agent uses the skill; no cloud required for free tier

### Stored on Outreach Magic servers

We store **account and operational metadata only**, not your full outreach message archive as a searchable cloud database:

- API / relay tokens and usage counts
- Billing and subscription state (portal)
- Workspace routing configuration when cloud sync is enabled (campaign → workspace maps)

We do **not** use our servers as the long-term store for webhook payload content used for agent queries — that data is synced into your local SQLite file.

### Pass-through (not stored server-side as archive)

- **Inbound webhooks** from sending platforms (Smartlead, Instantly, Heyreach, PlusVibe, EmailBison, etc.) arrive at `api.outreachmagic.io` (Cloudflare Worker), are buffered briefly for pull/ack, and are imported into your local database when you run `pull`.

## External network calls

| Domain | Purpose | Auth | Data sent |
|--------|---------|------|-----------|
| `api.outreachmagic.io` | Relay pull, ack, webhook routing | Bearer token in URL path / headers | Token, pull cursor; returns event payloads for local import |
| `dev.outreachmagic.io` | Portal API (tokens, billing, routing config sync) | Bearer token | Routing config, account metadata — **not** full local DB export |
| `api.github.com` | Latest release lookup for update checks | None | Public releases API only |
| `raw.githubusercontent.com` | Tagged release downloads (`pipeline.py update`) | None | Only on explicit user-triggered update |

Override portal API: `"api_base_url"` in config (default `https://dev.outreachmagic.io`).

Relay URL is fixed in code (`api.outreachmagic.io`). Updates install from GitHub release tags; local dev uses `dev_repo` / `dev_update_url` in config.

## Credentials

- **Never** commit tokens, API keys, or `.env` files.
- Store relay tokens only in `outreachmagic_config.json` (local) or pass via `connect --key` once.
- Do not paste tokens into SKILL.md, issues, or chat logs.

## Skill updates

Updates are **user-triggered only**. The CLI may print a notice when a newer GitHub release exists (checked at most once per hour). It never downloads or replaces scripts in the background.

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
hermes skills update
```

Releases are pinned to GitHub tags (e.g. `v1.4.5`), not the moving `main` branch. When a manifest is published with the release, downloads are verified with SHA256 checksums.

Development overrides (in `outreachmagic_config.json`, not environment variables):

- `data_root` — e.g. `~/.claude` for Claude Code (default `~/.hermes`)
- `dev_repo` — path to a local clone for `pipeline.py update`
- `dev_update_url` — custom raw URL base for dev/testing only
- `api_base_url` — portal API host (default `https://dev.outreachmagic.io`)

Install through Hermes when possible — Hermes runs its own security scan on hub installs:

```bash
hermes skills install outreachmagic/hermes-agent/skills/outreachmagic
```

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Email: **security@outreachmagic.io**

Include:

- Description of the issue and impact
- Steps to reproduce
- Affected version (`pipeline.py version`)
- Whether data exfiltration or token exposure is possible

We aim to acknowledge reports within **3 business days** and provide a remediation timeline for confirmed issues.

## Registry & scanning

Before submitting to HermesHub, we run their SkillScan locally:

```bash
bash scripts/skill-scan.sh
```

See [docs/SKILL_REGISTRY_PLAN.md](docs/SKILL_REGISTRY_PLAN.md) for the full security rollout checklist.
