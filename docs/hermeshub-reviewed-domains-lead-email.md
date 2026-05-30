# Hermes Hub — Reviewed Domains issue (lead-email)

Copy into a Hermes Hub Reviewed Domains issue when submitting **lead-email**.

**Title:** Reviewed Domains request — api.trykitt.ai for lead-email skill

**Skill:** lead-email (`outreachmagic/lead-email`)

**Domains:**

- **api.trykitt.ai** — Email find + SMTP verify. POST JSON to `/job/find_email` with
  user-provided `x-api-key` header. Only called when the user runs `lead_email.py find` or
  `batch-find`. No data sent to Outreach Magic cloud except via separate outreachmagic
  save (local SQLite).

**Related:** outreachmagic (local save), lead-enrich (domain discovery upstream)
