# Security — lead-enrich

## Data boundaries

- **Local:** Reads lead records via outreachmagic `batch-lead-lookup` (your machine's
  SQLite database only; includes workspace tags for dedup).
- **External:** Calls **google.serper.dev** with your Serper API key when the
  agent runs `serper-search` or equivalent HTTP requests.
- **No** Outreach Magic cloud person-research API. **No** scraping of Google or LinkedIn HTML.

## Secrets

- Store `SERPER_API_KEY` in `~/.hermes/.env` (preferred on Hermes), `config.json`
  (gitignored), or shell env. `OUTREACHMAGIC_AGENT_KEY` belongs in the same file.
- CLI output masks keys in `config` and uses `$SERPER_API_KEY` in generated curl
  examples — do not paste live keys into chat.

## Optional dependency

outreachmagic is recommended but not required for search/format helpers. When
installed, saving uses `import-profiles` / `tag bulk` locally — same trust model as the core skill.

## Reporting vulnerabilities

Email security concerns for Outreach Magic products to the address in
[SECURITY.md](https://github.com/outreachmagic/hermes-outreachmagic/blob/main/SECURITY.md)
for the core outreachmagic skill, or contact **security@outreachmagic.io**.
