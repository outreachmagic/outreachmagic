# Campaign Stats Export — Google Sheets Specification

> **Feature:** `pipeline.py sheets campaign-stats`
> Generates a 4-sheet Google Workbook with all campaign performance data from Outreach Magic's local SQLite database.

---

## Overview

This feature produces a **4-sheet Google Workbook** that gives anyone (client, team lead, operations manager) an instant view of every campaign's health -- send volume, reply rates, OOO detection, lead sentiment, conversion funnels, and daily breakdowns -- in a format that's easy to scan, sort, and present.

The workbook is **fully dynamic**: it queries the database by workspace slug, uses whatever campaign names exist, and reflects whatever lead status values your sequencer emits. The only hardcoded dimension is **lead sentiment** -- the sentiment field (`lead_status_sentiment`) is always one of: `positive`, `negative`, `autoreply`, `invalid`.

Every sheet tab includes a **settings note in cell A1** with workspace, time window, generation timestamp, and timezone offset.

---

## Command Interface

```bash
# Export all campaign stats for a workspace (last 14 days by default)
pipeline.py sheets campaign-stats --workspace popcam

# Custom time window
pipeline.py sheets campaign-stats --workspace popcam --since 30d

# All-time (no time filter)
pipeline.py sheets campaign-stats --workspace popcam --since all

# Reuse the same sheet (cron-friendly -- saves and reuses the sheet ID)
pipeline.py sheets campaign-stats --workspace popcam --since 14d --update

# Custom share email
pipeline.py sheets campaign-stats --workspace popcam --share-email client@example.com

# JSON preview (debug / review before sheet creation)
pipeline.py sheets campaign-stats --workspace popcam --since 14d --dry-run --json
```

---

## Database Tables Used

| Table | Purpose |
|---|---|
| `events` | Primary timeline. Every email sent, reply, bounce, and status label event lives here. |
| `campaigns` | Campaign names. Convention: `"{workspace} \| {campaign_name}"` |
| `leads` | One row per lead. Used when we need lead names for reply highlights. |
| `workspace_leads` | Per-workspace lead status and sentiment (alternative source when `events.metadata_json` is unavailable). |

### Key `events` columns for this export

| Column | Use |
|---|---|
| `event_type` | `email_sent`, `email_reply`, `email_bounce`, `linkedin_connect`, `linkedin_accept`, `linkedin_message`, `linkedin_reply`, `lead_marked_as_interested`, etc. |
| `direction` | `outbound` (sent) or `inbound` (reply) |
| `campaign_id` | FK to `campaigns.id` |
| `created_at` | Event timestamp -- used for time-window filtering and daily breakdown |
| `metadata_json` | JSON blob with `lead_status_sentiment`, `lead_status_raw`, `lead_status_display`, `is_auto_reply` |

### Key `metadata_json` fields

```json
{
  "lead_status_sentiment": "positive",
  "lead_status_raw": "interested",
  "lead_status_display": "Interested - Meeting Booked",
  "is_auto_reply": 0
}
```

- `is_auto_reply` = `1` means this was an out-of-office or auto-reply (used for OOO detection)
- `lead_status_sentiment` is the canonical sentiment classifier (always one of the values in the Sentiment Reference section below)
- `lead_status_display` is the sequencer-specific label (varies by platform -- includes both interested and not-interested variants)

---

## Sentiment Reference (Canonical Values)

These are the only `lead_status_sentiment` values that exist in the database. Do not hardcode campaign or lead-status names -- but do hardcode these sentiment values:

| Sentiment | Meaning |
|---|---|
| `positive` | Lead expressed interest, wants more info |
| `negative` | Lead not interested, "stop emailing" |
| `autoreply` | Out-of-office or automatic reply detected |
| `invalid` | Bounced / wrong person / bad email |

The export should use these values for the sentiment columns. Campaign names, lead status display labels, and other string fields vary per workspace and must be read dynamically.

---

## Sheet 1: Campaign Overview

This is the **master table** -- one row per campaign. Sorted by activity status (active first), then by reply rate descending.

### Columns

```
Campaign | Status | Sent | Delivered | Bounced | Bounce % | Total Replies | OOO | Human | Reply % | LI Connects | LI Accepts | Accept % | LI Messages | LI Replies | Interested | Not Interested | Sentiment Rate | Last Activity
```

### Column Definitions

| # | Column | Source | Notes |
|---|---|---|---|
| 1 | **Campaign** | `campaigns.name` -- strip the workspace prefix. If name is `"popcam \| nace"`, display as `"nace"`. | Remove `"{workspace} \| "` prefix for display. |
| 2 | **Status** | Computed: if `sent_count > 0` in window -> `"active"`. If `sent_count = 0` but campaign has sends outside window -> `"paused"`. If `sent_count = 0` and no sends ever -> `"exhausted"`. | See Status Detection below. |
| 3 | **Sent** | `COUNT(*) FROM events WHERE event_type IN ('email_sent', 'email_sent_auto') AND direction = 'outbound'` | |
| 4 | **Delivered** | `sent - bounced` | Computed column. |
| 5 | **Bounced** | `COUNT(*) FROM events WHERE event_type IN ('email_bounce', 'bounced_email', 'email_bounced')` | |
| 6 | **Bounce %** | `bounced / sent * 100` | Formula in sheet. |
| 7 | **Total Replies** | `COUNT(*) FROM events WHERE event_type IN ('email_reply', 'linkedin_reply') OR (direction='inbound' AND event_type='email')` | Includes OOO. Uses `reply_event_sql_condition()` from platform_registry (see appendix). |
| 8 | **OOO** | `COUNT(*) FROM events WHERE (reply condition above) AND CAST(json_extract(metadata_json, '$.is_auto_reply') AS INTEGER) = 1` | Auto-replies flagged by the sequencer. |
| 9 | **Human** | `Total Replies - OOO` | Computed column. Human replies only. |
| 10 | **Reply %** | `Total Replies / Delivered * 100` | Formula in sheet. |
| 11 | **LI Connects** | `COUNT(*) FROM events WHERE event_type = 'linkedin_connect'` | |
| 12 | **LI Accepts** | `COUNT(*) FROM events WHERE event_type = 'linkedin_accept'` | |
| 13 | **Accept %** | `LI Accepts / LI Connects * 100` | Formula. |
| 14 | **LI Messages** | `COUNT(*) FROM events WHERE event_type = 'linkedin_message'` | |
| 15 | **LI Replies** | `COUNT(*) FROM events WHERE event_type = 'linkedin_reply'` | |
| 16 | **Interested** | Number of unique leads whose latest status-bearing event in this window has `lead_status_sentiment IN ('positive', 'interested')` | Use the `LATEST_STATUS_CTE` pattern from read_queries.py. |
| 17 | **Not Interested** | Same as above but `lead_status_sentiment IN ('negative', 'not_interested')` | |
| 18 | **Sentiment Rate** | `Interested / (Interested + Not Interested) * 100` | Formula. Only when denominator > 0. |
| 19 | **Last Activity** | `MAX(created_at)` for any event type in this window | Display as `"Jun 13"` format. |

### Status Detection Logic

```python
def campaign_status(name: str, sends_in_window: int, sends_outside_window: int) -> str:
    if sends_in_window > 0:
        return "active"
    if sends_outside_window > 0:
        return "paused"
    return "exhausted"
```

- `sends_in_window`: count of `email_sent` / `email_sent_auto` events with `created_at >= [since]`
- `sends_outside_window`: count of `email_sent` / `email_sent_auto` events with `created_at < [since]` (any time before the window)

### SQL: Campaign Overview Row (one campaign)

```sql
WITH campaign_ids AS (
  SELECT id, name FROM campaigns
  WHERE name LIKE '{workspace} |%'
),
window_sends AS (
  SELECT campaign_id, COUNT(*) AS sent_count
  FROM events WHERE event_type IN ('email_sent', 'email_sent_auto')
    AND campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
),
all_sends AS (
  SELECT campaign_id, COUNT(*) AS total_sent
  FROM events WHERE event_type IN ('email_sent', 'email_sent_auto')
    AND campaign_id IN (SELECT id FROM campaign_ids)
  GROUP BY campaign_id
),
bounces AS (
  SELECT campaign_id, COUNT(*) AS bounce_count
  FROM events WHERE event_type IN ('email_bounce', 'bounced_email', 'email_bounced')
    AND campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
),
replies AS (
  SELECT campaign_id, COUNT(*) AS reply_count
  FROM events e WHERE ({reply_condition})
    AND campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
),
ooo AS (
  SELECT campaign_id, COUNT(*) AS ooo_count
  FROM events e WHERE ({reply_condition})
    AND CAST(json_extract(metadata_json, '$.is_auto_reply') AS INTEGER) = 1
    AND campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
),
li_connects AS (
  SELECT campaign_id, COUNT(*) AS li_connect_count
  FROM events WHERE event_type = 'linkedin_connect'
    AND campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
),
-- repeat for li_accepts, li_messages, li_replies (same pattern)

last_activity AS (
  SELECT campaign_id, MAX(created_at) AS last_at
  FROM events
  WHERE campaign_id IN (SELECT id FROM campaign_ids)
    AND created_at >= {since_expr}
  GROUP BY campaign_id
)
SELECT
  c.name,
  COALESCE(s.sent_count, 0) AS sent,
  COALESCE(b.bounce_count, 0) AS bounced,
  COALESCE(r.reply_count, 0) AS total_replies,
  COALESCE(o.ooo_count, 0) AS ooo,
  COALESCE(lc.li_connect_count, 0) AS li_connects,
  COALESCE(la.li_accept_count, 0) AS li_accepts,
  COALESCE(lm.li_message_count, 0) AS li_messages,
  COALESCE(lr.li_reply_count, 0) AS li_replies,
  laa.last_at AS last_activity,
  COALESCE(ast.total_sent, 0) AS all_time_sent  -- for status detection
FROM campaign_ids c
LEFT JOIN window_sends s ON c.id = s.campaign_id
LEFT JOIN all_sends ast ON c.id = ast.campaign_id
LEFT JOIN bounces b ON c.id = b.campaign_id
LEFT JOIN replies r ON c.id = r.campaign_id
LEFT JOIN ooo o ON c.id = o.campaign_id
LEFT JOIN li_connects lc ON c.id = lc.campaign_id
LEFT JOIN li_accepts la ON c.id = la.campaign_id
LEFT JOIN li_messages lm ON c.id = lm.campaign_id
LEFT JOIN li_replies lr ON c.id = lr.campaign_id
LEFT JOIN last_activity laa ON c.id = laa.campaign_id
ORDER BY
  CASE WHEN s.sent_count > 0 THEN 0 ELSE 1 END,
  COALESCE(r.reply_count, 0) DESC;
```

### Sentiment Per Campaign (Interested / Not Interested)

Uses the `LATEST_STATUS_CTE` pattern from `read_queries.py`:

```sql
WITH ranked_status AS (
  SELECT
    e.lead_id,
    LOWER(JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment')) AS current_sentiment,
    e.campaign_id,
    ROW_NUMBER() OVER (
      PARTITION BY e.lead_id
      ORDER BY e.created_at DESC, e.id DESC
    ) AS rn
  FROM events e
  WHERE JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
    AND e.created_at >= {since_expr}
)
SELECT
  c.name AS campaign,
  rs.current_sentiment AS sentiment,
  COUNT(DISTINCT rs.lead_id) AS lead_count
FROM ranked_status rs
JOIN campaigns c ON rs.campaign_id = c.id
WHERE rs.rn = 1
  AND rs.current_sentiment IN ('positive', 'interested', 'negative', 'not_interested')
  AND c.name LIKE '{workspace} |%'
GROUP BY c.name, rs.current_sentiment
ORDER BY campaign, lead_count DESC;
```

Then in application code, split into `interested` and `not_interested`:

```python
interested_sentiments = {"positive", "interested"}
not_interested_sentiments = {"negative", "not_interested"}

for row in sentiment_rows:
    if row.sentiment in interested_sentiments:
        counts[(row.campaign, "interested")] += row.lead_count
    elif row.sentiment in not_interested_sentiments:
        counts[(row.campaign, "not_interested")] += row.lead_count
```

---

## Sheet 2: Campaign Funnels

One **section per active campaign** (campaigns with `sent > 0` in the window). Each section is a vertical funnel table showing conversion from initial touch through to interested/not interested.

### Funnel Table Structure

| Stage | Volume | % of Sent |
|---|---|---|
| Emails Sent (unique) | 264 | 100% |
| Emails Delivered | 262 | 99.2% |
| Bounced | 2 | 0.8% |
| Total Replies | 11 | 4.2% |
| OOO Auto-Replies | 2 | 0.8% |
| Human Replies | 9 | 3.4% |
| LinkedIn Connects | 415 | (N/A) |
| LinkedIn Accepts | 182 | 43.9% |
| LinkedIn Messages | 206 | (N/A) |
| LinkedIn Replies | 2 | 1.0% |
| Interested Leads | 3 | 1.1% |
| Not Interested | 2 | 0.8% |

### Per-Campaign SQL

```sql
WITH campaign AS (
  SELECT id, name FROM campaigns
  WHERE name = '{workspace} |{campaign_name}'
  LIMIT 1
),
unique_sent AS (
  SELECT COUNT(DISTINCT lead_id) AS val FROM events
  WHERE campaign_id = (SELECT id FROM campaign)
    AND event_type IN ('email_sent', 'email_sent_auto')
    AND created_at >= {since_expr}
),
delivered AS (
  SELECT COUNT(*) AS val FROM events
  WHERE campaign_id = (SELECT id FROM campaign)
    AND event_type IN ('email_sent', 'email_sent_auto')
    AND created_at >= {since_expr}
),
-- Reuse bounce, reply, OOO, LinkedIn CTEs from Sheet 1 scoped to this campaign
-- ...
SELECT
  (SELECT val FROM unique_sent) AS unique_sent,
  (SELECT val FROM delivered) AS delivered,
  (SELECT COALESCE(COUNT(*), 0) FROM events
    WHERE campaign_id = (SELECT id FROM campaign)
      AND event_type IN ('email_bounce', 'bounced_email', 'email_bounced')
      AND created_at >= {since_expr}) AS bounced,
  (SELECT COALESCE(COUNT(*), 0) FROM events e
    WHERE ({reply_condition})
      AND campaign_id = (SELECT id FROM campaign)
      AND created_at >= {since_expr}) AS total_replies,
  -- ... OOO, LinkedIn counts ...
```

### Output Format

For each active campaign, write a **named section** in the sheet:

```
Row 1: [Campaign Name] -- Funnel
Row 2: Stage | Volume | % of Sent
Row 3+: data rows
Row N: (blank row)
```

---

## Sheet 3: Lead Sentiment Summary

A compact pivot table showing sentiment distribution across campaigns that have any sentiment-tagged leads.

| Campaign | Positive | Interested | Neutral | Negative | Not Interested | Invalid | Total Tagged | Positivity Rate |
|---|---|---|---|---|---|---|---|---|
| nace | 1 | 2 | 0 | 1 | 1 | 0 | 5 | 60% |
| headshot lounge | 0 | 0 | 0 | 3 | 0 | 0 | 3 | 0% |
| nonprofit event outreach | 3 | 0 | 0 | 1 | 1 | 0 | 5 | 60% |

### SQL

```sql
WITH ranked_status AS (
  SELECT
    e.lead_id,
    LOWER(JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment')) AS sentiment,
    e.campaign_id,
    ROW_NUMBER() OVER (
      PARTITION BY e.lead_id
      ORDER BY e.created_at DESC, e.id DESC
    ) AS rn
  FROM events e
  WHERE JSON_EXTRACT(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
    AND e.created_at >= {since_expr}
)
SELECT
  c.name AS campaign,
  rs.sentiment,
  COUNT(*) AS lead_count
FROM ranked_status rs
JOIN campaigns c ON rs.campaign_id = c.id
WHERE rs.rn = 1
  AND c.name LIKE '{workspace} |%'
GROUP BY c.name, rs.sentiment
ORDER BY campaign, lead_count DESC;
```

Then pivot in application code -- sentiment values become columns. The column headers are **always** the 6 canonical sentiment values from the Sentiment Reference above, even if all counts are zero for a given sentiment.

---

## Sheet 4: Daily Breakdown

One row per (campaign, day) combination with the same core metrics as Campaign Overview, split by calendar day. Timezone offset is configurable (default -4 for EDT / UTC-4).

### Columns

```
Campaign | Day | Sent | Delivered | Bounced | Bounce % | Total Replies | OOO | Human | Reply % | LI Connects | LI Accepts | Accept % | LI Messages | LI Replies
```

Column definitions match the Campaign Overview (columns 1 and 3-15). No Status, sentiment, or Last Activity (those are campaign-level, not per-day).

### SQL Pattern

Same CTEs as the overview, but grouped by `(campaign_id, date(datetime(created_at, '{offset} hours')))` instead of just `campaign_id`. The `daily_days` CTE unions all event-type days to ensure no gaps are missed:

```sql
daily_days AS (
  SELECT campaign_id, day FROM daily_sends
  UNION SELECT campaign_id, day FROM daily_bounces
  UNION SELECT campaign_id, day FROM daily_replies
  UNION SELECT campaign_id, day FROM daily_ooo
  UNION SELECT campaign_id, day FROM daily_li_connects
  UNION SELECT campaign_id, day FROM daily_li_accepts
  UNION SELECT campaign_id, day FROM daily_li_messages
  UNION SELECT campaign_id, day FROM daily_li_replies
)
```

This ensures a campaign appears for a given day even if it only had LinkedIn activity with zero email sends.

---

## Implementation Details for Backend Dev

### Step 1: Query the data

Make 4 database queries:

1. **Campaign Overview** -- one wide query that joins all event-type CTEs (as shown in Sheet 1 SQL above)
2. **Daily Breakdown** -- same CTEs but grouped by `(campaign_id, day)`
3. **Sentiment Distribution** -- the `ranked_status` CTE query grouped by campaign and sentiment
4. **Per-campaign funnel data** -- query all active campaign funnels in one pass, or iterate campaigns (small workspaces: iterate; large: batch query)

### Step 2: Compute derived fields

```python
def build_overview_row(campaign_data, sentiment_counts):
    sent = campaign_data["sent"]
    bounced = campaign_data["bounced"]
    total_replies = campaign_data["total_replies"]
    ooo = campaign_data["ooo"]
    human = total_replies - ooo
    delivered = sent - bounced
    li_connects = campaign_data["li_connects"]
    li_accepts = campaign_data["li_accepts"]

    return {
        "campaign": strip_prefix(campaign_data["name"]),
        "status": detect_status(sent, campaign_data["all_time_sent"]),
        "sent": sent or "\u2014",
        "delivered": delivered,
        "bounced": bounced or "\u2014",
        "bounce_pct": pct(bounced, sent),
        "total_replies": total_replies,
        "ooo": ooo,
        "human": human,
        "reply_pct": pct(total_replies, delivered),
        "li_connects": li_connects or "\u2014",
        "li_accepts": li_accepts or "\u2014",
        "accept_pct": pct(li_accepts, li_connects),
        "li_messages": campaign_data["li_messages"] or "\u2014",
        "li_replies": campaign_data["li_replies"] or "\u2014",
        "interested": sentiment_counts.get("interested", 0) or "\u2014",
        "not_interested": sentiment_counts.get("not_interested", 0) or "\u2014",
        "sentiment_rate": pct(interested, interested + not_interested),
        "last_activity": format_date(campaign_data["last_activity"]),
    }
```

### Step 3: Format for Google Sheets

Each sheet is an array of rows. The first row (or second, after the metadata note) is the header.

**Campaign Overview Sheet** -- flat array of `build_overview_row()` dicts.

**Campaign Funnels Sheet** -- sectioned layout. Insert a header row for each campaign section:

```python
funnel_rows = []
for campaign in active_campaigns:
    funnel_rows.append([f"{campaign.name} -- Funnel", "", ""])  # section header
    funnel_rows.append(["Stage", "Volume", "% of Sent"])
    funnel_rows.append(["Emails Sent (unique)", campaign.unique_sent, "100%"])
    funnel_rows.append(["Emails Delivered", campaign.delivered, pct(campaign.delivered, campaign.unique_sent)])
    # ... more rows ...
    funnel_rows.append([])  # spacer
```

**Lead Sentiment Sheet** -- pivot table. Use `collections.defaultdict` to build the matrix:

```python
sentiment_order = ["positive", "negative", "autoreply", "invalid"]
pivot = defaultdict(lambda: defaultdict(int))

for row in sentiment_rows:
    pivot[row["campaign"]][row["sentiment"]] = row["lead_count"]

headers = ["Campaign"] + [s.capitalize() for s in sentiment_order] + ["Total Tagged", "Positivity Rate"]
```

**Daily Breakdown Sheet** -- flat array, one row per (campaign, day). Same row-building logic as overview but without Status/Sentiment/LastActivity.

### Step 4: Create the Google Sheet

Use the existing `sheets export` infrastructure -- the sheet is created on `app.outreachmagic.io` (not direct Google API calls). Send the structured data as JSON in the export payload:

```json
{
  "template": "campaign-stats",
  "workspace": "popcam",
  "title": "Popcam Campaign Stats - 14d",
  "sheets": [
    {
      "title": "Campaign Overview",
      "metadata": "Settings: workspace=popcam, since=14d, generated=2026-06-22T12:00:00Z, tz_offset=-4",
      "headers": ["Campaign", "Status", "Sent", ...],
      "rows": [["nace", "active", 607, ...], ...]
    },
    {
      "title": "Campaign Funnels",
      "metadata": "Settings: workspace=popcam, since=14d, generated=2026-06-22T12:00:00Z, tz_offset=-4",
      "headers": ["Stage", "Volume", "% of Sent"],
      "rows": [["nace -- Funnel", "", ""], ["Emails Sent", 607, "100%"], ...]
    },
    {
      "title": "Lead Sentiment",
      "metadata": "Settings: workspace=popcam, since=14d, generated=2026-06-22T12:00:00Z, tz_offset=-4",
      "headers": ["Campaign", "Positive", "Interested", ...],
      "rows": [["nace", 1, 2, ...], ...]
    },
    {
      "title": "Daily Breakdown",
      "metadata": "Settings: workspace=popcam, since=14d, generated=2026-06-22T12:00:00Z, tz_offset=-4",
      "headers": ["Campaign", "Day", "Sent", ...],
      "rows": [["nace", "2026-06-22", 42, ...], ...]
    }
  ]
}
```

### Step 5: Sheet-level formatting

If the sheet renderer supports it:

1. **Freeze header row** on all sheets (freeze 2 rows when metadata is present: metadata note + header)
2. **Alternating row colors** on the Overview sheet for scanability
3. **Bold section headers** on the Funnels sheet
4. **Percentage format** on all `%` columns (e.g. `0.0%`)
5. **Strikethrough** paused/exhausted campaign names (or color-code the Status column: green=active, yellow=paused, gray=exhausted)

---

## Caveats & Edge Cases

### No events in window

If a campaign has zero events in the time window, still include it in the Campaign Overview with `sent = 0` and status = `"paused"` or `"exhausted"`. Do not omit it -- the user needs to know campaigns exist that aren't producing activity. The Daily Breakdown only includes campaigns with at least one event in the window.

### Campaigns with no email sends

Some campaigns may have LinkedIn activity but no email sends. Include them in the Overview with email columns empty (`\u2014`) but LinkedIn columns populated. In the Daily Breakdown they'll appear on days they had LinkedIn activity.

### Very large workspaces

If a workspace has 50+ campaigns, consider:
- Paginating the Overview sheet (e.g. 100 rows max)
- Only generating funnels for the top 10 active campaigns by send volume
- Adding a note at the bottom: "*Showing top N active campaigns by volume*"

### Sentiment with no replies

Sentiment tags can be applied manually by an SDR even when no reply event exists. The `LATEST_STATUS_CTE` handles this correctly since it looks for any event with `lead_status_sentiment` in metadata, not just reply events.

### Empty sentiment sheet

If no leads have been sentiment-tagged in the window, still create the sheet with just the header row and a single row: `["No sentiment data in this period", "", "", "", "", "", ""]`.

### OOO detection accuracy

OOO detection depends on the sequencer flagging `is_auto_reply` in the event's `metadata_json`. Not all sequencers do this reliably. If OOO counts seem low, the issue is on the sequencer side, not the export. Add a footer note: *"OOO detection depends on sequencer auto-reply flags."*

---

## Appendix A: Reply Event Condition

From `platform_registry.py`, the canonical reply event filter:

```python
def reply_event_sql_condition() -> str:
    """SQL WHERE condition matching reply events across all platforms."""
    return """
    (
      LOWER(e.event_type) IN ('email_reply', 'linkedin_reply')
      OR (LOWER(e.direction) = 'inbound' AND LOWER(e.event_type) = 'email')
    )
    """
```

Use this exact condition in all reply-count queries.

## Appendix B: Campaign Name Prefix Stripping

```python
def strip_workspace_prefix(campaign_name: str, workspace: str) -> str:
    """Strip '{workspace} |' prefix from campaign names for display."""
    prefix = f"{workspace} |"
    if campaign_name.lower().startswith(prefix.lower()):
        return campaign_name[len(prefix):]
    return campaign_name
```

## Appendix C: Example Output

### Campaign Overview (14-day window)

| Campaign | Status | Sent | Delivered | Bounced | Bounce % | Total Replies | OOO | Human | Reply % | LI Connects | LI Accepts | Accept % | Interested | Not Int. | Sentiment Rate | Last Activity |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nace | active | 607 | 592 | 15 | 2.5% | 34 | 3 | 31 | 5.7% | 415 | 182 | 43.9% | 3 | 2 | 60% | Jun 13 |
| headshot lounge | active | 256 | 254 | 2 | 0.8% | 11 | 2 | 9 | 4.3% | -- | -- | -- | 0 | 3 | 0% | Jun 11 |
| nonprofit event outreach | active | 400 | 390 | 10 | 2.5% | 11 | 2 | 9 | 2.8% | -- | -- | -- | 3 | 2 | 60% | Jun 12 |
| free headshots | active | -- | -- | -- | -- | 6 | 0 | 6 | -- | -- | -- | -- | 3 | 0 | 100% | Jun 11 |
| people operations | active | 298 | 295 | 3 | 1.0% | 4 | 1 | 3 | 1.3% | -- | -- | -- | 0 | 1 | 0% | Jun 12 |
| career services | paused | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| field marketing | paused | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| conference sponsorships | exhausted | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| new student orientation | exhausted | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |

### NACE Funnel

| Stage | Volume | % |
|---|---|---|
| Unique Leads Contacted | 599 | |
| Emails Sent | 607 | |
| Delivered | 592 | 97.5% |
| Bounced | 15 | 2.5% |
| Total Replies | 34 | 5.7% |
| OOO Auto-Replies | 3 | 0.5% |
| Human Replies | 31 | 5.2% |
| LinkedIn Connects | 415 | |
| LinkedIn Accepts | 182 | 43.9% |
| LinkedIn Messages | 206 | |
| LinkedIn Replies | 2 | 1.0% |
| Interested Leads | 3 | 0.5% |
| Not Interested | 2 | 0.3% |

### Lead Sentiment

| Campaign | Positive | Interested | Neutral | Negative | Not Interested | Invalid | Total | Positivity Rate |
|---|---|---|---|---|---|---|---|---|
| nace | 1 | 2 | 0 | 1 | 1 | 0 | 5 | 60.0% |
| nonprofit event outreach | 3 | 0 | 0 | 1 | 1 | 0 | 5 | 60.0% |
| free headshots | 2 | 1 | 0 | 0 | 0 | 0 | 3 | 100.0% |
| headshot lounge | 0 | 0 | 0 | 3 | 0 | 0 | 3 | 0.0% |
| people operations | 0 | 0 | 0 | 1 | 0 | 0 | 1 | 0.0% |
