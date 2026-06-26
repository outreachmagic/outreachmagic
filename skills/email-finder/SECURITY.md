# Security — email-finder

## Data boundaries

- **Local:** Reads lead records via outreachmagic `pipeline.py history` (SQLite on your machine).
- **External:** Calls **api.trykitt.ai** and/or **app.icypeas.com** with your API keys when running `find` or `batch-find`.
- **Save:** Writes via outreachmagic locally (`apply-email-find-results` when every batch row has `lead_id` + workspace; otherwise chunked `import-profiles` and `verify-email`). No relay upload on save — same trust model as the core skill.

## Secrets

API keys (TryKitt, Icypeas, MillionVerifier) are managed in the **Outreach Magic portal** and synced to `config/agent_secrets.env` via `pipeline.py sync-secrets`. Do not store keys in shell config, `config.json`, or local `.env` files for interactive use.

**CI/automation only:** set `OM_ALLOW_LOCAL_API_KEYS=1` to allow legacy env/config key loading. Never use in agent chat contexts.

Do not paste keys into chat.

## Reporting vulnerabilities

Contact **security@outreachmagic.io** or see outreachmagic [SECURITY.md](https://github.com/outreachmagic/outreachmagic/blob/main/SECURITY.md).
