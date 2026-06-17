# Releasing Outreach Magic Skill

## Architecture

**Private monorepo** (`magic-creators/outreachmagic-skill`) → **three public repos**:

| Skill | Public repo | Tag prefix |
|-------|-------------|------------|
| outreachmagic (main) | `outreachmagic/outreachmagic` | `v` |
| email-finder (companion) | `outreachmagic/email-finder` | `email-finder-v` |
| lead-enrich (companion) | `outreachmagic/lead-enrich` | `lead-enrich-v` |

The public repo's `main` branch is always the latest committed code (updated on every merge to monorepo `main`). Tags create permanent GitHub Releases — users get the latest tagged release by default.

```
┌─────────────────────────────────────────────────────────┐
│ PRIVATE: magic-creators/outreachmagic-skill             │
│                                                         │
│  Push to main → CI publishes main to public repo        │
│  Push tag v*    → CI creates GitHub Release on public   │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│ PUBLIC: outreachmagic/outreachmagic                     │
│                                                         │
│  main branch  → latest code (--channel main)            │
│  tags+vX.Y.Z  → permanent snapshots (default update)    │
└─────────────────────────────────────────────────────────┘
```

### How users update

`pipeline.py update` → `GET /repos/outreachmagic/outreachmagic/releases/latest` → downloads from that tag's raw files. Users can `--tag vX.Y.Z` for a specific version or `--channel main` for bleeding edge.

---

## Testing before release

Three methods, from local to public:

### 1. Local clone (`dev_repo` config)

Clone the monorepo on the test machine. Add to config:

```json
// outreachmagic_config.json
{ "dev_repo": "/home/you/outreachmagic-skill" }
```

Then `pipeline.py update` copies files directly from the local clone. No tag or network download needed.

To unset: remove `dev_repo` from config.

### 2. Bleeding edge (`--channel main`)

Merge code to monorepo `main`. CI pushes to the public repo's `main` branch automatically. On the test machine:

```bash
pipeline.py update --channel main
```

Installs from `raw.githubusercontent.com/outreachmagic/outreachmagic/main/...`. No tag required.

### 3. Release candidate tags (`--tag`)

For beta testing before a final release:

```bash
echo "1.1.0" > skills/outreachmagic/scripts/VERSION
python3 -c "import sys; sys.path.insert(0,'skills/outreachmagic/scripts'); import pipeline as om; om.sync_skill_md_version()"
python3 scripts/sync_install_docs.py
make release-check
git commit -am "v1.1.0-rc.1"
git tag v1.1.0-rc.1
git push origin main --tags
```

Creating a tag matching `v*-*` (contains a hyphen) triggers CI as a **prerelease** — it publishes to the public repo but won't appear as the "latest" release to users. Testers install with:

```bash
pipeline.py update --tag v1.1.0-rc.1
```

Fix bugs, bump to `rc.2`, repeat. When satisfied, create the stable release (no hyphen).

---

## Release process (main skill)

### Prerequisites

- Push access to `magic-creators/outreachmagic-skill`
- `PUBLISH_TOKEN` secret on the repo (GitHub PAT with `repo` scope for the `outreachmagic` org)
- Dark factory tests pass (see [dark-factory-setup.md](./dark-factory-setup.md))

### Steps

```bash
# 1. Bump version
echo "1.1.0" > skills/outreachmagic/scripts/VERSION

# 2. Sync SKILL.md frontmatter version
python3 -c "
import sys
sys.path.insert(0, 'skills/outreachmagic/scripts')
import pipeline as om
om.sync_skill_md_version()
"

# 3. Sync install docs (updates OM_VERSION in all docs + snippet)
python3 scripts/sync_install_docs.py

# 4. Regenerate manifests and run pre-tag gate
make release-check

# 5. Commit
git add -A
git commit -m "Release v1.1.0"

# 6. Tag and push (triggers CI: test → publish to public repo → create GitHub Release)
git tag v1.1.0
git push origin main --tags
```

### After CI finishes — verify

```bash
TAG=v1.1.0

# Release exists on public repo
gh release view "$TAG" --repo outreachmagic/outreachmagic

# Raw files resolve
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/outreachmagic/${TAG}/skills/outreachmagic/scripts/VERSION" | head -1

# Install smoke test
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update --check
```

---

## Companion skills (email-finder, lead-enrich)

Companion skills are published to their own repos (`outreachmagic/email-finder`, `outreachmagic/lead-enrich`). The same tag-as-release pattern applies.

### Update companion_common.py before tagging

Companion skills share `scripts/companion_common.py`. Canonical file lives in `skills/email-finder/scripts/`. After editing it:

```bash
# Sync to lead-enrich, then verify
bash scripts/sync-companion-common.sh
bash scripts/sync-companion-common.sh --check  # CI gate

# Regenerate manifests for both
python3 scripts/generate_skill_manifest.py --all
```

### Tagging a companion

```bash
# 1. Sync companion_common and regenerate manifests
bash scripts/sync-companion-common.sh
python3 scripts/generate_skill_manifest.py --all

# 2. Verify
make release-check

# 3. Commit companion changes if any
git add -A
git commit -m "email-finder: validate CC number digits"

# 4. Tag (companion version read from SKILL.md frontmatter)
git tag email-finder-v1.0.1
git push origin main --tags
```

This triggers `.github/workflows/publish-email-finder.yml` (test → publish to `outreachmagic/email-finder` → create release).

For lead-enrich, same pattern with `lead-enrich-v*`.

> Companion tags are **not** "new version of everything" — they only deploy the companion skill. The main skill (outreachmagic) deploys independently via its own `v*` tags.

### Companion testing

Same patterns as the main skill. For a local copy set `dev_repo` in the companion's config. Companion skills don't have `--channel main` — use `--tag` with an RC tag for beta testing.

---

## What CI does

| Trigger | Action |
|---------|--------|
| Push to `main` (monorepo) | Publishes assembled files to public repo's `main` branch |
| Tag `v*` (monorepo) | Publishes to public repo's `main` + creates git tag + creates GitHub Release |
| Tag `v*-*` (prerelease) | Same as above, but release is marked as prerelease (not `latest`) |
| Tag `email-finder-v*` | Tests + publishes to `outreachmagic/email-finder` + creates release |
| Tag `lead-enrich-v*` | Tests + publishes to `outreachmagic/lead-enrich` + creates release |

Workflows: `publish-platforms.yml`, `publish-email-finder.yml`, `publish-lead-enrich.yml`.

---

## Version numbering

- **Stable**: `v1.0.0`, `v1.1.0`, `v1.1.1` — patch for hotfixes, minor for features, major for breaking changes.
- **Release candidate**: `v1.1.0-rc.1`, `v1.1.0-rc.2` — prerelease tags for testing before the stable release.
- **Companion skills** follow the same pattern with their prefix: `email-finder-v1.0.1`, `lead-enrich-v1.1.0-rc.1`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No GitHub release found` | Tag pushed but no release created | Check CI logs on monorepo; verify `PUBLISH_TOKEN` is valid |
| Update 404 from public repo | Release exists on private repo only | Check `outreachmagic/outreachmagic`, not the monorepo |
| `Checksum mismatch` | Manifest generated before code changes were final | Regenerate manifest and retag |
| CI publish fails | `PUBLISH_TOKEN` missing or expired | Rotate secret, re-run workflow |
| `--channel main` downloads old code | Public repo `main` stale | Verify `publish-platforms.yml` ran on last merge |

---

## Secrets

| Secret | Purpose | Scope |
|--------|---------|-------|
| `PUBLISH_TOKEN` | Push to `outreachmagic/*` + create releases | GitHub PAT with `repo` for `outreachmagic` org |
