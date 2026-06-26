<p align="center">
  This is a read-only mirror. Stars, issues, and pull requests belong at
  <a href="https://github.com/outreachmagic/outreachmagic">outreachmagic/outreachmagic</a>.
</p>

# Email Waterfall Finder

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills) [![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills) [![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Find work emails when you have a name and company domain. Or verify emails you already have. Works standalone with just API keys, or pairs with Outreach Magic to save every result and never search for the same lead twice.

Part of the [Outreach Magic skill suite](https://github.com/outreachmagic/outreachmagic).

## How it fits

Two ways to use this skill. They are separate. You pick the one you need.

**Path 1: Find emails with the waterfall.** Give it a name and domain (one off or a whole list). Enable trykitt, Icypeas, or both. The waterfall hits the first enabled platform. No match? Falls back to the next one. If it finds an email, it saves to local agent storage. If Outreach Magic is connected, it saves there instead.

```
                      ┌────────────────────┐
name + domain ───────►│   email finder     │
(single or list)      └─────────┬──────────┘
                                │
                   ┌────────────▼────────────┐       hit
                   │  trykitt (if enabled)   ├────────────────┐
                   └────────────┬────────────┘                │
                                │ miss                        │
                   ┌────────────▼────────────┐       hit      │
                   │  Icypeas (if enabled)   ├──────────────┐ │
                   └────────────┬────────────┘              │ │
                                │ miss                      │ │
                   ┌────────────▼────────────┐              │ │
                   │  no email found         │              │ │
                   └─────────────────────────┘              │ │
                                                            ▼ ▼
                                                   ┌──────────────────┐
                                                   │   email found    │
                                                   └────────┬─────────┘
                                                            │
                                            ┌───────────────┴───────────────┐
                                            ▼                               ▼
                                  ┌──────────────────┐          ┌──────────────────┐
                                  │  agent replies   │          │  OM database     │
                                  │  (not saved)     │          │  (if connected)  │
                                  └──────────────────┘          └──────────────────┘
```

**Path 2: Verify emails you already have.** Got a list of emails you collected somewhere else? Send them through MillionVerifier. It checks deliverability. You do not need trykitt or Icypeas for this. This path does not connect back to the waterfall.

```
                ┌──────────────────┐
 email list ───►│  MillionVerifier │──► emails verified
                └──────────────────┘
```

| Mode | What happens | What you need |
|------|-------------|---------------|
| Standalone find | Hits trykitt (if enabled), falls back to Icypeas (if enabled), saves result locally | One or more API keys (trykitt, Icypeas, or both) |
| Standalone verify | Checks emails you already have for deliverability | MillionVerifier API key |
| With Outreach Magic | Same waterfall, but saves to your pipeline and skips leads you already searched | OM account + API keys |

## Quick start

Once it's installed, try prompts like these:

```
find the email for bill smith at acmecorp.com using trykitt only
```

```
find emails for everyone in leads.csv
```

```
verify these emails: bill@acme.com, jane@xyz.io
```

Not sure what it can do? Ask your agent:

```
tell me everything the email finder skill can do
```

## Install

You can install just the email finder skill on its own. Or install the full Outreach Magic suite, which gives you the email finder, the local database, and lead enrichment all at once.

**Install just the email finder:**
```bash
npx skills add outreachmagic/email-finder
```

**Install the full Outreach Magic suite (email finder + database + lead enrich):**
```bash
npx skills add outreachmagic/outreachmagic
```

Or follow the agent install guide: [AGENTS-INSTALL.md](https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md)

## What you need

| Key | For | Required? |
|-----|-----|-----------|
| `TRYKITT_API_KEY` | Find emails via trykitt.ai | One or the other, not both |
| `ICYPEAS_API_KEY` | Fallback find via Icypeas | One or the other, not both |
| `MILLIONVERIFIER_API_KEY` | Verify emails you already have | Only for the verify path |
| Outreach Magic login | Dedup and save results to pipeline | Only with OM |

Set your API keys in your agent's environment config. If you use Outreach Magic, you can set them in the portal instead and they get passed through automatically.

## License

MIT. [Outreach Magic](https://outreachmagic.io)
