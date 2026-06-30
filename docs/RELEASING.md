# Releasing Outreach Magic

## Architecture

This is the public monorepo. CI publishes companion skills to their own repos on tag:

| Skill | Public repo | Tag prefix |
|-------|-------------|------------|
| outreachmagic (main) | `outreachmagic/outreachmagic` | `v` |
| email-finder (companion) | `outreachmagic/email-finder` | `email-finder-v` |
| lead-enrich (companion) | `outreachmagic/lead-enrich` | `lead-enrich-v` |

`main` is always the latest committed code. Tags create permanent GitHub Releases — users get the latest tagged release by default.

### How users update

`pipeline.py update` → `GET /repos/outreachmagic/outreachmagic/releases/latest` → downloads from that tag's raw files. Users can `--tag vX.Y.Z` for a specific version or `--channel main` for bleeding edge.

---

## Testing before release

### 1. Local clone (`dev_repo` config)

Clone this repo on the test machine. Add to config:

```json
{ "dev_repo": "/home/you/outreachmagic" }
```

Then `pipeline.py update` copies files directly from the local clone. No tag or network download needed.

To unset: remove `dev_repo` from config.

### 2. Bleeding edge (`--channel main`)

Merge code to `main`. On the test machine:

```bash
pipeline.py update --channel main
```

Installs from `raw.githubusercontent.com/outreachmagic/outreachmagic/main/...`. No tag required.

### 3. Release candidate tags (`--tag`)

For beta testing before a final release:

```bash
echo "1.2.0" > skills/outreachmagic/scripts/VERSION
make manifests
make release-check
git commit -am "v1.2.0-rc.1"
git tag v1.2.0-rc.1
git push origin main --tags
```

Creating a tag matching `v*-*` (contains a hyphen) triggers CI as a **prerelease** — it creates a GitHub Release but won't appear as "latest." Testers install with:

```bash
pipeline.py update --tag v1.3.0-rc.1
```

Fix bugs, bump to `rc.2`, repeat. When satisfied, create the stable release (no hyphen).

---

## Release process (main skill)

### Prerequisites

- Push access to `outreachmagic/outreachmagic`
- Tests pass (see [run-tests.sh](../scripts/run-tests.sh))

### Steps

```bash
# 1. Bump version
echo "1.2.0" > skills/outreachmagic/scripts/VERSION

# 2. Regenerate manifests and run pre-tag gate
make manifests
make release-check

# 3. Commit
git add -A
git commit -m "Release v1.2.0"

# 4. Tag and push (triggers CI: test → create GitHub Release)
git tag v1.2.0
git push origin main --tags
```

### After CI finishes — verify

```bash
TAG=v1.2.0

# Release exists
gh release view "$TAG" --repo outreachmagic/outreachmagic

# Raw files resolve
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/outreachmagic/${TAG}/skills/outreachmagic/scripts/VERSION" | head -1

# Install smoke test
pipeline.py update --check
```

---

## Companion skills (email-finder, lead-enrich)

Companion skills are published to their own repos (`outreachmagic/email-finder`, `outreachmagic/lead-enrich`). The same tag-as-release pattern applies.

### Update companion_common.py before tagging

Companion skills share `scripts/companion_common.py`. Canonical file lives in `skills/email-finder/scripts/`. After editing it:

```bash
bash scripts/sync-companion-common.sh
python3 scripts/generate_skill_manifest.py --all
```

### Tagging a companion

```bash
bash scripts/sync-companion-common.sh
python3 scripts/generate_skill_manifest.py --all
make release-check
git add -A
git commit -m "email-finder: validate CC number digits"
git tag email-finder-v1.0.1
git push origin main --tags
```

This triggers `.github/workflows/publish-email-finder.yml` (test → publish to `outreachmagic/email-finder` → create release).

For lead-enrich, same pattern with `lead-enrich-v*`.

> Companion tags only deploy the companion skill. The main skill (outreachmagic) deploys independently via its own `v*` tags.

### Companion testing

Same patterns as the main skill. For a local copy set `dev_repo` in the companion's config. Companion skills don't have `--channel main` — use `--tag` with an RC tag for beta testing.

---

## What CI does

| Trigger | Action |
|---------|--------|
| Push to `main` | Tests run. Manual release via tag. |
| Tag `v*` | Creates GitHub Release on `outreachmagic/outreachmagic` |
| Tag `v*-*` (prerelease) | Same as above, marked as prerelease (not `latest`) |
| Tag `email-finder-v*` | Tests + publishes to `outreachmagic/email-finder` + creates release |
| Tag `lead-enrich-v*` | Tests + publishes to `outreachmagic/lead-enrich` + creates release |

---

## Version numbering

- **Stable**: `v1.0.0`, `v1.1.0`, `v1.1.1` — patch for hotfixes, minor for features, major for breaking changes.
- **Release candidate**: `v1.1.0-rc.1`, `v1.1.0-rc.2` — prerelease tags for testing before the stable release.
- **Companion skills** follow the same pattern with their prefix: `email-finder-v1.0.1`, `lead-enrich-v1.1.0-rc.1`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No GitHub release found` | Tag pushed but no release created | Check CI logs; verify `PUBLISH_TOKEN` is valid |
| Update 404 from public repo | Release not yet published | Verify tag exists on `outreachmagic/outreachmagic` |
| `Checksum mismatch` | Manifest generated before code changes were final | Regenerate manifest and retag |
| CI publish fails | `PUBLISH_TOKEN` missing or expired | Rotate secret, re-run workflow |
| `--channel main` downloads old code | Public repo `main` stale | Verify CI ran on last merge |
