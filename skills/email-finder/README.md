# Email Finder

Find work emails with **trykitt.ai**. Checks **outreachmagic** before spending credits; saves via `import-profiles` + `verify-email`.

Part of the [Outreach Magic skill suite](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/skill-suite.md).

## Install

Published repo: [outreachmagic/email-finder](https://github.com/outreachmagic/email-finder)

Install with outreachmagic on any platform — see [install-companions.md](https://github.com/magic-creators/outreachmagic-skill/blob/main/docs/install-companions.md).

Or clone a release tag into your platform skills dir (e.g. `~/.hermes/skills/email-finder/`).

## Requirements

| Key | Purpose |
|-----|---------|
| `TRYKITT_API_KEY` | trykitt find API |
| outreachmagic + agent key | Dedup + save |

## Quick start

```bash
python3 scripts/email_finder.py config
python3 scripts/email_finder.py find --name "Jane Doe" --domain acme.com --save --workspace your_workspace
```

## License

MIT — [Outreach Magic](https://outreachmagic.io)
