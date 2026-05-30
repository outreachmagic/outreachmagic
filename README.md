# Outreach Magic — Development Monorepo

> **Private repo.** This is the development source for OutreachMagic skill scripts. Published platform repos are under the [outreachmagic](https://github.com/outreachmagic) org.

## Published Repos

| Platform | Repo | Install |
|----------|------|---------|
| Hermes | [outreachmagic/hermes-outreachmagic](https://github.com/outreachmagic/hermes-outreachmagic) | See `docs/install.md` (git clone) |
| Cursor | [outreachmagic/cursor-outreachmagic](https://github.com/outreachmagic/cursor-outreachmagic) | Copy to `~/.cursor/skills/outreachmagic/` |
| Claude Code | [outreachmagic/claude-code-outreachmagic](https://github.com/outreachmagic/claude-code-outreachmagic) | Copy scripts + append `CLAUDE_SNIPPET.md` to `CLAUDE.md` |

### Companion skills

| Skill | Repo | Purpose | Monorepo tag |
|-------|------|---------|--------------|
| lead-enrich | [outreachmagic/lead-enrich](https://github.com/outreachmagic/lead-enrich) | Serper person research; dedup before credits | `lead-enrich-vX.Y.Z` |
| lead-email | [outreachmagic/lead-email](https://github.com/outreachmagic/lead-email) | trykitt email find + save via OM | `lead-email-vX.Y.Z` |

Companion source lives in `skills/lead-enrich/` and `skills/lead-email/`. See [docs/skill-suite.md](docs/skill-suite.md) and [docs/RELEASING.md](docs/RELEASING.md).

## Repository Layout

```
skills/outreachmagic/          # Canonical skill source (scripts, SKILL.md, references)
platforms/
  hermes/                      # Hermes-specific SKILL.md + README
  cursor/                      # Cursor SKILL.md + .mdc rule + README
  claude-code/                 # CLAUDE_SNIPPET.md + README
.github/workflows/
  release.yml                  # Build tarball + GitHub Release on v* tag
  publish-platforms.yml         # Push to outreachmagic/* public repos on v* tag
  skill-scan.yml               # SkillScan + tests on PRs
scripts/                       # Dev scripts (build, sync, scan, verify)
tests/                         # Workspace routing tests
docs/                          # Install guide, skill suite, registry publish
```

## Release Flow

1. Bump `skills/outreachmagic/scripts/VERSION`
2. `python3 scripts/generate-update-manifest.py`
3. Commit and tag: `git tag vX.Y.Z && git push origin main --tags`
4. CI runs tests + SkillScan, builds tarball, publishes GitHub Release here
5. `publish-platforms.yml` assembles platform bundles, pushes to public repos, and creates GitHub Releases

> **Full details:** See [docs/RELEASING.md](docs/RELEASING.md) for the complete architecture, troubleshooting, and manual release instructions.

## Local Development

```bash
bash scripts/sync-local.sh     # Copy skill to ~/.hermes/skills/outreachmagic/
bash scripts/run-tests.sh      # Run workspace routing tests
bash scripts/skill-scan.sh     # Run HermesHub SkillScan
```

## Related Repos (magic-creators)

- `magic-creators/wbhk` — Cloudflare Worker relay (`api.outreachmagic.io`)
- `magic-creators/wbhkapp` — Next.js dashboard (`dev.outreachmagic.io`)
- `magic-creators/wbhk-billing` — Shared billing library

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
