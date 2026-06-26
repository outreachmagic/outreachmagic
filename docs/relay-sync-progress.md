# Relay sync progress (log format)

`pipeline.py pull` and `pipeline.py sync` print human-readable progress to stdout. When run via `export/batch_sync_to_relay.py`, each line is also prefixed with an ISO timestamp and phase label in `outreachmagic/logs/batch_sync.log`.

## Streams

| Label | Direction | Meaning |
|-------|-----------|---------|
| **Event** | ↓ pull / ↑ push | Outreach timeline (replies, sends, bounces, etc.) |
| **Lead** | ↓ pull / ↑ push | Org-level lead record (core snapshot) |
| **Workspace** | ↓ pull / ↑ push | Per-workspace lead overlay (`workspace_leads`: tags, status, sentiment) |
| **Company** | ↓ pull / ↑ push | Company entity updates |

**Workspace** here is lead state per workspace, not workspace routing/M2N config (that is a separate routing sync).

## Arrows

- **↓** — download from relay (`pull`)
- **↑** — upload to relay (`sync`)

## Pull (`pipeline.py pull`)

1. Optional preamble: `Pulling from relay (events: 1000/page, snapshots up to 5000/page)...`
2. Pending banner (first page of each stream, `~` on counts/pages only here):

   ```text
   [02:10] ↓ Event     : ~12,400 pending (~13p @ 1000/p) ...
   [02:10] ↓ Lead      : ~117,431 pending (~24p @ 5000/p) ...
   ```

3. One line per page (exact `pN/M`, no `ok`):

   ```text
   [02:11] ↓ Event     : p1/13 — 1,000 this page, 1,000/12,400 (8%) ...
   [02:29] ↓ Lead      : p24/24 — 2,431 this page, 117,431/117,431 (100%) ...
   [02:30] ↓ Workspace : p1/24 — 5,000 this page, 5,000/117,431 (4%) ...
   ```

Percent uses the relay’s pending total for that stream’s first page. It is capped at 100% if counts overlap across kinds.

## Push (`pipeline.py sync`)

1. Optional: `Syncing to relay (...)` summary
2. Pending banner (exact page count):

   ```text
   [03:55] ↑ Event     : 62,093 pending (13p @ 5000/p) ...
   ```

3. One merged line per HTTP page (after success):

   ```text
   [03:56] ↑ Event     : p2/13 — ok 7.9s, 5,000 this page (10,000/62,093 (16%))
   ```

4. Stream complete:

   ```text
   [03:57] ↑ Event     : done — 62,093 in 13 pages (110.6s)
   ```

On failure: `↑ Event : failed — …` or `failed (5,000 pushed before failure) — …` for partial uploads.

## `batch_sync.log` (orchestrator)

Long lead uploads use two levels of “batch”:

| Layer | Example | Meaning |
|-------|---------|---------|
| **batch N** in log wrapper | `batch 1: [04:00] ↑ Lead : p1/1 — …` | SQLite walk: mark 2,500 leads `cloud_pending`, run one `sync` |
| **pN/M** inside pipeline lines | `↑ Lead : p1/1` | HTTP pages inside that `sync` |

`PROGRESS [...]` lines report overall lead-walk percent (`leads 2,500/114,226`), separate from per-stream ↑ lines.

## Page sizes

- **Pull (events):** 1000 rows/page always (D1 memory). **Pull (snapshots):** 1000 routine; 5000 when pending ≥ 2500 on first page of that kind.
- **Push:** 200/request routine; 5000 when pending snapshots/events ≥ 2500.

See `SKILL.md` § “Relay sync limits” for env overrides.

## Grep tips

```bash
tail -f outreachmagic/logs/batch_sync.log | grep '↑ Event'
tail -f outreachmagic/logs/batch_sync.log | grep '↑ Lead'
python3 skills/outreachmagic/scripts/pipeline.py pull 2>&1 | grep '↓'
```
