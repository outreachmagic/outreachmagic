# Outreach Magic — Local SQLite Schema

Canonical schema is defined in `scripts/pipeline.py` (`SCHEMA_SQL`). This reference is for agents querying the database directly.

**Database path:** `~/.hermes/skills/outreachmagic/databases/outreachmagic.db`

## Core tables

| Table | Purpose |
|-------|---------|
| `leads` | One row per lead (email and/or LinkedIn identity) |
| `events` | Outreach timeline (sent, reply, bounce, status labels) |
| `companies` | Canonical company records linked from leads |
| `campaigns` | Campaign names from relay imports |
| `workspaces` | Multi-workspace routing (org-scoped) |
| `campaign_maps` | Platform campaign ID → workspace |
| `quarantine_queue` | Events awaiting workspace assignment |

Use `pipeline.py show`, `history`, `stats`, and `campaigns` instead of raw SQL when possible.
