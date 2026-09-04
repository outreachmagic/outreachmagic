# Email finding

Email finding and verification are part of the consolidated outreachmagic skill:

- CLI: `python3 scripts/email_finder.py find --name --domain ...`
- Waterfall orchestration: `python3 scripts/waterfall.py`
- Verification: via `pipeline.py verify-email`, `pipeline.py verify-status`, `pipeline.py verify-pending`
- Documentation: see [email-finding-research.md](email-finding-research.md)

## Bulk verification scope

`verify-bulk` submits the candidate set from
`pipeline.py verification-candidates`. Four flags widen that set; each one only
removes a filter, so they compose:

| Flag | Drops from the candidate filter |
|---|---|
| `--limit N` | The cap on how many candidates are submitted. **`--limit 0` = no cap.** Default 5000. |
| `--include-valid` | The age filters (`--max-age` / `--skip-mv-days`), so addresses already marked valid come back. |
| `--include-mv-attempted` | The "already sent to MillionVerifier" exclusion. |
| `--all` | Exactly `--include-valid` plus `--include-mv-attempted`. It does **not** change `--limit`. |

`--limit` is deliberately independent of `--all`: which addresses *qualify* and
how many you *submit* are different questions, and a bulk submit is billed per
address. Run `--dry-run` first — it prints credits required against credits
remaining, and says whether the candidate set was capped.

```bash
# everything, no ceiling -- price it before you spend it
email_finder.py verify-bulk --workspace acme --all --limit 0 --dry-run
email_finder.py verify-bulk --workspace acme --all --limit 0 --poll
```

