# Releasing OutreachMagic Skill

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  PRIVATE (magic-creators org)                                       │
│                                                                     │
│  magic-creators/outreachmagic-skill   ← development monorepo       │
│                                                                     │
│  On push of a v* tag, CI runs:                                      │
│    1. release.yml        → validates, builds tarball, creates        │
│                            GitHub Release on THIS repo               │
│    2. publish-platforms.yml → assembles platform-specific bundles    │
│                               and pushes to PUBLIC repos             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    pushes code + tag + GitHub Release
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
┌───────────────────┐ ┌─────────────────┐ ┌─────────────────────┐
│  PUBLIC (outreachmagic org)                                     │
│                                                                 │
│  outreachmagic/     outreachmagic/      outreachmagic/          │
│  hermes-skill       cursor-skill        claude-code-skill       │
│                                                                 │
│  Each contains:     Each contains:      Each contains:          │
│  - scripts/         - scripts/          - scripts/              │
│  - references/      - references/       - references/           │
│  - SKILL.md         - SKILL.md          - CLAUDE_SNIPPET.md     │
│  - README.md        - README.md         - README.md             │
└─────────────────────────────────────────────────────────────────┘
```

## How `pipeline.py update` Works

When a user runs `pipeline.py update`, it:

1. Calls `https://api.github.com/repos/<GITHUB_REPO>/releases/latest`
2. Gets the latest release tag (e.g. `v1.6.1`)
3. Downloads files from `https://raw.githubusercontent.com/<GITHUB_REPO>/<tag>/...`
4. Verifies checksums against `update-manifest.json`
5. Overwrites local scripts with the new version

The `GITHUB_REPO` constant in `pipeline.py` is set to `"outreachmagic/outreachmagic-skill"` in the source.
During publishing, CI rewrites it per-platform:

| Platform   | GITHUB_REPO value                    |
|------------|--------------------------------------|
| Hermes     | `outreachmagic/hermes-outreachmagic`         |
| Cursor     | `outreachmagic/cursor-outreachmagic`         |
| Claude Code| `outreachmagic/claude-code-outreachmagic`    |

**Key requirement:** Each public repo must have a GitHub Release (not just a tag) for the update command to find it.

## lead-enrich companion skill

Published separately to **outreachmagic/lead-enrich** when you push a tag:

```bash
git tag lead-enrich-v1.1.1
git push origin lead-enrich-v1.1.1
```

Workflow: `.github/workflows/publish-lead-enrich.yml` (requires `PUBLISH_TOKEN` and the
public repo to exist). HermesHub domain request template:
`docs/hermeshub-reviewed-domains-lead-enrich.md`.

### How `enrich.py update` Works

When a user runs `enrich.py update`, it:

1. Calls `https://api.github.com/repos/outreachmagic/lead-enrich/releases/latest` (or `--tag`)
2. Downloads `update-manifest.json` from that release tag
3. Downloads each skill file from `raw.githubusercontent.com`
4. Verifies SHA256 checksums for every file in the manifest
5. Aborts on any mismatch; otherwise overwrites local skill files

`config.json` is not part of the release bundle and is never overwritten.

## How to Release

### Prerequisites

- Push access to `magic-creators/outreachmagic-skill`
- `PUBLISH_TOKEN` secret configured in the repo (a GitHub PAT with `repo` scope for the `outreachmagic` org)

### Steps

```bash
# 1. Bump the version
echo "1.7.0" > skills/outreachmagic/scripts/VERSION

# 2. Regenerate the update manifest (checksums for each file)
python3 scripts/generate-update-manifest.py

# 3. Commit
git add -A
git commit -m "Release v1.7.0"

# 4. Tag and push
git tag v1.7.0
git push origin main --tags
```

### lead-enrich patch release steps

```bash
# 1. Bump lead-enrich version in frontmatter
# skills/lead-enrich/SKILL.md -> version: 1.1.5

# 2. Regenerate lead-enrich update manifest
python3 scripts/generate-lead-enrich-manifest.py

# 3. Commit
git add skills/lead-enrich scripts/generate-lead-enrich-manifest.py .github/workflows/publish-lead-enrich.yml
git commit -m "Release lead-enrich v1.1.5"

# 4. Tag and push
git tag lead-enrich-v1.1.5
git push origin main --tags
```

CI then automatically:
- Validates (tests + SkillScan)
- Builds a release tarball
- Creates a GitHub Release on the private repo
- Pushes assembled bundles to all 3 public repos
- Creates a GitHub Release on each public repo ← enables `pipeline.py update`

### Manual Release (if CI is broken)

If you need to manually create releases on the public repos:

```bash
gh auth login

# Push latest code (from this repo's publish workflow logic)
gh release create v1.6.1 --repo outreachmagic/cursor-outreachmagic --title "v1.6.1" --notes "Release v1.6.1"
gh release create v1.6.1 --repo outreachmagic/hermes-outreachmagic --title "v1.6.1" --notes "Release v1.6.1"
gh release create v1.6.1 --repo outreachmagic/claude-code-outreachmagic --title "v1.6.1" --notes "Release v1.6.1"
```

## User Update Commands

After a release is published, users update with:

```bash
# Cursor
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update

# Hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update

# Claude Code
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update

# Specific version
python3 <path>/scripts/pipeline.py update --tag v1.6.1

# Check only (don't install)
python3 <path>/scripts/pipeline.py update --check

# lead-enrich update (all platforms)
python3 <path>/scripts/enrich.py update --check
python3 <path>/scripts/enrich.py update
python3 <path>/scripts/enrich.py update --tag v1.1.5
```

## Local Development (skip GitHub entirely)

For testing changes before publishing, users or developers can set `dev_repo` in their config:

```json
// ~/.cursor/skills/outreachmagic/config/outreachmagic_config.json
{
  "dev_repo": "/Users/you/Developer/outreachmagic-skill"
}
```

Then `pipeline.py update` copies directly from the local clone.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No GitHub release found" | Public repo has tags but no Release | Create a Release: `gh release create <tag> --repo outreachmagic/<platform>-skill` |
| "No GitHub release found" | `GITHUB_REPO` not rewritten by CI | Check `publish-platforms.yml` sed patterns match the source value |
| Releases exist but update pulls old version | `update-manifest.json` not regenerated | Run `python3 scripts/generate-update-manifest.py` before tagging |
| `enrich.py update` fails with manifest error | lead-enrich manifest missing or stale | Run `python3 scripts/generate-lead-enrich-manifest.py` before `lead-enrich-v*` tag |
| CI publish fails | `PUBLISH_TOKEN` missing or expired | Update the secret in repo settings |
| Repo 404 from GitHub API | Repo is private | Public repos must stay public for unauthenticated update checks |

## Secrets Required

| Secret | Purpose | Scope |
|--------|---------|-------|
| `PUBLISH_TOKEN` | Push to `outreachmagic/*` public repos + create releases | GitHub PAT with `repo` scope for the `outreachmagic` org |
