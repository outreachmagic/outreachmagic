# SQL pitfalls

`pipeline.py sql` is the escape hatch, not the front door. Every rule below
exists because this schema has produced a wrong or non-terminating answer in
the shape described.

**Before writing SQL, check whether a canonical command already answers the
question.** It will be right, it will be fast, and it will agree with the
dashboard:

| Question | Command |
|---|---|
| Per-campaign sends, replies, sentiments, meetings, rates | `campaign-audit` |
| The same, broken out by day | `campaign-audit --by-day` |
| This window vs the one before it | `campaign-audit --compare-previous-window` |
| Which lead attributes perform best | `attribute-audit` |
| Sending domains, mailboxes, bounces | `deliverability` |
| Who replied, with the reply text | `campaign-replies` |
| What a column actually means | `pipeline.py schema <table>` |

---

## The query never returns

`pipeline.py sql` cancels a query after **10 seconds** (`--timeout N` to raise
it, `--timeout 0` to remove the ceiling). Before that ceiling existed, a 4-way
join over a multi-gigabyte database ran until the operator killed the process.

The cause is almost always the same: a large table joined with nothing to
reduce its rows first.

```sql
-- Hangs: events is scanned in full, then joined three ways.
SELECT c.name, COUNT(*)
  FROM campaigns c
  JOIN campaign_leads cl ON cl.campaign_id = c.id
  JOIN workspace_leads wl ON wl.lead_id = cl.lead_id
  JOIN events e ON e.campaign_id = c.id
 GROUP BY c.name;
```

```sql
-- Returns: events is filtered to the window first, in a CTE.
WITH recent AS (
    SELECT campaign_id, lead_id, event_type
      FROM events
     WHERE created_at >= datetime('now', '-14 days')
)
SELECT c.name, COUNT(*)
  FROM campaigns c
  JOIN recent e ON e.campaign_id = c.id
 WHERE c.workspace_id = ?
 GROUP BY c.name;
```

`--explain` prints the query plan and the warnings without running anything.
A `SCAN` over `events`, `relay_ingested` or `workspace_lead_events` is the
line to look for.

---

## `email_sent_count` is not sends in this campaign

`workspace_leads.email_sent_count` (and `linkedin_sent_count`,
`total_replies_count`) is a **lifetime counter for the whole workspace**. Using
it inside a per-campaign aggregation once turned 106 sends into 15,565.

Per-campaign sends come from `campaign-audit`, or from the read-only view:

```sql
SELECT lead_id, sends, replies FROM v_campaign_lead_sends WHERE campaign_id = ?;
```

---

## `current_sentiment_since` is not "when we first reached them"

It is **when the status last changed**. Windowing on it to find "positives in
the last 14 days" silently drops every lead whose status has not moved since,
which is why a 14-day question can come back empty on a workspace full of
positives.

To window on positives, use `first_positive_at` — or don't write it at all:
`campaign-audit --positive-anchor first` and `attribute-audit --since 14d`
already do.

---

## Every table is not workspace-scoped

`leads` has **no `workspace_id` column**; the relationship lives in
`workspace_leads`, and `events` reach their workspace through `campaigns`.
Pass `--workspace <slug>` and join the pre-scoped CTEs you get for free:
`ws_leads`, `ws_campaigns`, `ws_events`, `ws_sender_accounts`, `ws_workspace`.

Without it, `campaigns` and `workspace_leads` count every workspace at once.

---

## `truncated: true` means the answer is a sample

Ad-hoc queries are capped at 500 rows. The cap is reported in the JSON payload
and warned about on stderr, and a total computed from a truncated result is
wrong. Re-run with `--limit 0`, or aggregate in SQL instead of in your head.

---

## The warnings

`pipeline.py sql` prints these to stderr. They never block a query — a
deliberate full scan is legitimate — but each one has been a real wrong answer:

- `CROSS JOIN` with no relationship named.
- Three or more joins with no `WHERE` at all.
- `events` / `relay_ingested` / `workspace_lead_events` with no date, campaign
  or lead predicate.
- `campaigns` / `workspace_leads` with no `workspace_id` filter.
- `email_sent_count` in a query that also mentions campaigns.
