# Security — lead-enrich

## Data boundaries

- **Local:** Reads lead records via outreachmagic `batch-lead-lookup` (your machine's
  SQLite database only; includes workspace tags for dedup).
- **External:** Calls **google.serper.dev** with your Serper API key when the
  agent runs `serper-search` or equivalent HTTP requests.
- **No** Outreach Magic cloud person-research API. **No** scraping of Google or LinkedIn HTML.

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

## Reporting vulnerabilities

Email security concerns to **security@outreachmagic.io** or see
[SECURITY.md](https://github.com/outreachmagic/outreachmagic/blob/main/SECURITY.md).
