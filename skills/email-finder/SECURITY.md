# Security — email-finder

## Data boundaries

- **Local:** Reads lead records via outreachmagic `pipeline.py history` (SQLite on your machine).
- **External:** Calls **api.trykitt.ai** with your API key when running `find` or `batch-find`.
- **Save:** Uses outreachmagic `import-profiles` and `verify-email` locally — same trust model as the core skill.

## Secrets

Store `TRYKITT_API_KEY` and `OUTREACHMAGIC_AGENT_KEY` in `~/.hermes/.env` (preferred) or shell env. Do not paste keys into chat.

## Reporting vulnerabilities

Contact **security@outreachmagic.io** or see outreachmagic [SECURITY.md](https://github.com/outreachmagic/hermes-outreachmagic/blob/main/SECURITY.md).
