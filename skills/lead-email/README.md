# lead-email

Find work emails with **trykitt.ai**. Checks **outreachmagic** before spending credits; saves via `import-profiles` + `verify-email`.

Part of the [Outreach Magic skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md).

## Install

Published repo: [outreachmagic/lead-email](https://github.com/outreachmagic/lead-email)

```bash
bash platforms/hermes/install.sh --with-lead-email
```

Or clone a release tag into `~/.hermes/skills/lead-email/`.

## Requirements

| Key | Purpose |
|-----|---------|
| `TRYKITT_API_KEY` | trykitt find API |
| outreachmagic + agent key | Dedup + save |

## Quick start

```bash
python3 scripts/lead_email.py config
python3 scripts/lead_email.py find --name "Jane Doe" --domain acme.com --save
```

## License

MIT — [Outreach Magic](https://outreachmagic.io)
