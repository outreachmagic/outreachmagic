# Outreach Magic — Development Monorepo

> **Private repo.** Development source for Outreach Magic. The public install repo is [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic).

## Public Install Repo

| Platform | Install |
|----------|---------|
| All (Hermes, Cursor, Claude Code) | [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) — `install.sh --platform <name>` |

See [docs/install.md](docs/install.md) for curl one-liners.

### Companion skills

| Skill | Repo | Purpose | Monorepo tag |
|-------|------|---------|--------------|
| lead-enrich | [outreachmagic/lead-enrich](https://github.com/outreachmagic/lead-enrich) | Serper person research; dedup before credits | `lead-enrich-vX.Y.Z` |
| email-finder | [outreachmagic/email-finder](https://github.com/outreachmagic/email-finder) | trykitt email find + save via OM | `email-finder-vX.Y.Z` |

Companion source lives in `skills/lead-enrich/` and `skills/email-finder/`. See [docs/skill-suite.md](docs/skill-suite.md) and [docs/RELEASING.md](docs/RELEASING.md).

## Repository Layout

```
skills/outreachmagic/          # Canonical skill source (scripts, SKILL.md, references)
install.sh                     # Unified cross-platform installer
platforms/
  common/install-companions.sh
  overlays/                    # Cursor .mdc, Claude snippet
  hermes|cursor|claude-code/   # Thin install wrappers
.github/workflows/
  release.yml                  # Build tarball + GitHub Release on v* tag (private repo)
  publish-platforms.yml        # Push to outreachmagic/outreachmagic on v* tag
  skill-scan.yml               # SkillScan + tests on PRs
scripts/                       # Dev scripts (build, sync, scan, verify)
tests/
docs/
```

## Release Flow

1. Bump `skills/outreachmagic/scripts/VERSION`
2. `make release-check` (regenerates manifests from `skill-suite.json`)
3. Commit and tag: `git tag vX.Y.Z && git push origin main --tags`
4. CI runs tests + SkillScan, builds tarball on private repo
5. `publish-platforms.yml` pushes to [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) and creates a GitHub Release

> **Full details:** See [docs/RELEASING.md](docs/RELEASING.md) · [docs/README.md](docs/README.md) for all docs

## Local Development

```bash
bash install.sh --platform hermes --local --migrate   # Install from monorepo checkout
bash scripts/run-tests.sh
bash scripts/skill-scan.sh
```

## Related Repos (magic-creators)

Full ecosystem map: [docs/ecosystem.md](docs/ecosystem.md)

| Repo | Surface |
|------|---------|
| `outreach-magic-site` | Marketing site (`outreachmagic.io`) |
| `wbhk-app` | Agent portal (`app.outreachmagic.io`) |
| `wbhk-worker` | Cloudflare relay (`api.outreachmagic.io`) |
| `wbhk-billing` | Shared billing / plan limits |

**Cursor:** open `outreach-magic.code-workspace` to load all sibling repos in one window.

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT — [Outreach Magic](https://outreachmagic.io)
