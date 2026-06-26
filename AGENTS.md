# Agent guide — outreachmagic/outreachmagic monorepo

Read this first when changing skills, install, or release.

## Layout

| Skill | CLI | Config |
|-------|-----|--------|
| outreachmagic | `skills/outreachmagic/scripts/pipeline.py` | `skill-suite.json` |
| email-finder | `skills/email-finder/scripts/email_finder.py` | same |
| lead-enrich | `skills/lead-enrich/scripts/enrich.py` | same |

**Single source of truth:** [`skill-suite.json`](skill-suite.json) — install pins, manifest file lists, public repos.

## If you add `skills/<skill>/scripts/*.py`

1. Add to `script_exclude` in `skill-suite.json` **only** if the file must not ship (e.g. `run_v22_tests.py`).
2. Run: `python3 scripts/generate_skill_manifest.py <skill>` or `make manifests`
3. Run: `make release-check`

Do **not** edit `UPDATE_FILES` or hand-maintained manifest tuples — companions read `update-manifest.json` keys at update time.

## Public README (outreachmagic skill)

**Single file:** [`skills/outreachmagic/README.md`](skills/outreachmagic/README.md) — published to [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) and the [GitHub org profile](https://github.com/outreachmagic). Do not add copies under `platforms/`. See [`docs/github-org-profile.md`](docs/github-org-profile.md).

After README edits: `make manifests` then commit (manifest hash for `README.md` must match).

## If you change pricing / billing limits

1. `tests/billing_contract.json`

## Release (outreachmagic)

1. Add section to `CHANGELOG.md` for the new version (use human-readable, user-facing language).
2. Bump version and run gate:

```bash
echo X.Y.Z > skills/outreachmagic/scripts/VERSION
python3 -c "import sys; sys.path.insert(0,'skills/outreachmagic/scripts'); import pipeline as om; om.sync_skill_md_version()"
python3 scripts/sync_install_docs.py
make release-check
git commit -am "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

Companion tags: `email-finder-v*` / `lead-enrich-v*` (see `skill-suite.json` → `release_tag_prefix`).

## Testing before tagging

- **`dev_repo` config** — point `outreachmagic_config.json` at a local clone. `pipeline.py update` copies from disk.
- **`--channel main`** — merge to `main`, then run `pipeline.py update --channel main` on the test machine. Release tags trigger CI to create GitHub Releases directly on this repo.
- **RC tags** — tag `vX.Y.Z-rc.1` to publish a prerelease to the public repo. Testers run `pipeline.py update --tag vX.Y.Z-rc.1`.

## Brand assets

Logos live in `brand/` and publish to [outreachmagic/brand](https://github.com/outreachmagic/brand) via `publish-brand.yml` on merge to `main`.

## Public vs private

This repo is public. All source code is visible. Secrets live in environment variables and `skills/outreachmagic/config/` (gitignored). Companion publish workflows push to `outreachmagic/email-finder` and `outreachmagic/lead-enrich` (read-only mirrors with redirect banners). Brand assets publish to `outreachmagic/brand`. Org profile publishes to `outreachmagic/.github`.

## Tests before tag

```bash
make release-check          # full pre-tag gate
```
