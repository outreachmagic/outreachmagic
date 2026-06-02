# Releasing Outreach Magic Skill

## Two repos, one layout (read this first)

Outreach Magic has a **private dev monorepo** and a **single public install repo**. Both use the same `skills/outreachmagic/` layout.

| | Private monorepo | Public install repo |
|---|------------------|---------------------|
| **Repo** | `magic-creators/outreachmagic-skill` | `outreachmagic/outreachmagic` |
| **Who uses it** | Developers only | End users (Hermes / Cursor / Claude Code) |
| **File layout** | `skills/outreachmagic/scripts/…` | Same: `skills/outreachmagic/…` |
| **Releases** | Optional tarball on private repo | **Required** — this is what `update` downloads |
| **`GITHUB_REPO` in `pipeline.py`** | `outreachmagic/outreachmagic` | `outreachmagic/outreachmagic` |

**Rule:** End users install via `install.sh --platform <name>` from `outreachmagic/outreachmagic`. `pipeline.py update` downloads from the same repo.

```
┌─────────────────────────────────────────────────────────────────────┐
│  PRIVATE — magic-creators/outreachmagic-skill (development)         │
│                                                                     │
│  Tag v* on main triggers CI:                                        │
│    1. release.yml           → validate, tarball, Release (private)  │
│    2. publish-platforms.yml → push to outreachmagic/outreachmagic   │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  same tag (e.g. v1.21.0)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PUBLIC — outreachmagic/outreachmagic                               │
│                                                                     │
│  skills/outreachmagic/{SKILL.md,scripts,references,update-manifest} │
│  install.sh  (--platform hermes|cursor|claude)                      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ~/.hermes/skills/…      ~/.cursor/skills/…       ~/.claude/skills/…
 (profiles symlink)       (+ .mdc overlay)         (+ CLAUDE_SNIPPET)
```

### Legacy platform repos (removed)

Fresh installs use [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) only. `pipeline.py update` no longer falls back to deprecated per-platform repos.

## How `pipeline.py update` works

When a user runs `pipeline.py update`:

1. Resolve download source from `outreachmagic/outreachmagic` (`skills/outreachmagic/` prefix).
2. `GET https://api.github.com/repos/<GITHUB_REPO>/releases/latest` (or `--tag vX.Y.Z`).
3. Download each file listed in `update-manifest.json` from `raw.githubusercontent.com`.
4. Verify SHA256 checksums.
5. Overwrite local `scripts/` (and `SKILL.md`). Config and SQLite DB are **not** overwritten.

### Download URLs (unified repo)

For tag `v1.21.0`:

| File | URL |
|------|-----|
| Manifest | `https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/skills/outreachmagic/update-manifest.json` |
| Scripts | `https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/skills/outreachmagic/scripts/<file>` |
| SKILL.md | `https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/skills/outreachmagic/SKILL.md` |

**Key requirement:** The public repo must have a GitHub **Release** (not only a git tag) or `update` / `update --check` cannot find the version.

---

## How to release (maintainers)

### Prerequisites

- Push access to `magic-creators/outreachmagic-skill`
- `PUBLISH_TOKEN` secret on the repo (GitHub PAT with `repo` scope for the `outreachmagic` org)

### Steps

```bash
# 1. Bump version
echo "1.20.9" > skills/outreachmagic/scripts/VERSION

# 2. Sync SKILL.md frontmatter, then regenerate checksums (order matters for CI)
python3 -c "
import sys
sys.path.insert(0, 'skills/outreachmagic/scripts')
import pipeline as om
om.sync_skill_md_version()
"
python3 scripts/generate-update-manifest.py

# 3. Commit
git add skills/outreachmagic/scripts/VERSION skills/outreachmagic/update-manifest.json
git add skills/outreachmagic/scripts/   # and any other changed skill files
git commit -m "Release v1.20.9"

# 4. Tag and push (triggers CI)
git tag v1.20.9
git push origin main --tags
```

### After CI finishes — verify public release

Do **not** assume the private monorepo tag alone is enough. Confirm the **public install repo** has a GitHub Release:

```bash
TAG=v1.20.9

# Release exists on the unified public repo
gh release view "$TAG" --repo outreachmagic/outreachmagic

# Raw files resolve (skills/outreachmagic/ prefix)
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/outreachmagic/${TAG}/skills/outreachmagic/scripts/VERSION" | head -1
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/outreachmagic/${TAG}/skills/outreachmagic/update-manifest.json" | head -1
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh" | head -1

# Smoke-test update on a Hermes install
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --tag "$TAG"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

Check `GITHUB_REPO` in the installed script after update:

```bash
grep '^GITHUB_REPO' ~/.hermes/skills/outreachmagic/scripts/pipeline.py
# Expected: GITHUB_REPO = "outreachmagic/outreachmagic"
```

Hermes install layout (symlinks, not profile copies):

```bash
readlink ~/.hermes/profiles/*/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

See [hermes-skills-layout.md](hermes-skills-layout.md).

### What CI does automatically

- Validates (tests + SkillScan)
- Builds a release tarball on the **private** repo
- Pushes assembled bundle to `outreachmagic/outreachmagic` (tag + `main`)
- Creates a GitHub **Release** on the public repo ← required for `pipeline.py update`

### Manual release (if CI is broken)

```bash
gh auth login
TAG=v1.20.9

gh release create "$TAG" --repo outreachmagic/outreachmagic --title "$TAG" --notes "Release $TAG"
```

Re-run the `publish-platforms.yml` assemble steps locally if you need to rebuild `staging/` before pushing.

---

## Companion skills (lead-enrich, email-finder)

Three-skill suite: see [skill-suite.md](./skill-suite.md).

### lead-enrich

Published to **outreachmagic/lead-enrich**:

```bash
python3 scripts/generate-lead-enrich-manifest.py
git tag lead-enrich-v2.0.0
git push origin lead-enrich-v2.0.0
```

Workflow: `.github/workflows/publish-lead-enrich.yml` (tests + SkillScan + publish).
Domains: `docs/hermeshub-reviewed-domains-lead-enrich.md` (Serper only in v2+).

### email-finder

Published to **outreachmagic/email-finder** (create public repo first):

```bash
python3 scripts/generate-email-finder-manifest.py
git tag email-finder-v1.0.0
git push origin email-finder-v1.0.0
```

Workflow: `.github/workflows/publish-email-finder.yml`.
Domains: `docs/hermeshub-reviewed-domains-email-finder.md`.

Both companions vend `scripts/companion_common.py` in manifests. Regenerate manifests before every companion tag.

### How `enrich.py` / `email_finder.py update` works

1. `https://api.github.com/repos/outreachmagic/lead-enrich/releases/latest` (or `--tag`)
2. Download `update-manifest.json` from that release tag
3. Download each file from `raw.githubusercontent.com`
4. Verify SHA256 checksums
5. Abort on mismatch; never overwrite `config.json`

---

## User update commands

After a release is published to the **platform** repo:

```bash
# Hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --tag v1.20.8

# Cursor
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update

# Claude Code
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update
```

If `update` fails on an **old** install, use `--tag` with a version you confirmed on GitHub:

`https://github.com/outreachmagic/outreachmagic/releases`

---

## Release validation (relay / pull changes)

Before tagging releases that touch relay ingest or pull:

```bash
python3 skills/outreachmagic/scripts/pipeline.py pull --diagnose
python3 skills/outreachmagic/scripts/pipeline.py pull --full --diagnose
```

Expected:

- Diagnostics show mode, cursor start/end, pages, newest relay id, skip breakdown.
- Full pull completes without cursor stall in healthy environments.

---

## Local development (skip GitHub)

Point `dev_repo` at your monorepo clone (uses `skills/outreachmagic/scripts/` layout):

```json
// ~/.hermes/skills/outreachmagic/config/outreachmagic_config.json
{
  "dev_repo": "/Users/you/Developer/outreachmagic-skill"
}
```

Then:

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Update failed: HTTP Error 404` | Release missing on public repo or wrong tag | `gh release view vX.Y.Z --repo outreachmagic/outreachmagic`; reinstall from `outreachmagic/outreachmagic` if scripts are corrupt |
| `No GitHub release found` | Checking wrong repo (`magic-creators/outreachmagic-skill` is private) or release not created on public repo | `gh release view vX.Y.Z --repo outreachmagic/outreachmagic`; fix CI / `PUBLISH_TOKEN` |
| Tag on GitHub but update 404 | Release on **private** repo only | User needs **outreachmagic/outreachmagic**, not monorepo |
| `update --check` says no update but GitHub has newer tag | Wrong `GITHUB_REPO` in installed script | Reinstall or verify `grep GITHUB_REPO …/pipeline.py` → `outreachmagic/outreachmagic` |
| Checksum mismatch | Stale `update-manifest.json` in monorepo before tag, or manual edit of published files | Regenerate manifest before tag; re-run publish job |
| `enrich.py update` manifest error | Stale lead-enrich manifest | `python3 scripts/generate-lead-enrich-manifest.py` before `lead-enrich-v*` tag |
| CI publish fails | `PUBLISH_TOKEN` missing/expired | Rotate secret; re-run workflow |
| User on old version forever | Never got one successful platform update | See 404 row; ship fix + doc; verify with post-release checklist above |
| `platforms/common/install-companions.sh not found` on `curl \| bash` | Installer before v1.21.4 (piped script had no sibling files) | Use `main` or tag **v1.21.4+**; installer clones the public repo and re-execs |

### Bootstrap one stuck Hermes install (last resort)

Only if `update` cannot run at all. Reinstall from the unified public repo:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes --migrate
```

Or download manifest files manually from `raw.githubusercontent.com/outreachmagic/outreachmagic/<tag>/skills/outreachmagic/…` (see `update_skill` in `pipeline.py`).

Prefer `pipeline.py update --tag "$TAG"` once any v1.21+ copy is on disk.

---

## Secrets required

| Secret | Purpose | Scope |
|--------|---------|-------|
| `PUBLISH_TOKEN` | Push to `outreachmagic/outreachmagic` + create releases | GitHub PAT with `repo` for `outreachmagic` org |
