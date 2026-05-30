# HermesHub Reviewed Domains request — lead-enrich

Open on [amanning3390/hermeshub](https://github.com/amanning3390/hermeshub/issues/new) before submitting the lead-enrich skill PR.

---

**Title:** Reviewed Domains request — google.serper.dev + api.trykitt.ai for lead-enrich skill

**Body:**

```
Planning to submit the lead-enrich companion skill (person research via Serper).

External domain:

- google.serper.dev — Serper.dev Google Search API. Used only when the user
  (or agent) explicitly runs person/company research. Queries are built from
  name + company (+ optional job title). No HTML scraping.

- api.trykitt.ai — Optional email find + SMTP verify (Phase 5). Only when
  TRYKITT_API_KEY is set and the user requests email finding. POST JSON with
  name + domain + LinkedIn URL; no HTML scraping.

The skill optionally integrates with the outreachmagic skill for local SQLite
dedup (zero API calls when a matching lead already has LinkedIn). Saving uses
outreachmagic's local CLI — not a separate cloud API from this skill.

Open source: github.com/outreachmagic/lead-enrich
Python stdlib only (no pip). Install via git clone.

SECURITY.md included. Serper API key stays in user config / env.
```

Link this issue in the HermesHub skill PR. outreachmagic's domains (`api.outreachmagic.io`) are covered by the core skill submission.
