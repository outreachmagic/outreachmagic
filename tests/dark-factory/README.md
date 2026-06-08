# Dark factory tests

Multi-layer skill testing for the Outreach Magic suite. See [docs/dark-factory-setup.md](../../docs/dark-factory-setup.md).

```bash
bash scripts/dark-factory/run.sh --layer 3 --tags smoke
bash scripts/dark-factory/run.sh --release email_finder
```

- `catalog.json` — single catalog, filter by `--skills` / `--tags` / `--release`
- `harness-hermes/` — deployed to VPS as `test-harness` skill
- `results/` — gitignored JSON output
