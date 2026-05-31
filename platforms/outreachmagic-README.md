# Outreach Magic

Cross-platform AI agent skill for outbound pipeline tracking. Works with **Hermes**, **Cursor**, and **Claude Code** from one repo.

## Install

Get platform-specific commands at [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent).

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder --migrate \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
```

Use `--platform cursor` or `--platform claude` for other agents.

Connect after install:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
# or ~/.cursor/skills/... / ~/.claude/skills/... depending on platform
```

## Layout

```
skills/outreachmagic/     # SKILL.md, scripts/, references/, update-manifest.json
install.sh                # --platform hermes|cursor|claude
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
