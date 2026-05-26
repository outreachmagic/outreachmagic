# Cloud Sync — Architecture Plan

Cross-platform sync for agent-originated data. Ensures leads, stage changes, notes, and other local mutations persist when users switch between Cursor, Hermes, Claude Code, or other agent platforms.

**Status:** Planning  
**Target:** Premium addon (not included in base marketplace skill)

---

## Why This Matters

Today the skill is local-first. Webhook events from sequencers (Smartlead, Instantly, Heyreach, PlusVibe, EmailBison) live on the relay and can always be re-pulled. But agent-originated data — manually added leads, stage updates, notes, tags, merges, imported profiles — only exists in the local SQLite database. If a user switches platforms or machines, that data is gone.

### Data at risk

| Operation | What's lost |
|-----------|------------|
| `add-lead` / `import-profiles` | Manually added leads and enrichment |
| `update-stage` | Pipeline stage changes, next-action notes |
| `log-event` | Manually tracked sends |
| `merge-leads` | Identity resolution decisions |
| Field edits | Notes, tags, title, industry, headcount |

---

## Why It's Not in the Base Skill

### Marketplace security requirements

Agent skill marketplaces (HermesHub, Cursor registry, etc.) expect skills to be **local-first and pull-only from remote**. A skill that uploads user data to a third-party server is a fundamentally different trust level:

- **Data exfiltration risk.** A skill with filesystem access AND outbound POST capability can theoretically send anything to an external server. Reviewers flag this regardless of intent.
- **PII concerns.** Push payloads contain personal data (names, emails, companies, message bodies). This triggers GDPR/CCPA obligations and review scrutiny.
- **Attack surface.** A push endpoint means a compromised agent key could be used to inject malicious data that other clients replay into their local DB.

### How we keep the base skill clean

- `pipeline.py` contains zero push/upload code. It can only read from the relay.
- No outbound HTTP POST with user data exists in the marketplace package.
- The skill passes SkillScan and marketplace review with a simple, auditable network surface: `GET /pull` (fetch events) and `GET /api/routing-config` (fetch workspace config).

---

## How Cloud Sync Will Work

### Core idea

The relay already stores raw webhook payloads and the client reprocesses them on `pull --full`. Cloud Sync extends this pattern: agent-originated mutations get pushed to the relay as raw JSON blobs, stored alongside webhook events, and pulled by other clients.

The server stays dumb (append-only raw log). The client stays smart (all processing logic lives in `pipeline.py`). Schema migrations become "ship new processing code + `pull --full`" — no server-side migration logic needed.

### Architecture

```
Sequencers (Smartlead, PlusVibe, etc.)
        |
        | webhooks
        v
    ┌─────────────────────────────────┐
    │   Relay (Cloudflare Worker)     │
    │                                 │
    │   Webhook events (raw payloads) │
    │   Agent changes  (raw payloads) │  <── push from clients
    │                                 │
    └─────────┬───────────────────────┘
              │ pull (paginated)
              v
    ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
    │  Cursor client   │  │  Hermes client   │  │  Claude Code     │
    │  (pipeline.py    │  │  (pipeline.py    │  │  (pipeline.py    │
    │   + sync.py)     │  │   + sync.py)     │  │   + sync.py)     │
    │                  │  │                  │  │                  │
    │  local SQLite    │  │  local SQLite    │  │  local SQLite    │
    └──────────────────┘  └──────────────────┘  └──────────────────┘
```

### What gets pushed

Every local write path emits a raw JSON entry. Six mutation types:

- `lead_create` — from `add-lead`, `import-profiles`
- `lead_update` — from `import-profiles --overwrite`, field edits
- `stage_change` — from `update-stage`
- `event_log` — from `log-event` (manually tracked sends)
- `lead_merge` — from `merge-leads`
- `company_create` — implicit when a lead creates a new company

Example raw entry:

```json
{
  "action": "lead_create",
  "client_id": "a1b2c3d4-...",
  "timestamp": "2026-05-25T12:00:00Z",
  "payload": {
    "email": "j@acme.com",
    "name": "Jane Doe",
    "company": "Acme Corp",
    "stage": "prospecting"
  }
}
```

The relay stores the blob as-is and assigns a monotonic server ID. No parsing or validation of the payload server-side.

### Sync flow

1. **Push** (before every pull, automatic when sync is enabled): send un-pushed local changes to the relay.
2. **Pull** (existing flow, extended): fetch webhook events AND agent changes from other clients. Process both through their respective ingest functions.
3. **Replay**: apply remote agent changes to local SQLite using email/linkedin as entity keys. Dedup by `"agent:{client_id}:{original_id}"`.

### Schema migrations

This is the key advantage of the raw log approach. When a breaking schema change ships:

1. Release new `pipeline.py` with updated processing logic.
2. User runs `pipeline.py update`.
3. User runs `pull --full` — wipes local DB, re-fetches all raw entries, reprocesses with new code.

No migration transforms, no version stamps, no server-side compaction. The raw payloads never change. Only the client-side processing logic changes.

Cost: `pull --full` at 500K entries takes roughly 1-2 minutes (download + reprocess). This only happens on major schema changes or new machine setup.

### Conflict resolution

Deliberately simple:

- **Lead fields:** last-writer-wins by timestamp.
- **Events:** append-only, dedup by client ID + original ID. No conflicts possible.
- **Merges:** replayed in timestamp order, skipped if already applied.
- **Push-before-pull** ensures a client's changes are on the server before it sees changes from others.

---

## How Users Will Activate It

Cloud Sync is a premium addon, separate from the base Pro tier.

### Activation flow

1. User installs the skill from a marketplace (pull-only, no push code).
2. They use it, build up local data.
3. Skill detects un-synced local changes and mentions Cloud Sync is available (at most once per session, not a nag).
4. User visits `outreachmagic.io/sync`, upgrades, clicks "Enable Cloud Sync."
5. Dashboard generates a scoped **sync token** (separate from agent key).
6. User pastes the token, or the skill auto-detects sync entitlement on next pull.
7. Skill downloads `sync.py` — a separate module that adds push capability. This file is NOT part of the marketplace package.
8. From now on, `pull` automatically pushes local changes first.

### Server-side entitlement gate

- `POST /push` returns 403 if the org doesn't have sync enabled.
- `GET /pull/agent` returns 403 without sync entitlement.
- Regular `GET /pull` (webhook events) works as before — no sync needed.

Even if someone manually installs `sync.py`, they can't use it without a paid sync entitlement. The gate is server-side.

### Separation of code

```
~/.cursor/skills/outreachmagic/
  scripts/
    pipeline.py            # marketplace-reviewed, pull-only
    sync.py                # downloaded after opt-in, handles push
    relay_extractors.py
    workspace_routing.py
    routing_cloud.py
```

`pipeline.py` checks for `sync.py` on startup. If present and configured, it calls into it. If not, everything works exactly as today.

---

## Implementation Plan (High Level)

### Phase 0 — Export + manual push (ship first, marketplace-safe)

The immediate solution. The skill itself only gets a read-only `export-local` command — no outbound network calls, no push code, fully marketplace-safe. The actual sync to the server happens outside the skill via curl/cron that the user sets up themselves.

**What already works today:**
- `show --json` exports all leads as JSON.
- `import-profiles --file` imports CSV/JSON with dedup by email/linkedin.
- The `relay_ingested` table already tracks which events came from the relay, so the signal for "local vs relay" data exists implicitly.

**What's needed in the skill (client-side, ~50 lines):**
- Add an `export-local` CLI command to `pipeline.py`.
- Exports only agent-originated data: leads not referenced in `relay_ingested`, locally-logged events, stage changes made after initial relay ingest.
- Supports `--json` (stdout) and `--file path.csv` output modes.
- JSON output format is compatible with both `import-profiles` and the relay's `/push` endpoint.
- Include current stage, notes, tags, and all lead fields so the receiving side gets the full current state.

**What's needed on the server (relay, separate from skill):**
- Add `POST /push` endpoint to the relay (Cloudflare Worker). Accepts the JSON from `export-local`, stores entries as raw blobs in D1 with monotonic IDs.
- Extend existing `GET /pull` to also return agent-originated entries, so other clients pick them up alongside webhook events.

**How it works — the skill stays clean:**

The skill only outputs JSON to stdout. The push is the user's own curl command or cron job — completely outside the marketplace package.

```bash
# One-time manual push:
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py export-local --json \
  | curl -s -X POST \
    -H "Authorization: Bearer om_agent_xxx" \
    -H "Content-Type: application/json" \
    -d @- \
    https://api.outreachmagic.io/push

# Automated via cron (every 15 minutes):
# crontab -e
*/15 * * * * python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py export-local --json | curl -s -X POST -H "Authorization: Bearer om_agent_xxx" -H "Content-Type: application/json" -d @- https://api.outreachmagic.io/push 2>/dev/null
```

On the receiving side, nothing changes — `pull` already fetches from the relay. Once agent-originated entries are in the relay's log, every client picks them up on their next `pull`.

**Why this passes security review:**
- `pipeline.py` contains zero outbound POST code. `export-local` is a pure read from local SQLite → JSON to stdout.
- The push is a user-initiated curl command, not skill code. Marketplace reviewers see no network writes.
- The user explicitly sets up the cron with their own API key. Full visibility and control.
- If they don't want sync, they just don't set up the curl. The skill works exactly as before.

**For users who just want file-based transfer (no server):**

```bash
# Machine A:
pipeline.py export-local --file local_changes.csv

# Transfer however they want (email, cloud drive, airdrop)

# Machine B:
pipeline.py import-profiles --file local_changes.csv --overwrite
```

**Scope and limitations:**
- Covers the main use case: locally added leads, stage changes, notes, tags, enrichment.
- Event history (logged sends, reply timelines) is included in the export but replayed as new events on import — timestamps are preserved but local IDs change.
- Cron-based sync is near-real-time (within the cron interval) but not instant.
- If two machines push conflicting changes, last-write-wins by timestamp on the relay.

**Implementation summary:**

| Component | Where | Change |
|-----------|-------|--------|
| `export-local` command | `pipeline.py` (skill) | ~50 lines, read-only, marketplace-safe |
| `POST /push` endpoint | `wbhk-worker` (relay) | Accepts JSON, stores raw blobs in D1 |
| Extend `GET /pull` | `wbhk-worker` (relay) | Include agent entries in paginated response |
| Cron setup docs | `SKILL.md` or docs/ | Show users how to set up curl + cron |

---

### Phase 1 — Client-side change tracking (prepare for Cloud Sync)

- Add `client_id` (UUID) to config, generated on first `init`.
- Add `local_changes` table to SQLite schema for structured change tracking.
- Instrument write paths in `pipeline.py` to emit raw entries to `local_changes`. This is safe for the marketplace — the table is local-only, no data leaves the machine.
- `export-local` switches from querying `relay_ingested` to reading from `local_changes` (cleaner, more reliable signal).

### Phase 2 — Relay endpoints (if not already built in Phase 0)

- Add `agent_changes` table to D1 (Cloudflare Worker relay) if not using the same events table.
- Harden `POST /push` with rate limiting, payload size limits, entitlement checks.
- Add `GET /pull/agent` endpoint — paginated by server ID, supports `exclude_client` param.

### Phase 3 — Sync module (premium addon)

- Build `sync.py` with automated push logic, replay/ingest for remote agent changes, and push-before-pull orchestration.
- Host for download behind entitlement check.
- Wire `pipeline.py` to load `sync.py` if present. Replaces the manual curl/cron with built-in sync.

### Phase 4 — Dashboard + billing

- Add Cloud Sync toggle and billing to `outreachmagic.io`.
- Generate scoped sync tokens.
- Sync entitlement enforcement on relay.

### Phase 5 — Nudge mechanics

- Detect un-synced local changes, show one-time per-session note about Cloud Sync availability.
- Detect empty DB on a new machine with an existing agent key — suggest Cloud Sync.

---

## Security Considerations

| Concern | Mitigation |
|---------|-----------|
| PII in push payloads | HTTPS in transit; future: client-side encryption so server can't read payloads |
| Compromised agent key | Sync uses a separate scoped token; push and pull tokens can be rotated independently |
| Replay poisoning (malicious entries injected) | Entries are scoped per-org; replay uses existing dedup + validation; future: signed entries |
| Marketplace review | Push code is not in the marketplace package; `sync.py` is a separate download |
| GDPR / data subject rights | Server stores raw blobs; deletion endpoint to purge an org's agent changes on request |

### Future: client-side encryption

Encrypt push payloads with a user-held key before sending. The relay stores opaque blobs it cannot read. Only clients with the decryption key can process them. This eliminates PII-at-rest concerns on the server entirely.

---

## Pricing (Tentative)

| Tier | Price | Includes |
|------|-------|----------|
| Free | $0/mo | Pull-only, 100 relay events/mo, local data only |
| Pro | $19/mo | Unlimited relay events, pull-only, local data only |
| Pro + Cloud Sync | $29/mo | Everything in Pro + cross-platform sync |
