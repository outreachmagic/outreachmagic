# HermesHub Reviewed Domains request

Open this issue on [amanning3390/hermeshub](https://github.com/amanning3390/hermeshub/issues/new) **before** submitting the skill PR.

---

**Title:** Reviewed Domains request — outreachmagic.io for Outreach Magic skill submission

**Body:**

```
Hey HermesHub maintainers,

Planning to submit the Outreach Magic skill to the registry shortly and
wanted to request domain review first per the contribution guidelines.

The skill makes external calls to these domains:

- api.outreachmagic.io — Cloudflare Worker relay. Handles:
  - /{platform}/{token} — inbound webhook payloads from outreach platforms
    (Smartlead, Instantly, Heyreach, PlusVibe, EmailBison, etc.). Events are
    queued for pull/ack; we do not operate a searchable cloud archive of message
    content — data lands in the user's local SQLite database.
  - /pull/{token} — authenticated pull endpoint for the CLI to import events.
  - /pull/{token}/ack — acknowledges imported event IDs.

- app.outreachmagic.io — user portal and API. Used for:
  - Token generation and account management
  - Billing / subscription status
  - Workspace routing config sync (campaign → workspace maps) when connected

The skill is open source: github.com/outreachmagic/outreachmagic
(skill path: skills/outreachmagic/).

Users install via `install.sh --platform <name>` from github.com/outreachmagic/outreachmagic.
SECURITY.md and LICENSE are in the repo root.

Happy to provide any additional info you need. Thanks!
```

---

After the issue is opened, link it in your HermesHub skill PR description.
