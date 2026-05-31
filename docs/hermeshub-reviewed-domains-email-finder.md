# Hermes Hub — Reviewed Domains issue (email-finder)

Copy into a Hermes Hub Reviewed Domains issue when submitting **email-finder**.

**Title:** Reviewed Domains request — api.trykitt.ai for email-finder skill

**Skill:** email-finder (`outreachmagic/email-finder`)

**Domains:**

- **api.trykitt.ai** — Email find + SMTP verify. POST JSON to `/job/find_email` with
  user-provided `x-api-key` header. Only called when the user runs `email_finder.py find` or
  `batch-find`. No data sent to Outreach Magic cloud except via separate outreachmagic
  save (local SQLite).

**Related:** outreachmagic (local save), lead-enrich (domain discovery upstream)
