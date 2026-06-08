# Dedup dark-factory fixtures

Generic test data (no campaign-specific tags). Checked-in SQLite snapshot for layer 2 script tests.

```bash
# Regenerate after changing build_fixture.py
python3 tests/dark-factory/fixtures/dedup/build_fixture.py
```

| File | Purpose |
|------|---------|
| `data-root/` | `OUTREACHMAGIC_DATA_ROOT` for tests (workspace `df-dedup`, tag `dedup-test`) |
| `candidates.json` | Orphan merge_id test (phantom lead 99999) |
| `candidates-commit.json` | Valid HIGH pair for `--commit` test |
| `meta.json` | Expected find stats (generated) |
