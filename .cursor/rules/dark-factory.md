# Dark factory — Outreach Magic skill testing

When the user asks to **test**, **dark factory**, **run smoke**, or **prepare release**, follow this workflow before tagging.

## Quick commands

```bash
# Layer 1 only — local pytest (pull/relay/sync, no VPS)
bash scripts/dark-factory/run.sh --layer 1

# Smoke (Layer 3 agent tests on VPS dark-factory)
bash scripts/dark-factory/run.sh --layer 3 --tags smoke

# Release-scoped (see docs/RELEASING.md)
bash scripts/dark-factory/run.sh --release v_star
bash scripts/dark-factory/run.sh --release lead_enrich
bash scripts/dark-factory/run.sh --release email_finder
bash scripts/dark-factory/run.sh --release companion_common
bash scripts/dark-factory/run.sh --release dedup

# Deploy monorepo skills to VPS without testing
bash scripts/dark-factory/run.sh --deploy-only
```

## Workflow

1. Ensure `test-config.local.json` exists (copy from `test-config.example.json`).
2. Ensure VPS instance `dark-factory` is provisioned (`hermes-vps/deploy/runbooks/dark-factory-provision.md`).
3. Run `bash scripts/dark-factory/run.sh` with appropriate `--release` or `--skills` / `--tags`.
4. Read `tests/dark-factory/results/*.json` and `report.py` output.
5. **All pass** → proceed with `docs/RELEASING.md` version bump + tag.
6. **Any fail** → fix skills/scripts, re-deploy (`--skip-deploy` only if rsync already done), re-run.

## Publish gate

Do **not** tag or push `v*`, `lead-enrich-v*`, or `email-finder-v*` until dark factory passes for the affected release filter.

If `platforms/overlays/cursor/outreachmagic.mdc` changed, remind user to run manual Cursor smoke (see `tests/dark-factory/harness-cursor/rules.md`).

## One-time VPS setup

See `hermes-vps/deploy/runbooks/dark-factory-provision.md` and `docs/dark-factory-setup.md`.
