# Outreach Magic — SQL query guide

Read-only analytics against your local SQLite database. Prefer **`pipeline.py query`** presets; use `query --sql` only when presets do not fit. **Writes** stay on mutation commands (`add-lead`, `import-profiles`, `sync`, etc.).

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
| `unmapped_campaign_queue` | Quarantined relay events; resolutions sync via `sync` |
| `workspace_lead_events` | Workspace-scoped ingest audit — **subset**, not full relay volume |
| `relay_ingested` | Pull dedupe keys |

## Which table?

| Question | Use |
|----------|-----|
| Replies, engagement, campaign breakdowns, timelines | **`events`** + `campaigns` |
| Lead list with stage / sentiment filters | `pipeline.py show` or `lead-table` |
| Tag counts, LinkedIn connected-by-sender | `pipeline.py workspace summary --json` |
| Full message bodies / copy winners | `pipeline.py history`, `copy-insights` |
| Workspace ingest audit (rare) | `workspace_lead_events` — **not** for volume analytics |

`workspace_lead_events` is a workspace-scoped ingest subset. Relay volume and agent timelines live in **`events`**.

## Blessed CLI presets (fastest)

```bash
# Inbound engagement by campaign (last 48 hours) — popcam example
python3 scripts/pipeline.py query engagement --workspace popcam --since 48h --json

# Reply events only (platform reply types)
python3 scripts/pipeline.py query replies --workspace popcam --since 7d --json

# Leads currently marked interested (latest status-bearing event)
python3 scripts/pipeline.py query interested --workspace popcam --since 48h --json

# Custom prefix instead of workspace slug
python3 scripts/pipeline.py query engagement --campaign-prefix "popcam |%" --since 48h --json
```

**All-time** campaign totals (no time window): `pipeline.py campaigns` or `stats --json`.

## Canonical SQL (engagement by campaign)

```sql
SELECT c.name, e.event_type, COUNT(*) AS count
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE c.name LIKE 'popcam |%'
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
" --params '["popcam |%"]' --json
```

## View: inbound events by campaign

After `init` or `pull` (migrate applies views):

```sql
SELECT campaign_name, event_type, COUNT(*) AS n
FROM v_inbound_events_by_campaign
WHERE campaign_name LIKE 'popcam |%'
  AND created_at >= datetime('now', '-48 hours')
GROUP BY campaign_name, event_type
ORDER BY n DESC;
```

## Replies (registry-aligned types)

```sql
SELECT c.name, COUNT(*) AS replies
FROM events e
LEFT JOIN campaigns c ON e.campaign_id = c.id
WHERE c.name LIKE 'popcam |%'
  AND (
    lower(e.event_type) IN ('email_reply', 'linkedin_reply')
    OR (lower(e.direction) = 'inbound' AND lower(e.event_type) = 'email')
  )
  AND e.created_at >= datetime('now', '-7 days')
GROUP BY c.name
ORDER BY replies DESC;
```

Or: `pipeline.py query replies --workspace popcam --since 7d --json`

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

Local analytics do **not** require `pull` first. Run `pull` when the user needs the latest relay events or says “refresh.”
