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

- **Inbound webhooks** from sending platforms (Smartlead, Instantly, Heyreach, PlusVibe, EmailBison, etc.) arrive at `api.outreachmagic.io`, are buffered briefly for pull/ack, and are imported into your local database when you run `pull`.

## External network calls

| Domain | Purpose | Auth | Data sent |
|--------|---------|------|-----------|
| `api.outreachmagic.io` | Relay **pull** (import webhooks + agent snapshots) | Bearer token in URL path / headers | Token, pull cursor; returns event payloads for local import |
| `api.outreachmagic.io` | Relay **push** (`pipeline.py sync` only) | `Authorization: Bearer <om_agent_…>` | Lead snapshots and local agent events the user chose to sync — **never** sent on `import-profiles`, `init`, or `pull` |
| `app.outreachmagic.io` | Portal API (tokens, billing, routing config sync) | Bearer token | Routing config, account metadata — **not** full local DB export |
| `app.outreachmagic.io` | Device authorization (`POST /api/device/code`, `/token`) during `pipeline.py login` only | None / device code | Client label, platform, hostname — **no** lead data |
| `app.outreachmagic.io` | Portal **`POST /api/agent/db-health`** (end of explicit `sync` only) | Bearer agent key | Aggregate local DB stats only (~1 KB): file size, row counts, top table names, health status — **no** emails, bodies, or lead names |
| `api.github.com` | Latest release lookup for update checks | None | Public releases API only (read-only; at most once per hour) |
| `raw.githubusercontent.com` | Tagged release downloads (`pipeline.py update`) | None | Only on explicit user-triggered update |

### No automatic upload

- **`import-profiles`** — local SQLite only; sets `cloud_pending` but does **not** call the network.
- **`pull`** — downloads from relay only (plus optional routing config **pull** from portal).
- **`sync`** — the **only** command that POSTs lead data to `api.outreachmagic.io/push`. The agent or user must run it explicitly. At the end of the same `sync`, the CLI may POST aggregate DB health to the portal (throttled ~6h); use `sync --no-health-report` to skip.
- **`db-health`** — local inspection only unless `--push` is passed explicitly.
- **`query`** — read-only SQLite analytics (presets or `SELECT`/`WITH` only). Local DB only; no network. Mutations remain on other commands only.
- **`archive`** — local export/purge only; never calls the network.
- Hermes cron examples in docs use `pull --cron` (inbound only), not `sync`.

Override portal API: `"api_base_url"` in config (default `https://app.outreachmagic.io`).

Relay URL is fixed in code (`api.outreachmagic.io`). Updates install from GitHub release tags; local dev uses `dev_repo` / `dev_update_url` in config.

## Credentials

- **Never** commit tokens, API keys, or `.env` files.
- Store your agent key only in `outreachmagic_config.json` (local) or `OUTREACHMAGIC_AGENT_KEY` in the environment. Run `pipeline.py login` once (browser device authorization). Do not pass keys on `curl | bash` install lines.
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
- `api_base_url` — portal API host (default `https://app.outreachmagic.io`)

Install from a pinned release (download → verify → run — never `curl | bash`):

```bash
OM_VERSION=v1.2.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

API keys for companion skills are managed in the Outreach Magic portal and synced via `pipeline.py sync-secrets` — do not store keys in shell config or local `.env` files.

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

See [outreachmagic-brand/copy/hub/registry-publish.md](../outreachmagic-brand/copy/hub/registry-publish.md) for the registry submission checklist.
