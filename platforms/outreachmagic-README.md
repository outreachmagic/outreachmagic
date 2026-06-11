# Outreach Magic

Cross-platform AI agent skill for outbound pipeline tracking. Works with **Hermes**, **Cursor**, and **Claude Code** from one repo.

## Install

**For AI agents (Claude Code, Cursor, Hermes):**

```
Fetch https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/AGENTS-INSTALL.md
and follow its instructions exactly to install the Outreach Magic skill suite on this machine.

Ask me which platform (Claude Code / Cursor / Hermes) and OS (Mac / Windows / Linux)
before starting. Stop and show me any errors.
```

**Manual install (full suite):** download → verify → run (see [AGENTS-INSTALL.md](./AGENTS-INSTALL.md)):

```bash
OM_VERSION=v1.34.0
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" -o /tmp/om_install.sh
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" -o /tmp/om_SHA256SUMS
(cd /tmp && grep ' install.sh$' om_SHA256SUMS | shasum -a 256 --check)
bash /tmp/om_install.sh --platform hermes --tag "${OM_VERSION}" \
  --with-lead-enrich --with-email-finder --migrate-hermes-profiles
```

Use `--platform cursor` or `--platform claude` for other agents. Outreach Magic only: omit `--with-lead-enrich` and `--with-email-finder`.

Full agent install guide: [AGENTS-INSTALL.md](./AGENTS-INSTALL.md) · Portal: [app.outreachmagic.io/onboarding](https://app.outreachmagic.io/onboarding)

Connect after install (run in terminal, not chat):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
# or ~/.cursor/skills/... / ~/.claude/skills/... depending on platform
```

## API keys

Configure providers and sequencer connections in the [Outreach Magic portal](https://app.outreachmagic.io) after `pipeline.py login`. Keys sync locally via `pipeline.py sync-secrets` — do not set shell env vars for interactive installs.

| Provider | Skill | Required? |
|----------|-------|-----------|
| Outreach Magic account (`pipeline.py login`) | outreachmagic | Yes |
| Serper | lead-enrich | If using lead-enrich |
| TryKitt / Icypeas | email-finder | One required for find |
| MillionVerifier | email-finder | Optional |

CI/automation: set `OUTREACHMAGIC_AGENT_KEY` in secrets (never commit).

## Layout

```
skills/outreachmagic/     # SKILL.md, scripts/, references/, update-manifest.json
install.sh                # --platform hermes|cursor|claude
AGENTS-INSTALL.md         # Agent-readable full install guide
platforms/overlays/       # Cursor .mdc, Claude snippet (optional)
```

## Update

```bash
python3 <skill>/scripts/pipeline.py update
```

Downloads from tagged releases on this repo (`skills/outreachmagic/` layout).

## Hermes profiles

Real files live under `~/.hermes/skills/outreachmagic/`. Profile dirs symlink only — never full copies. See `skills/outreachmagic/SKILL.md` → **Hermes profiles**.

## Companion skills

- [lead-enrich](https://github.com/outreachmagic/lead-enrich)
- [email-finder](https://github.com/outreachmagic/email-finder)

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
