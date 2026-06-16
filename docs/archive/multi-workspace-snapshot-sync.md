# Multi-workspace snapshot sync (v2)

Relay stores lead snapshots in two D1 tables:

| Table | Action | Key |
|-------|--------|-----|
| `relay_lead_core_snapshots` | `lead_core_update` | `(organization_id, entity_key)` |
| `relay_lead_workspace_snapshots` | `lead_workspace_update` | `(organization_id, entity_key, workspace_slug)` |
| `relay_company_snapshots` | `company_update` | `(organization_id, entity_key)` |

Usage billing: **one unit per successful snapshot upsert** (hash-unchanged rows are skipped and not billed).

## Local pending flags

- `leads.cloud_pending` — org-wide profile (core)
- `workspace_leads.cloud_pending` — tags, status, activity, LinkedIn per workspace

## Cutover runbook (single org)

1. Deploy `wbhk-worker` migration `0005_relay_snapshot_v2.sql` and worker code.
2. Update outreachmagic skill on the machine with the canonical SQLite DB.
3. Run:

```bash
pipeline.py sync --full-snapshot-v2
```

4. Verify `sync --status` shows zero pending core/workspace snapshots.
5. Optional: fresh DB + `pull --full` on another machine to confirm round-trip.

Pull uses three snapshot cursors: `last_snapshot_core_after_id`, `last_snapshot_workspace_after_id`, `last_snapshot_company_after_id`.

Sync JSON uses `lead_snapshots_pushed` (core + workspace snapshot upserts). Legacy `lead_update` / `lead_create` / `relay_lead_snapshots` are removed.
