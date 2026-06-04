# Email Finder

Find work emails with **trykitt.ai** and **Icypeas** (waterfall). Checks **outreachmagic** before spending credits; saves to your local pipeline.

Part of the [Outreach Magic skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md).

## Install

[outreachmagic/email-finder](https://github.com/outreachmagic/email-finder) — see [install-companions.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/install-companions.md).

## Keys

| Key | Required |
|-----|----------|
| `TRYKITT_API_KEY` | One of trykitt / Icypeas for find |
| `ICYPEAS_API_KEY` | |
| outreachmagic + agent key | Dedup + save |
| `MILLIONVERIFIER_API_KEY` | Optional (`verify*` commands) |

## Quick start

```bash
python3 scripts/email_finder.py batch-find --workspace YOUR_WS --yes \
  --output-base ./export/emails leads.json
```

Include `lead_id` in `leads.json` when enriching existing OM imports.

## License

MIT — [Outreach Magic](https://outreachmagic.io)
