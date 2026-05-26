# Bug: Incremental `pull` Misses Events Due to Clock-Based Sync Cursor

## Summary

`pipeline.py pull` (the default incremental pull) can silently miss relay events. Events that arrive on the relay around the same time as a pull are sometimes excluded by the `?since=` filter, causing them to be permanently invisible to incremental pulls. Only `pull --full` recovers them.

## Affected Code

**File:** `scripts/pipeline.py`

**Functions:**
- `sync_from_relay()` (line ~3595)
- `sync_from_relay_org()` (line ~3641)

**Root cause line (both functions):**

```python
set_last_pull(datetime.now(timezone.utc).isoformat())
```

## Root Cause

The sync cursor (`last_pull` in `outreachmagic_config.json`) is set to the **client's wall-clock time** (`datetime.now(UTC)`) after each pull. The next incremental pull sends this as `?since=<timestamp>` to the relay API, which only returns events with `received_at` after that value.

This creates a race condition:

```
Timeline:
  T1  12:15:00  Cron runs pull, relay returns 0 new events
  T2  12:15:01  set_last_pull("2026-05-26T12:15:01Z") saved to config
  T3  12:15:03  Webhook arrives at relay, stored with received_at = "2026-05-26T12:14:58Z"
               (clock skew, processing delay, or batch ingestion on the relay side)
  T4  12:30:00  Next cron pull sends ?since=2026-05-26T12:15:01Z
  T5            Relay filters out the event because 12:14:58 < 12:15:01
  T6            Event is permanently stuck — no future incremental pull will ever see it
```

### Why it happens

1. **Client clock vs relay clock** — the `last_pull` timestamp comes from the client machine. The relay's `received_at` comes from the relay server. Any clock drift between the two causes gaps.
2. **Relay ingestion delay** — even with perfectly synced clocks, a webhook can arrive at the relay *during* a pull but be timestamped slightly before the pull completes. The client writes `last_pull = now()` after processing, jumping the cursor past that event.
3. **No overlap/safety margin** — the cursor advances exactly to `now()` with zero overlap, so any event in the gap is lost.

### Why `--full` works

`pull --full` sets `page_since = None`, sending no `?since=` parameter. The relay returns the entire archive. Client-side deduplication (`relay_already_ingested()`) skips events already in SQLite and imports only the missed ones.

## Observed Behavior

- LaunchAgent cron running `pull` every 15 minutes consistently reports "No events on relay"
- Manual `pull` from the CLI also reports "No events on relay"
- `pull --full` finds and imports events that were missed (e.g., 4 events in our case)
- Cron logs show no errors — it silently believes there's nothing new

## Reproduction Steps

1. Set up the cron (LaunchAgent) running `pipeline.py pull` every 15 minutes
2. Have a connected platform (e.g., PlusVibe) sending webhooks to the relay
3. Wait for events to arrive on the relay around the same time as a cron pull
4. Observe that `pull` reports "No events on relay"
5. Run `pull --full` — observe that it finds events the incremental pull missed

## Recommended Fix: Persist `max_id` Instead of Wall-Clock Time

The relay API already supports cursor-based pagination via `after_id`. The `pull_events_org()` function already accepts an `after_id` parameter:

```python
def pull_events_org(agent_key, since=None, after_id=None, platform=None):
    params = []
    if since:
        params.append(f"since={urllib.parse.quote(since)}")
    if after_id:
        params.append(f"after_id={after_id}")
    ...
```

And the pagination loop already tracks `max_id` within a single pull:

```python
after_id = result.get("max_id") or 0
```

The fix is to **persist `max_id` across pulls** instead of using wall-clock time.

### Changes Required

#### 1. Add `last_max_id` to config helpers

```python
def get_last_max_id() -> Optional[int]:
    return load_config().get("last_max_id")

def set_last_max_id(max_id: int):
    cfg = load_config()
    cfg["last_max_id"] = max_id
    save_config(cfg)
```

#### 2. Update `sync_from_relay_org()` to use `after_id` instead of `since`

```python
def sync_from_relay_org(
    agent_key: str,
    since: Optional[str] = None,       # keep for backward compat
    after_id: Optional[int] = None,     # new: preferred cursor
    full: bool = False,
    debug_sentiment: bool = False,
    quiet: bool = False,
) -> tuple[int, int]:
    maybe_sync_routing_from_cloud(agent_key, quiet=quiet)
    imported = skipped = 0
    page_after_id = None if full else (after_id or 0)

    while True:
        result = pull_events_org(
            agent_key,
            since=None,  # stop using time-based filtering
            after_id=page_after_id if page_after_id else None,
        )
        if result.get("error"):
            raise RuntimeError(result.get("message", "pull failed"))

        events = result.get("events") or []
        if not events:
            break

        for event in events:
            if ingest_relay_event(event, ...) is None:
                skipped += 1
            else:
                imported += 1

        page_after_id = result.get("max_id") or page_after_id

        if len(events) < 1000:
            break

    if page_after_id:
        set_last_max_id(page_after_id)
    set_last_pull(datetime.now(timezone.utc).isoformat())  # keep for display/debug
    if not quiet:
        print_quarantine_guidance()
    return imported, skipped
```

#### 3. Same change for `sync_from_relay()` (legacy token path)

Apply the same pattern to the legacy `sync_from_relay()` function.

#### 4. Update the pull command handler to pass `after_id`

```python
# In the pull command handler (line ~4876):
if agent_key and not args.key:
    imported, skipped = sync_from_relay_org(
        agent_key,
        after_id=None if args.full else get_last_max_id(),
        full=args.full,
        debug_sentiment=args.debug_sentiment,
        quiet=args.cron,
    )
```

### Why This Fix Works

- `max_id` is a **monotonically increasing integer** assigned by the relay — immune to clock skew
- Events are never skipped because the cursor is a relay-side sequence number, not a client-side timestamp
- `after_id` is already supported by both the client and relay API — no server-side changes needed
- Backward compatible: if `last_max_id` is not in the config (existing installs), falls back to a full pull on the first run, then uses `max_id` going forward

### Relay API Requirement

Verify that the relay's `/pull` endpoint correctly filters by `after_id` (returns only events with `id > after_id`). This already appears to work based on the pagination logic, but should be confirmed for the primary filter case (no `since`, only `after_id`).

## Alternative Fixes (Less Preferred)

### Option B: Safety margin on wall-clock cursor

```python
set_last_pull((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
```

Pros: One-line change. Cons: Re-fetches 5 minutes of events every pull (wasteful), still vulnerable to large clock skew or relay delays > 5 minutes.

### Option C: Use relay's latest event timestamp

```python
if events:
    latest_ts = max(e.get("received_at", "") for e in events)
    set_last_pull(latest_ts)
```

Pros: Eliminates client/server clock skew. Cons: Still vulnerable to relay-side ingestion ordering issues where `received_at` is not strictly monotonic.

## Impact

- **Severity:** Medium — data is not lost (it stays on the relay archive), but users see stale pipelines and must manually run `--full` to recover
- **Frequency:** Depends on webhook arrival timing relative to pull schedule; more likely with frequent pulls (15-min cron) and bursty webhook traffic
- **Scope:** Affects all users relying on incremental `pull` (cron or manual)
