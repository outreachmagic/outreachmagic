# Relay Sync Backlog — Investigation & Remediation Plan

**Date:** 2026-07-24  
**Status:** Phase 0 complete — revised assessment  
**Org:** `cmplyyu9k0002weok1pa3k4dy`  
**Local DB:** `~/.hermes/skills/outreachmagic/databases/outreachmagic.db`

---

## Executive summary (revised after Phase 0)

The relay push path works. D1 and local entity counts are **in parity**. The apparent "~50% gap" was a **metrics illusion** caused by `sync_shadow` double-counting legacy natural keys and `uid:` keys for the same entities.

| Signal | Before audit | After Phase 0 |
|--------|--------------|---------------|
| D1 vs local `leads` count | looked ~50% short | **178,282 local vs 179,283 D1** ✓ |
| Outbox upsert backlog | unknown | **0 rows** — drain complete |
| `sync_shadow` total | ~347k lead_core (misleading) | **802,301 rows** (347k lead_core alone, dual-key inflated) |
| Real problem | "pushes not landing" | **shadow hygiene + status UX** |

**Do not run `outbox --backfill` now.** Backfill would re-queue 431k entities unnecessarily; echo-drop would skip most, but it wastes hours of CPU.

**Next operational step:** Phase 2 shadow prune (after reading safety checks below).  
**Next engineering step:** Phase 4 prevention work — this is what stops recurrence.

---

## Scope of proposed changes

Nothing in this plan modifies **D1 data** or the **relay database schema**. The relay worker code gets one small, optional API addition. Almost everything else is the **skill** (agent) plus **one-time local SQLite cleanup**.

| Layer | Repo / store | Proposed changes | Required? |
|-------|--------------|------------------|-----------|
| **Relay worker** | `wbhk-worker` | Extend `/push` JSON response with `snapshot_stale` + `snapshot_failed` (P3-4 / 4.5). No handler, schema, or D1 migration changes. | Optional but recommended — improves observability when stale-write guard rejects a push |
| **Skill (agent)** | `outreachmagic-skill` | `sync-health` command, fix `sync --status`, `shadow --prune-legacy` CLI, migration auto-prune (P3-1–3, 4.1–4.3), dashboard sync tab (4.2), agent handling of new relay fields (4.5), outbox tombstone compaction (4.6), docs/checklists (4.4, 4.8) | Yes — this is where the bug class lived (shadow accounting + misleading status) |
| **Local SQLite** | `~/.hermes/.../outreachmagic.db` | **One-time** `DELETE` of legacy-key `sync_shadow` rows (Phase 2). Optional orphan uid cleanup (Phase 2.2). Optional tombstone compaction (4.6B). No changes to `leads`, `companies`, or other entity tables. | Phase 2 yes (operator); rest optional |
| **D1 (Cloudflare)** | `outreach-magic-relay` | **None.** No migrations, no manual deletes, no schema drift to fix. D1 is healthy and in parity with local entity counts. | — |

**Summary:** Fix the skill's metrics and shadow hygiene; optionally teach the relay to report stale/failed writes more clearly. Do not touch D1.

---

## Phase 0 results (2026-07-24)

Run on agent machine with `OUTREACHMAGIC_DATA_ROOT=~/.hermes`.

### 0.1 Outbox — ground truth

```json
{
  "pending": {
    "company:delete": 228,
    "lead_core:delete": 5858,
    "lead_workspace:delete": 5229,
    "sender_domain:delete": 2
  },
  "sync_shadow": 802301
}
```

- **Upsert backlog: 0** — nothing waiting to push.
- **Delete tombstones: 11,317** — local trigger artifacts; generic push never sends these (by design). Not a relay gap.
- `outbox --backfill --dry-run` would queue 431,030 entities — **do not run** unless triggers were broken and upserts reappear.

### 0.2 Local entity counts vs D1

| Local table | Local count | D1 snapshot count | Delta |
|-------------|-------------|-------------------|-------|
| `leads` | 178,282 | 179,283 (lead_core) | +1,001 D1 |
| `workspace_leads` | 171,629 | 172,709 (workspace) | +1,080 D1 |
| `companies` | 80,311 | 80,313 (company) | +2 D1 |
| `sender_accounts` | 754 | 754 | 0 |
| `sender_domains` | 54 | 56 | +2 D1 |

Local and D1 are aligned within ~0.6%. D1 slightly ahead (normal — may include rows not in local or timing skew).

### 0.3 Shadow key split — root cause confirmed

| entity_type | uid_keys | legacy_keys | total |
|-------------|----------|-------------|-------|
| lead_core | 180,156 | 166,853 | **347,009** |
| lead_workspace | 172,709 | 140,082 | **312,791** |
| company | 80,539 | 61,152 | **141,691** |
| sender_account | 0 | 754 | 754 |
| sender_domain | 0 | 56 | 56 |

- Leads/companies have **both** uid and legacy shadow rows → totals ≈ 2× entity count.
- Senders have **legacy keys only** (754/56) — no uid migration on shadow yet; still matches D1 counts.
- D1 is **100% uid-keyed** for leads/companies; all legacy shadows are stale metadata.

### 0.4 Recent push audit (`sync_audit`, last 7 days)

| action | pushes | errors |
|--------|--------|--------|
| lead_core_update | 49,609 | 0 |
| lead_workspace_update | 31,804 | 0 |
| company_update | 23,192 | 0 |
| sender_account_update | 253 | 0 |

Push path is healthy; no error pattern.

---

## Root cause analysis (updated)

### RC-1: Pre-outbox cursor (historical — mitigated)

The old `updated_at` cursor silently dropped ~40% of writes. The outbox + triggers fix this **going forward**. Historical entities were eventually pushed (evidenced by D1 parity).

### RC-2: Shadow double-counting (active — primary remaining issue)

Pull seeds shadow under relay's key at pull time (legacy natural keys). Push records shadow under `uid:` keys. No prune on uid migration → `sync_shadow` totals are unusable as a D1 parity metric.

### RC-3: Misleading status commands (active — causes recurrence of confusion)

`sync --status` uses the deprecated cursor for lead pending. Operators compare shadow totals to D1 and conclude "half my data didn't sync" when the outbox is actually empty.

### RC-4: No parity dashboard (active)

Nothing surfaces **local table count vs D1 count vs outbox upserts vs shadow rows** in one view. Each metric alone is misleading.

---

## Phase 1 — Backlog drain

**Status: COMPLETE.** Outbox upserts = 0. Local entity counts match D1.

No further drain runs needed unless new outbox upserts appear after local writes.

---

## Phase 2 — Shadow reconciliation (next operational step)

**Goal:** Reduce `sync_shadow` from 802k → ~430k (one row per entity key that relay actually holds).

### 2.1 Safe prune — legacy keys for uid-migrated types

D1 is uid-only for leads/companies. Legacy shadows are orphaned local metadata.

```sql
-- PREVIEW
SELECT entity_type, COUNT(*) FROM sync_shadow
WHERE entity_type IN ('lead_core', 'lead_workspace', 'company')
  AND entity_key NOT LIKE 'uid:%'
GROUP BY entity_type;

-- EXECUTE (after preview matches expectations)
DELETE FROM sync_shadow
WHERE entity_type IN ('lead_core', 'lead_workspace', 'company')
  AND entity_key NOT LIKE 'uid:%';
```

Expected result:

| entity_type | shadow rows after prune |
|-------------|---------------------------|
| lead_core | ~180,156 |
| lead_workspace | ~172,709 |
| company | ~80,539 |
| sender_* | unchanged (754 + 56) |
| **total** | **~434,214** |

### 2.2 Orphan uid shadows (optional follow-up)

**Confirmed (2026-07-24):** 1,874 `lead_core` uid shadows have no matching live lead (`uid_keys` 180,156 vs `COUNT(leads)` 178,282). After legacy prune, run:

```sql
DELETE FROM sync_shadow s
WHERE s.entity_type = 'lead_core'
  AND s.entity_key LIKE 'uid:%'
  AND NOT EXISTS (
    SELECT 1 FROM leads l WHERE 'uid:' || l.uid = s.entity_key
  );
```

(Same pattern for `lead_workspace` joined via `workspace_leads`, and `company` via `companies.uid`.)

### 2.3 Sender shadow uid migration (future)

Senders still use legacy keys in shadow (integer id / domain string) but D1 keys match. Low priority unless sender entity keys change.

### 2.4 Success criteria

- [ ] `sync_shadow` lead_core ≈ `COUNT(leads)` (± deleted-lead orphans)
- [ ] 0 legacy-key rows for lead_core / lead_workspace / company
- [ ] `pipeline.py outbox` shadow total ≈ sum of D1 snapshot counts

---

## Phase 3 — Code fixes (short-term)

Prioritized PRs. Each includes tests.

| ID | Fix | Prevents |
|----|-----|----------|
| **P3-1** | `get_sync_status()` uses `outbox.count_dirty()` instead of `updated_at` cursor | False "pending" / "synced" reports |
| **P3-2** | `pipeline.py shadow --prune-legacy [--dry-run]` CLI | Manual SQL for shadow cleanup |
| **P3-3** | Auto-prune legacy shadow on uid migration (`pipeline_migration.py`) | RC-2 at migration time |
| **P3-4** | Relay `/push` response exposes `snapshot_stale`, `snapshot_failed` | Silent queue clears on stale reject |
| **P3-5** | `set_last_sync()` gates on outbox upserts = 0, not error-free push | Stale cursor after partial sync |
| **P3-6** | Remove `_log_selector_divergence` + deprecated cursor paths | Code path confusion |

### P3-3 detail — migration-time shadow prune (critical for prevention)

When uid columns are backfilled, add to `pipeline_migration.py` after uid assignment:

```sql
-- For each entity_type that switched to uid: keys, drop non-uid shadows
-- that map to a live row (keep uid: shadow if exists, else migrate hash)
DELETE FROM sync_shadow
WHERE entity_type = 'lead_core'
  AND entity_key NOT LIKE 'uid:%';
-- repeat for lead_workspace, company
```

Or safer: rewrite legacy → uid by joining `leads.uid` / `lead_identities`, then delete unmappable orphans.

---

## Phase 4 — Prevent recurrence (engineering + ops)

This section is the **permanent fix**. Phases 2–3 clean up today; Phase 4 ensures the next operator never sees "50% missing" again.

### 4.1 Single source of truth for sync health

Add `pipeline.py sync-health [--json]` that reports **one screen**:

```
┌─────────────────────────────────────────────────────────┐
│ Sync health                                             │
├──────────────────┬──────────┬──────────┬───────────────┤
│ Entity           │ Local    │ D1       │ Outbox upsert │
├──────────────────┼──────────┼──────────┼───────────────┤
│ lead_core        │ 178,282  │ 179,283  │ 0             │
│ lead_workspace   │ 171,629  │ 172,709  │ 0             │
│ company          │  80,311  │  80,313  │ 0             │
│ sender_account   │     754  │     754  │ 0             │
│ sender_domain    │      54  │      56  │ 0             │
├──────────────────┼──────────┼──────────┼───────────────┤
│ sync_shadow rows │ 434,214  │ (n/a)    │               │
│ shadow legacy    │       0  │          │               │
│ Status           │ IN PARITY│          │               │
└──────────────────┴──────────┴──────────┴───────────────┘
```

**Rules encoded in the command:**

| Condition | Status |
|-----------|--------|
| outbox upserts > 0 | `BACKLOG` — run sync |
| local vs D1 delta > 2% | `DRIFT` — investigate |
| legacy shadow keys > 0 | `SHADOW_STALE` — run shadow prune |
| all clear | `IN_PARITY` |

D1 counts: cache from last pull or lightweight relay query (optional `--live` flag hitting count endpoint or wrangler).

### 4.2 Dashboard sync tab upgrade

Extend `dashboard_queries.sync_outbox()` / Sync tab to show:

- **Pushable upserts** (not delete tombstones) — already partially done
- **Local vs shadow vs D1** per entity type
- **Legacy shadow key count** with "prune" action button (calls P3-2 CLI)
- Warning banner when `sync_shadow / COUNT(leads) > 1.1`

Never show raw `sync_shadow` total without the legacy/uid split.

### 4.3 Outbox trigger coverage test (CI)

Existing `sync_contract` coverage test should assert:

- Every table in `SYNC_MAP` has a working trigger
- Every trigger fires on INSERT/UPDATE/DELETE
- Run in `make release-check`

Add regression test: child-table write (e.g. `lead_provider_observations` INSERT) → outbox row appears for parent lead.

### 4.4 Post-migration checklist (document in AGENTS.md + SKILL.md)

After any schema migration that touches entity keys or sync:

1. [ ] Run `pipeline.py sync-health`
2. [ ] If legacy shadow keys > 0 → `pipeline.py shadow --prune-legacy --dry-run` then execute
3. [ ] Run `pipeline.py outbox --json` — confirm upsert counts expected
4. [ ] Spot-check 5 entities with `sync-diff`
5. [ ] Compare D1 counts (wrangler) to local table counts

### 4.5 Relay response contract (agent ↔ worker)

Extend `/push` response and agent logging:

```json
{
  "pushed": 200,
  "snapshot_upserts": 150,
  "snapshot_skipped_unchanged": 50,
  "snapshot_stale": 0,
  "snapshot_failed": 0,
  "truncated": false
}
```

Agent rules:

- If `snapshot_failed > 0` → do **not** settle those entries; retry
- If `snapshot_stale > 0` → log warning; optionally run `sync-diff` on sample
- If `truncated` → auto-retry remainder in same session

### 4.6 Delete tombstone UX

11k `op='delete'` outbox rows look like pending work but are never pushed. Options:

- **A)** Exclude from all "pending" counts (dashboard already splits `pushable_total`)
- **B)** Periodic `outbox` compaction: delete delete-tombstones older than N days where parent entity still exists
- **C)** Stop writing delete tombstones for non-owner-table deletes (trigger change)

Recommend **A + B** — don't change trigger semantics yet.

### 4.7 Monitoring alerts (manual until automated)

| Alert | Threshold | Action |
|-------|-----------|--------|
| Outbox upserts | > 1,000 for 24h after sync | Re-run sync; check sync_audit errors |
| Local vs D1 drift | > 2% on lead_core | sync-health; check outbox |
| Legacy shadow keys | > 0 after migration | Run shadow prune |
| Push error rate | > 5% in sync_audit (7d) | Check relay logs / billing buffer |

### 4.8 Deprecate misleading commands/paths

| Deprecated | Replacement |
|------------|-------------|
| `sync --status` lead pending (cursor-based) | `sync-health` or `outbox --json` |
| Raw `sync_shadow` count as parity metric | `sync-health` local vs D1 |
| `batch_sync_to_relay.py` cursor selection | outbox drain only |
| Comparing shadow total to D1 | Compare `COUNT(leads)` to D1 |

---

## Decision tree (updated)

```
pipeline.py sync-health
│
├─ outbox upserts > 0?
│   └─ YES → run sync (Phase 1 drain)
│
├─ local vs D1 delta > 2%?
│   └─ YES → sync-diff sample; check outbox triggers; do NOT backfill blindly
│
├─ legacy shadow keys > 0?
│   └─ YES → shadow --prune-legacy (Phase 2)
│
└─ all clear → IN PARITY (monitor with sync-health after migrations)
```

---

## Recommended execution order

| Step | Action | Owner | Status |
|------|--------|-------|--------|
| 1 | Phase 0 diagnostics | Agent | **Done** |
| 2 | Phase 2 shadow prune (legacy keys) | Operator | **Next** |
| 3 | P3-1 + P3-2 (status fix + shadow CLI) | Engineering | Sprint 1 |
| 4 | P3-3 (migration auto-prune) | Engineering | Sprint 1 |
| 5 | Phase 4.1 `sync-health` command | Engineering | Sprint 1 |
| 6 | Dashboard sync tab (4.2) | Engineering | Sprint 2 |
| 7 | Relay response + agent handling (4.5) | Engineering | Sprint 2 |
| 8 | Delete tombstone compaction (4.6B) | Engineering | Sprint 2 |

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Running backfill with empty upsert outbox | **Don't.** Phase 0 shows 0 upserts; backfill would waste 431k payload builds |
| Prune deletes shadow for entity only on legacy D1 key | D1 is 100% uid; legacy shadows are orphaned locally |
| uid shadow > leads count (deleted leads) | **1,874 confirmed** orphan lead_core uid shadows; Phase 2.2 cleanup after legacy prune |
| Future entity-key migration without shadow prune | P3-3 auto-prune in migration + post-migration checklist (4.4) |
| Operator trust in shadow total | sync-health replaces raw shadow count (4.1) |

---

## Appendix A — Relay infrastructure audit (Task A + Task B)

Read-only audit performed 2026-07-24 before Phase 0 local diagnostics. Confirms the push path works; the gap was misread metrics, not failed writes.

### A.1 Task A — `/push` handler code review (`wbhk-worker`)

**Authentication → `organization_id`**

- Route: `worker.js` lines 854–1010 (`POST /push`).
- Bearer must be `Authorization: Bearer om_agent_...` (`parseBearerAgent`).
- `resolveAgentKey(env, bearer)` → SHA-256 → Neon `AgentKey` lookup → `auth.organizationId`.
- Failures: 401 (bad key), 503 (Neon down), 429 (billing buffer cap). No silent auth bypass.

**Update actions → UPSERT**

All five agent update actions handled via `SNAPSHOT_ACTIONS` in `relay-db.js`, routed through `planSnapshotChunkWrites` → `buildSnapshotUpsertStatement`:

| Action | D1 table | Conflict key |
|--------|----------|--------------|
| `lead_core_update` | `relay_lead_core_snapshots` | `(organization_id, entity_key)` |
| `lead_workspace_update` | `relay_lead_workspace_snapshots` | `(organization_id, entity_key, workspace_slug)` |
| `company_update` | `relay_company_snapshots` | `(organization_id, entity_key)` |
| `sender_account_update` | `relay_sender_account_snapshots` | `(organization_id, entity_key)` |
| `sender_domain_update` | `relay_sender_domain_snapshots` | `(organization_id, entity_key)` |

Stored per row: `event_json`, `content_hash`, `updated_at`, `pushed_at`, `seq`, `source_updated_at_ms`, `billing_state`, `push_batch_id`, `legacy_entity_key`, plus `token` / `client_id`.

**Content-hash dedup**

- `payloadContentHash(JSON.stringify(payload.data))` compared to prefetched existing hash.
- Match → skip write, count as `snapshot_skipped_unchanged`.
- Note: `canonicalJson` documented as future migration (would invalidate existing hashes).

**Delete actions**

- `lead_core_delete`, `lead_workspace_delete`, `company_core_delete` → `buildSnapshotDeleteStatement`.
- No `sender_account_delete` / `sender_domain_delete` — expected; agent does not push these.

**Silent 200 without D1 write (footguns)**

| Path | HTTP | In response? |
|------|------|--------------|
| Hash dedup skip | 200 | `snapshot_skipped_unchanged` |
| Stale-write guard (`source_updated_at_ms`) | 200, `changes=0` | **No** — log-only `snapshot_stale`; still counted in `pushed` |
| Validation fail (missing key, etc.) | 200 | **No** — log-only `snapshot_failed`; not in `pushed` |
| Truncation (>5000 entries) | 200 | `truncated: true` |
| Billing buffer cap | 429 | error body |

**Schema vs handler**

Live D1 schema (wrangler, 2026-07-24) matches all columns the handler writes, including migration 0014 (`seq`, `source_updated_at_ms`, `legacy_entity_key`) and 0008 billing columns. No drift.

### A.2 Task B — Live D1 verification

**Org:** `cmplyyu9k0002weok1pa3k4dy` (sole org in relay DB).

**Row counts at audit time (before Phase 0 reframed the narrative)**

| Table | D1 rows | Local shadow (misleading) |
|-------|---------|---------------------------|
| `relay_lead_core_snapshots` | 179,283 | 347,009 |
| `relay_lead_workspace_snapshots` | 172,709 | 312,791 |
| `relay_company_snapshots` | 80,313 | 141,691 |
| `relay_sender_account_snapshots` | 754 | 754 |
| `relay_sender_domain_snapshots` | 56 | 56 |

Phase 0 later showed local **table** counts match D1; shadow totals were inflated.

**2026-07-24 push corroboration**

Rows with `pushed_at >= '2026-07-24'`:

| Type | D1 count | Expected from agent log |
|------|----------|-------------------------|
| lead_core | 1,040 | ~1,046 |
| lead_workspace | 688 | ~688 |
| company | 23 | ~14 |
| sender_account | 4 | ~1 |
| **Total** | ~1,755 | ~1,749 |

Most recent `pushed_at`: `2026-07-24T11:45:04.124Z`. Sample payloads well-formed (uid keys, populated `data`, aliases).

**Push history (lead_core by date)**

| Date | Rows pushed |
|------|-------------|
| 2026-07-14 | 140,011 |
| 2026-07-17 | 15,267 |
| 2026-07-19 | 6,992 |
| 2026-07-22 | 10,145 |
| 2026-07-24 | 1,040 |

**Integrity checks**

- Duplicate `(org, entity_key)` on lead_core: **0**
- Duplicate `(org, entity_key, workspace_slug)` on workspace: **0**
- Empty/null `event_json`: **0**
- Null/empty `content_hash`: **0**
- All rows `billing_state = 'delivered'`: **yes** (179,283/179,283)
- D1 entity keys: **100% `uid:`** for leads/companies (0 legacy natural keys)
- `relay_entity_aliases`: 368,028 rows

**Audit verdict (infrastructure)**

**Yes** — push → relay → D1 upsert path is working correctly. No relay schema fix or D1 data repair required.

---

## Appendix B — Key code references

| Topic | Location |
|-------|----------|
| `/push` route | `wbhk-worker/worker.js` |
| Outbox drain + echo drop | `outbox.py` |
| Push loop (uid keys) | `pipeline_workspace.py` `_push_pending_lead_snapshots` |
| Stale sync status cursor | `pipeline_workspace.py` `get_sync_status` (lines 538–566) |
| Accurate pending counts | `pipeline_workspace.py` `get_local_pending_counts` |
| Pull shadow seeding | `pipeline_sync.py` `_record_pull_shadow` |
| Uid migration (no shadow prune) | `pipeline_migration.py` lines 1590–1655 |
| Dashboard outbox view | `dashboard_queries.py` `sync_outbox` |
| Relay UPSERT + dedup | `wbhk-worker/relay-db.js` |

---

## Bottom line

**The data is synced.** Local tables and D1 match. The crisis was a **measurement problem**: dual-key shadow inflation plus a deprecated status cursor made it look like half the corpus never reached the relay.

**Today:** prune legacy shadows (Phase 2).  
**Tomorrow:** ship `sync-health`, fix `sync --status`, auto-prune on migration, and dashboard parity view (Phase 4) so this cannot confuse anyone again.
