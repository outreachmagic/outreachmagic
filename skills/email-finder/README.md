# Email Finder

Find work emails with **trykitt.ai** and **Icypeas** (waterfall). Checks **outreachmagic** before spending credits; saves to your local pipeline.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## Install

Install via the main repo agent guide:

https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md

Suite one-liner: [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) — `install.sh --platform <name> --with-email-finder` (implies lead-enrich).

## API keys

| Key | Required? |
|-----|-----------|
| `TRYKITT_API_KEY` | One of trykitt / Icypeas for find — [trykitt.ai](https://trykitt.ai) |
| `ICYPEAS_API_KEY` | |
| Outreach Magic (`pipeline.py login`) | Yes — dedup + save |
| `MILLIONVERIFIER_API_KEY` | Optional (`verify*` commands) |

Full key table: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md#third-party-api-keys-companions).

## Quick start

```bash
# Waterfall (default)
python3 scripts/email_finder.py batch-find --workspace YOUR_WS --yes \
  --output-base ./export/emails --workers 3 --delay 3 leads.json

# IcyPeas only (stricter rate limits)
python3 scripts/email_finder.py batch-find --provider icypeas --workspace YOUR_WS --yes \
  --workers 2 --delay 3 --output-base ./export/icypeas leads.json
```

Include `lead_id` in `leads.json` when enriching existing OM imports (see `config.example.json` for poll/rate-limit tuning).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
