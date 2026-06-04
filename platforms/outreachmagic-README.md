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

**Manual / one-liner (full suite):**

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes --with-lead-enrich --with-email-finder --migrate
```

Use `--platform cursor` or `--platform claude` for other agents. Outreach Magic only: omit `--with-lead-enrich` and `--with-email-finder`.

Full agent install guide: [AGENTS-INSTALL.md](./AGENTS-INSTALL.md) · Portal: [app.outreachmagic.io/setup/agent](https://app.outreachmagic.io/setup/agent)

Connect after install (run in terminal, not chat):

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
# or ~/.cursor/skills/... / ~/.claude/skills/... depending on platform
```

## API keys

| Key | Skill | Required? |
|-----|-------|-----------|
| Outreach Magic (via `pipeline.py login`) | outreachmagic | Yes |
| `SERPER_API_KEY` | lead-enrich | If using lead-enrich |
| `TRYKITT_API_KEY` / `ICYPEAS_API_KEY` | email-finder | One required for find |
| `MILLIONVERIFIER_API_KEY` | email-finder | Optional |

Details and signup links: [AGENTS-INSTALL.md](./AGENTS-INSTALL.md#third-party-api-keys-companions). CI may use `OUTREACHMAGIC_AGENT_KEY` instead of login.

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
