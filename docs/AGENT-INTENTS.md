# Agent intents — Outreach Magic CLI

Map common user requests to the correct `pipeline.py` commands. Agents should prefer these over improvised alternatives (gspread, browser automation, manual CSV for Sheets).

## Google Sheets export

```bash
SHARE_EMAIL=$(python3 <SKILLS>/outreachmagic/scripts/pipeline.py whoami --json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['share_email'])")

python3 <SKILLS>/outreachmagic/scripts/pipeline.py sheets export \
  --workspace <WORKSPACE> \
  --title "Lead Export — YYYY-MM-DD" \
  --share-email "$SHARE_EMAIL" \
  --detail full
```

| User says | Flags |
|-----------|-------|
| export to Google Sheets | `--workspace W` + `--share-email` from whoami |
| only leads with email | `--require-domain` |
| never contacted | `--never-contacted` |
| share with someone else | `--share-email their@email.com` |

**Not** `review export` unless doing dedup review (`--input candidates.json`).

## Import leads

```bash
pipeline.py import-profiles --file leads.csv --workspace W
# sync runs automatically; use --no-sync to skip
pipeline.py sync
```

## API keys

Portal only — `pipeline.py sync-secrets`. Never collect raw keys in chat.

## Login

Offer to run `pipeline.py login` after install; wait for browser sign-in.
