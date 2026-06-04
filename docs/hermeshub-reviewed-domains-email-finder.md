# HermesHub Reviewed Domains request — email-finder

Open on [amanning3390/hermeshub](https://github.com/amanning3390/hermeshub/issues/new) before submitting the email-finder skill PR.

---

**Title:** Reviewed Domains request — trykitt, Icypeas, MillionVerifier for email-finder (v2+)

**Body:**

```
Planning to submit the email-finder companion skill (work email find + optional verify).

External domains:

- api.trykitt.ai — Email find + inline verify. POST /job/find_email with user x-api-key.
  Called only on explicit find / batch-find. No Outreach Magic cloud in this path.

- app.icypeas.com — Email find (async poll on read endpoint). Authorization: user API key.
  Second step in provider waterfall when trykitt misses or is out of credits.

- api.millionverifier.com — Optional bulk/single email verification (MILLIONVERIFIER_API_KEY).
  Only when user runs verify / verify-bulk / verify-download. Not required for find.

Saving and dedup use the outreachmagic skill (local SQLite via CLI). batch-lead-lookup
runs locally — no extra cloud API from email-finder.

Open source: github.com/outreachmagic/email-finder
Python stdlib only (no pip). Install via git clone or email_finder.py update.

SECURITY.md included. API keys stay in user ~/.hermes/.env or profile .env.
```

Link this issue in the HermesHub skill PR.
