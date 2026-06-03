# Maintainer ops scripts (not part of the skill install)

One-off recovery, legacy import fixes, and VPS helpers. **Not** included in `update-manifest.json` or end-user installs.

Run from repo root, e.g.:

```bash
python3 scripts/ops/vps_advance_snapshot_cursors.py --help
python3 scripts/ops/legacy_import_post_fix.py --help
```

| Script | Purpose |
|--------|---------|
| `legacy_import_post_fix.py` | Post-import attribution / Sales Nav identity fixes |
| `legacy_import_may2026.py` | Batch legacy CSV import helper |
| `reset_workspaces_and_rules.py` | Reset local workspace routing state |
| `reset_routing_rules_after_sync.py` | Reset routing after batch sync |
| `vps_advance_snapshot_cursors.py` | Advance relay snapshot cursors on VPS |
| `vps_repair_company_snapshots.py` | Repair company snapshot ingest |
| `repush_events_to_relay.py` | Re-push local events to relay |
| `relay_sync_clean_run.py` | Controlled sync run wrapper |

User-facing VPS smoke test (kept in `scripts/`): `vps_test_query_layer.sh`, `vps_pull_smoke_test.sh`.
