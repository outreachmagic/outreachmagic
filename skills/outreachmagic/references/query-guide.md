# Outreach Magic — SQL query guide

Read-only analytics against your local SQLite database. Prefer **`pipeline.py query`** presets; use `query --sql` only when presets do not fit. **Writes** stay on mutation commands (`add-lead`, `import-profiles`, `sync`, etc.).

## Discover the schema first (do not guess)

```bash
python3 scripts/pipeline.py schema                 # every table + what it is for
python3 scripts/pipeline.py schema leads           # columns, types, foreign keys
python3 scripts/pipeline.py schema --relations     # join graph + workspace scoping
```

Full generated reference: [schema.md](schema.md). `PRAGMA` is rejected by the
read-only guard — `schema` exists so it does not need to be allowed.

**Workspace scoping differs per entity**, which is the single most expensive thing
to get wrong:

| Entity | How |
|--------|-----|
| `leads` | via `workspace_leads(workspace_id, lead_id)` — there is **no** `leads.workspace_id` |
| `campaigns` | direct `campaigns.workspace_id` |
| `events` | `events.campaign_id → campaigns.id → campaigns.workspace_id` |
| `sender_accounts` | via `workspace_sender_accounts` |
| `campaign_workspace_map` | relay routing only — **not** a join path for events |

Or let the CLI do it: `sql --workspace <slug>` pre-binds `ws_leads`,
`ws_campaigns`, `ws_events`, `ws_sender_accounts` and `ws_workspace`.

```bash
python3 scripts/pipeline.py sql \
  "SELECT stage, COUNT(*) n FROM ws_leads GROUP BY stage" --workspace acme --json
```

## Output contract

- The freshness line goes to **stderr**, and the same information is in the JSON
  body (`freshness_message`, `last_pull`, `stale_minutes`). If your harness merges
  the two streams, pass `--quiet` or set `OM_QUIET=1` — never strip it with a regex.
- Ad-hoc SQL caps at 500 rows, warns on stderr when the cap bites, and returns
  `truncated: true`. `--limit 0` removes the cap.
- `OM_WORKSPACE` fills in `--workspace` when omitted, and says so on stderr.

## Database path

```bash
python3 scripts/pipeline.py paths
```

Use the `database` field (typically `databases/outreachmagic.db` under the skill home).

Canonical DDL: [`scripts/schema.py`](../scripts/schema.py) (`SCHEMA_SQL`). Views: [`scripts/schema_views.py`](../scripts/schema_views.py).

## Core tables

| Table | Purpose |
|-------|---------|
| `leads` | One row per lead (email and/or LinkedIn identity) |
| `events` | **Primary timeline** — sent, reply, bounce, status labels (use for analytics) |
| `companies` | Canonical company records linked from leads |
| `campaigns` | Campaign names (`workspace \| campaign` prefix in multi-workspace setups) |
| `workspaces` | Multi-workspace routing (org-scoped) |
| `workspace_leads` | Per-workspace lead status, tags, activity |
| `campaign_workspace_map` | Platform campaign ID/name → workspace |
| `unmapped_campaign_queue` | Quarantined webhook events; resolutions sync via `sync` |
| `workspace_lead_events` | Workspace-scoped ingest audit — **subset**, not full relay volume |
| `relay_ingested` | Pull dedupe keys |

## Which table?

| Question | Use |
|----------|-----|
| Replies, engagement, campaign breakdowns, timelines | **`events`** + `campaigns` |
| Lead list with stage / sentiment filters | `pipeline.py show` or `lead-table` |
| Tag counts, LinkedIn connected-by-sender | `pipeline.py workspace summary --json` (add `--tags-only` on large workspaces) |
| Full message bodies / copy winners | `pipeline.py history`, `copy-insights` |
| Workspace ingest audit (rare) | `workspace_lead_events` — **not** for volume analytics |

`workspace_lead_events` is a workspace-scoped ingest subset. Relay volume and agent timelines live in **`events`**.

## Blessed CLI presets (fastest)

```bash
# Inbound engagement by campaign (last 48 hours) — acme example
python3 scripts/pipeline.py query engagement --workspace acme --since 48h --json

# Reply events only (platform reply types)
python3 scripts/pipeline.py query replies --workspace acme --since 7d --json

# Leads currently marked interested (latest status-bearing event)
python3 scripts/pipeline.py query interested --workspace acme --since 48h --json

# Custom prefix instead of workspace slug
python3 scripts/pipeline.py query engagement --campaign-prefix "acme |%" --since 48h --json
```

**All-time** campaign totals (no time window): `pipeline.py campaigns` or `stats --json`.

## Canonical SQL (engagement by campaign)

```sql
SELECT c.name, e.event_type, COUNT(*) AS count
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE c.name LIKE 'acme |%'
  AND e.created_at >= datetime('now', '-48 hours')
  AND lower(coalesce(e.direction, '')) = 'inbound'
GROUP BY c.name, e.event_type
ORDER BY count DESC;
```

Via CLI:

```bash
python3 scripts/pipeline.py query --sql "
SELECT c.name, e.event_type, COUNT(*) AS count
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE c.name LIKE ?
  AND e.created_at >= datetime('now', '-48 hours')
  AND lower(coalesce(e.direction, '')) = 'inbound'
GROUP BY c.name, e.event_type
ORDER BY count DESC
" --params '["acme |%"]' --json
```

## View: inbound events by campaign

After `init` or `pull` (migrate applies views):

```sql
SELECT campaign_name, event_type, COUNT(*) AS n
FROM v_inbound_events_by_campaign
WHERE campaign_name LIKE 'acme |%'
  AND created_at >= datetime('now', '-48 hours')
GROUP BY campaign_name, event_type
ORDER BY n DESC;
```

## Replies (registry-aligned types)

```sql
SELECT c.name, COUNT(*) AS replies
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE c.name LIKE 'acme |%'
  AND (
    lower(e.event_type) IN ('email_reply', 'linkedin_reply')
    OR (lower(e.direction) = 'inbound' AND lower(e.event_type) = 'email')
  )
  AND e.created_at >= datetime('now', '-7 days')
GROUP BY c.name
ORDER BY replies DESC;
```

Or: `pipeline.py query replies --workspace acme --since 7d --json`

## Recent events for one lead

```sql
SELECT event_type, direction, subject, created_at
FROM events
WHERE lead_id = ?
ORDER BY created_at DESC
LIMIT 50;
```

Or: `pipeline.py history --id <lead_id>`

## Stage counts

```sql
SELECT stage, COUNT(*) AS n FROM leads GROUP BY stage ORDER BY n DESC;
```

Or: `pipeline.py stats --json`

## Time filters

Presets accept: `48h`, `7d`, `2w`, `today`, or `YYYY-MM-DD`.

## Freshness

Local analytics do **not** require `pull` first. Read commands print `Data as of … (N minutes ago)` on stderr and include `last_pull` / `stale_minutes` in `--json`.

Run `pull` when the user needs the latest webhook events or says “refresh.” Use `pull --if-stale 5m` to skip redundant network round-trips in the same session.
