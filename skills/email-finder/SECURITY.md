# Security — email-finder

## Data boundaries

- **Local:** Reads lead records via outreachmagic `pipeline.py history` (SQLite on your machine).
- **External:** Calls **api.trykitt.ai** and/or **app.icypeas.com** with your API keys when running `find` or `batch-find`.
- **Save:** Writes via outreachmagic locally (`apply-email-find-results` when every batch row has `lead_id` + workspace; otherwise chunked `import-profiles` and `verify-email`). No relay upload on save — same trust model as the core skill.

## Secrets

Store `TRYKITT_API_KEY`, `ICYPEAS_API_KEY`, and `OUTREACHMAGIC_AGENT_KEY` in `~/.hermes/.env` (preferred) or shell env. Do not paste keys into chat.

## Reporting vulnerabilities

Contact **security@outreachmagic.io** or see outreachmagic [SECURITY.md](https://github.com/outreachmagic/hermes-outreachmagic/blob/main/SECURITY.md).
