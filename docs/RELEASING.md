# Releasing Outreach Magic

## Architecture

This is the public monorepo. CI publishes the outreachmagic skill to `outreachmagic/outreachmagic` on tag:

| Skill | Public repo | Tag prefix |
|-------|-------------|------------|
| outreachmagic | `outreachmagic/outreachmagic` | `v` |

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

## Release process

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

## What CI does

| Trigger | Action |
|---------|--------|
| Push to `main` | Tests run. Manual release via tag. |
| Tag `v*` | Creates GitHub Release on `outreachmagic/outreachmagic` |
| Tag `v*-*` (prerelease) | Same as above, marked as prerelease (not `latest`) |

---

## Version numbering

- **Stable**: `v1.0.0`, `v1.1.0`, `v1.1.1` — patch for hotfixes, minor for features, major for breaking changes.
- **Release candidate**: `v1.1.0-rc.1`, `v1.1.0-rc.2` — prerelease tags for testing before the stable release.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No GitHub release found` | Tag pushed but no release created | Check CI logs; verify `PUBLISH_TOKEN` is valid |
| Update 404 from public repo | Release not yet published | Verify tag exists on `outreachmagic/outreachmagic` |
| `Checksum mismatch` | Manifest generated before code changes were final | Regenerate manifest and retag |
| CI publish fails | `PUBLISH_TOKEN` missing or expired | Rotate secret, re-run workflow |
| `--channel main` downloads old code | Public repo `main` stale | Verify CI ran on last merge |
