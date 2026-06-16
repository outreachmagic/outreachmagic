# Outreach Magic

Cross-platform AI agent skill suite. Install all three, or grab companions standalone:

| Skill | Standalone | With OM (better together) |
|-------|-----------|--------------------------|
| **outreachmagic** | — | Pipeline DB, sequencer sync, cross-platform state |
| **lead-enrich** | Serper searches → JSON output | Dedup before Serper, save to pipeline |
| **email-finder** | trykitt/Icypeas → stdout | Pre-flight dedup, save to pipeline |

Works with **Hermes**, **Cursor**, and **Claude Code** from this repo.

## Install

**For AI agents (Claude Code, Cursor, Hermes):**

```
Fetch https://github.com/outreachmagic/outreachmagic/blob/main/AGENTS-INSTALL.md
and follow its instructions exactly to install the Outreach Magic skill suite on this machine.

Ask me which platform (Claude Code / Cursor / Hermes) and OS (Mac / Windows / Linux)
before starting. Stop and show me any errors.
```

**Manual install:**

```bash
OM_VERSION=v1.38.0
INSTALL_DIR=$(mktemp -d)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)
bash "${INSTALL_DIR}/install.sh" --platform hermes --tag "${OM_VERSION}"
```

Use `--platform cursor` or `--platform claude` for other agents.

Portal: [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding)

Connect after install:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
# or ~/.cursor/skills/... / ~/.claude/skills/... depending on platform
```

## API keys

Configure providers and sequencer connections in the [Outreach Magic portal](https://app.outreachmagic.io) after `pipeline.py login`. Keys sync locally via `pipeline.py sync-secrets`. Do not set shell env vars for interactive installs.

Don't see your email finder, enrichment provider, or sequencer in our supported list? [Open a GitHub issue](https://github.com/outreachmagic/outreachmagic/issues) and we'll look at adding it.

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

## Hermes profiles

Real files live under `~/.hermes/skills/`. Profile dirs symlink only. See `docs/hermes-skills-layout.md`.

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT. [Outreach Magic](https://outreachmagic.io)
