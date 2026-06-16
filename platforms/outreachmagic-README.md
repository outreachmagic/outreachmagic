# Outreach Magic

Pipeline visibility for AI agents. Syncs events from your outreach tools into a local SQLite database your agent can query. Works with **Claude Code**, **Cursor**, and **Hermes**.

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

**After install, ask your agent things like:**
- "Show me leads who replied this week"
- "Which campaign has the highest reply rate?"
- "Pull the latest events and give me a client briefing"
- "Are there leads still in interested stage from yesterday?"

## What's included

| Skill | What it does | Standalone? |
|-------|-------------|-------------|
| **outreachmagic** | Pipeline DB, sequencer sync, agent queries | — |
| **lead-enrich** | Serper research, company/LinkedIn lookup | Yes |
| **email-finder** | trykitt + Icypeas email lookup | Yes |

Install all three, or grab companions alone.

## Install

Paste this into Claude Code, Cursor, or Hermes:

```
Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md
and follow its instructions exactly to install the Outreach Magic skill suite on this machine.
```

**Manual install:**

```bash
OM_VERSION=v1.38.3
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

Use `--platform cursor` or `--platform claude` for other agents.

After install: run `python3 <skill_home>/scripts/pipeline.py login` to connect your account.

Portal: [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding)

## API keys

Configure sequencer connections and provider keys in the portal after login. Keys sync locally via `pipeline.py sync-secrets`.

Supports: Smartlead, Instantly, HeyReach, PlusVibe, EmailBison, Prosp, MasterInbox, Calendly, and more.

Don't see your tool in the list? [Open a GitHub issue](https://github.com/outreachmagic/outreachmagic/issues).

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
