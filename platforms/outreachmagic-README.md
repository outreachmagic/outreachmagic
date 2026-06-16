# Outreach Magic — Stop Stitching CSVs

[![MIT License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-black)](https://docs.anthropic.com/en/docs/claude-code/skills)
[![Cursor](https://img.shields.io/badge/Cursor-ready-007ACC)](https://docs.cursor.com/skills)
[![Hermes](https://img.shields.io/badge/Hermes-ready-8B5CF6)](https://hermes-agent.nousresearch.com/docs/skills)

Stop stitching CSVs across Smartlead, Instantly, HeyReach, PlusVibe, EmailBison,
Prosp, MasterInbox, and Calendly. Sync them all into one local SQLite database
your AI agent can query. Works with **Claude Code**, **Cursor**, and **Hermes**.

## How it works

```
  Smartlead ──┐
  Instantly ──┤
  PlusVibe  ──┤  webhooks ──►  api.outreachmagic.io  ── pull ──►  local SQLite DB
  HeyReach  ──┤                                                      ▲
  EmailBison ─┤                                                      │
  Prosp     ──┤                                                your agent talks to it
  Calendly  ──┘
```

Every reply, click, bounce, and booked call lands in a database on your machine. Your agent reads it directly. No CSV exports, no merged sheets, no API pagination.

## Getting started

**1. Install the skills**

In Claude Code, Cursor, or Hermes, paste:

```
Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md
and follow its instructions exactly to install the Outreach Magic skill suite on this machine.
```

Or manually:

```bash
npx skills add outreachmagic/outreachmagic
```

**2. Connect your account**

```bash
python3 <skill_home>/scripts/pipeline.py login
```

This opens a browser to sign in. Come back when you're done.

**3. Ask your agent**

```
"Show me leads who replied this week"
"Which campaign has the highest reply rate?"
"Pull the latest events and give me a client briefing"
"Are there leads still in interested stage from yesterday?"
```

## What's included

| Skill | What it does | Works standalone? |
|-------|-------------|-------------------|
| **outreachmagic** | Pipeline DB, sequencer sync, agent queries | Requires relay account |
| **lead-enrich** | Serper research, company and LinkedIn lookup | Yes, with just a Serper key |
| **email-finder** | trykitt + Icypeas email lookup | Yes, with just API keys |

Install all three from this repo, or grab companions alone from their repos.

## API keys

Configure sequencer connections and provider keys in the [portal](https://app.outreachmagic.io) after login. Keys sync locally via `pipeline.py sync-secrets`.

**Supported platforms:** Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, Calendly, and more.

Don't see your tool? [Open a GitHub issue](https://github.com/outreachmagic/outreachmagic/issues).

## Manual install

```bash
OM_VERSION=v1.38.7
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

Use `--platform cursor` or `--platform claude` for other agents.

## Layout

```
skills/outreachmagic/     # SKILL.md, scripts/, references/, update-manifest.json
install.sh                # --platform hermes|cursor|claude (full suite)
AGENTS-INSTALL.md         # Agent-readable install guide
platforms/overlays/       # Cursor .mdc, Claude snippet
```

## Update

```bash
python3 <skill>/scripts/pipeline.py update
```

Downloads from tagged releases on this repo.

## License

MIT. [Outreach Magic](https://outreachmagic.io)
