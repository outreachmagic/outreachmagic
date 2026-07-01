# Security — lead-enrich

## Data boundaries

- **Local:** Reads lead records via outreachmagic `batch-lead-lookup` (your machine's
  SQLite database only; includes workspace tags for dedup).
- **External:** Calls **google.serper.dev** with your Serper API key when the
  agent runs `serper-search` or equivalent HTTP requests.
- **External:** **api.github.com** and **raw.githubusercontent.com** are contacted
  only during `enrich.py update` for version checks and checksum-verified downloads.
- **No** Outreach Magic cloud person-research API. **No** scraping of Google or LinkedIn HTML.

## Network call table

| Domain | Purpose | Auth method | Data sent |
|--------|---------|-------------|-----------|
| `google.serper.dev` | Google Search API — company discovery and LinkedIn profile lookup | `X-API-KEY` header | Search query text (person name, company name, role) |
| `api.github.com` | Check latest release version for skill update | None (unauthenticated) | User-Agent header only |
| `raw.githubusercontent.com` | Download update-manifest.json, SHA256SUMS, and source files during update | None (unauthenticated) | URL path to public file |

## Secrets

`SERPER_API_KEY` is managed in the **Outreach Magic portal** and synced to
`config/agent_secrets.env` via `pipeline.py sync-secrets`. Do not store keys in shell
config or local `.env` files for interactive installs.

**CI/automation only:** set `OM_ALLOW_LOCAL_API_KEYS=1` to allow legacy env/config key loading.

`OUTREACHMAGIC_AGENT_KEY` is set via `pipeline.py login` (stored in outreachmagic config).

CLI output masks keys in `config` — do not paste live keys into chat.

## Optional dependency

outreachmagic is recommended but not required for search/format helpers. When
installed, saving uses `import-profiles` / `tag bulk` locally — same trust model as the core skill.

## Update mechanism

Skill updates are user-triggered via `enrich.py update`. Each release file is
verified against SHA256 checksums from `update-manifest.json` before replacement.
The manifest itself is cross-checked against `SHA256SUMS` when available.

## Reporting vulnerabilities

Email security concerns to **security@outreachmagic.io** or see
[SECURITY.md](https://github.com/outreachmagic/outreachmagic/blob/main/SECURITY.md).
