# Dark factory tests

Multi-layer skill testing for the Outreach Magic suite. See [docs/dark-factory-setup.md](../../docs/dark-factory-setup.md).

```bash
bash scripts/dark-factory/run.sh --layer 1              # local pytest (pull/relay/sync)
bash scripts/dark-factory/run.sh --layer 3 --tags smoke
bash scripts/dark-factory/run.sh --release email_finder
```

**Layers:** 1 = local pytest gate (no VPS); 2 = script tests on fixture DBs; 3 = Hermes agent tests.

- `catalog.json` — single catalog, filter by `--skills` / `--tags` / `--release`
- `harness-hermes/` — deployed to VPS as `test-harness` skill
- `results/` — gitignored JSON output
