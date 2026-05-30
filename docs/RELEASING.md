# Releasing OutreachMagic Skill

## Two repos, two layouts (read this first)

OutreachMagic has **two different GitHub homes**. Mixing them up breaks `pipeline.py update`.

| | Private monorepo | Public platform repos |
|---|------------------|------------------------|
| **Repo** | `magic-creators/outreachmagic-skill` | `outreachmagic/hermes-outreachmagic`, `outreachmagic/cursor-outreachmagic`, `outreachmagic/claude-code-outreachmagic` |
| **Who uses it** | Developers only | End users (Hermes / Cursor / Claude Code installs) |
| **File layout** | `skills/outreachmagic/scripts/…` | Flat: `scripts/…`, `update-manifest.json` at repo root |
| **Releases** | Optional tarball on private repo | **Required** — this is what `update` downloads |
| **`GITHUB_REPO` in `pipeline.py`** | `outreachmagic/outreachmagic-skill` (source default) | Rewritten by CI to the platform repo name |

**Rule:** Users never run `update` against the private monorepo. They run it against the **platform repo** for how they installed the skill.

```
┌─────────────────────────────────────────────────────────────────────┐
│  PRIVATE — magic-creators/outreachmagic-skill (development)         │
│                                                                     │
│  You develop here. Tag v* on main triggers CI:                      │
│    1. release.yml           → validate, tarball, Release (private)  │
│    2. publish-platforms.yml → push bundles to PUBLIC platform repos │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  same tag (e.g. v1.20.8)
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
┌─────────────────────────┐ ┌─────────────────────────┐ ┌──────────────────────────┐
│ outreachmagic/          │ │ outreachmagic/          │ │ outreachmagic/           │
│ hermes-outreachmagic    │ │ cursor-outreachmagic    │ │ claude-code-outreachmagic│
│                         │ │                         │ │                          │
│ scripts/                │ │ scripts/                │ │ scripts/                 │
│ references/             │ │ references/             │ │ references/              │
│ SKILL.md                │ │ SKILL.md                │ │ SKILL.md                 │
│ update-manifest.json    │ │ update-manifest.json    │ │ update-manifest.json     │
│ README.md               │ │ README.md (+ .mdc)      │ │ README.md (+ snippet)    │
└─────────────────────────┘ └─────────────────────────┘ └──────────────────────────┘
         ▲                           ▲                            ▲
         │                           │                            │
   ~/.hermes/skills/…          ~/.cursor/skills/…           ~/.claude/skills/…
```

## How `pipeline.py update` works

On each install, `pipeline.py` contains a `GITHUB_REPO` constant. **Published** copies point at the platform repo; **old** copies may still say `outreachmagic/outreachmagic-skill` (wrong for Hermes).

When a user runs `pipeline.py update`:

1. Resolve download source (see below).
2. `GET https://api.github.com/repos/<GITHUB_REPO>/releases/latest` (or `--tag vX.Y.Z`).
3. Download each file listed in `update-manifest.json` from `raw.githubusercontent.com`.
4. Verify SHA256 checksums.
5. Overwrite local `scripts/` (and `SKILL.md`). Config and SQLite DB are **not** overwritten.

### Download URLs (platform repos)

For Hermes (`outreachmagic/hermes-outreachmagic`), tag `v1.20.8`:

| File | URL |
|------|-----|
| Manifest | `https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.8/update-manifest.json` |
| Scripts | `https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/v1.20.8/scripts/<file>` |

**Wrong URL (404)** — monorepo layout on private repo name:

`https://raw.githubusercontent.com/outreachmagic/outreachmagic-skill/v1.20.8/skills/outreachmagic/scripts/pipeline.py`

### What CI rewrites on publish

`publish-platforms.yml` copies scripts into a flat `staging/` tree and runs:

```bash
sed -i 's|GITHUB_REPO = "outreachmagic/outreachmagic-skill"|GITHUB_REPO = "outreachmagic/hermes-outreachmagic"|' staging/scripts/pipeline.py
sed -i 's|SKILL_REPO_PATH = "skills/outreachmagic"|SKILL_REPO_PATH = "."|' staging/scripts/pipeline.py
```

| Platform install path | Public repo (`GITHUB_REPO`) | `SKILL_REPO_PATH` |
|-----------------------|-----------------------------|-------------------|
| `~/.hermes/skills/outreachmagic/…` | `outreachmagic/hermes-outreachmagic` | `.` |
| `~/.cursor/skills/outreachmagic/…` | `outreachmagic/cursor-outreachmagic` | `.` |
| `~/.claude/skills/outreachmagic/…` | `outreachmagic/claude-code-outreachmagic` | `.` |
| Monorepo clone (developers) | `outreachmagic/outreachmagic-skill` | `skills/outreachmagic` |

**v1.20.8+** also infers the platform repo from the install path when `GITHUB_REPO` is still the monorepo default, so one successful update fixes older installs.

**Key requirement:** Each **platform** repo must have a GitHub **Release** (not only a git tag) or `update` / `update --check` cannot find the version.

---

## How to release (maintainers)

### Prerequisites

- Push access to `magic-creators/outreachmagic-skill`
- `PUBLISH_TOKEN` secret on the repo (GitHub PAT with `repo` scope for the `outreachmagic` org)

### Steps

```bash
# 1. Bump version
echo "1.20.9" > skills/outreachmagic/scripts/VERSION

# 2. Regenerate checksums for the monorepo manifest (used before CI republishes per platform)
python3 scripts/generate-update-manifest.py

# 3. Commit
git add skills/outreachmagic/scripts/VERSION skills/outreachmagic/update-manifest.json
git add skills/outreachmagic/scripts/   # and any other changed skill files
git commit -m "Release v1.20.9"

# 4. Tag and push (triggers CI)
git tag v1.20.9
git push origin main --tags
```

### After CI finishes — verify platform releases

Do **not** assume the private monorepo tag alone is enough. Confirm **each** public repo:

```bash
TAG=v1.20.9

# Releases exist
gh release view "$TAG" --repo outreachmagic/hermes-outreachmagic
gh release view "$TAG" --repo outreachmagic/cursor-outreachmagic
gh release view "$TAG" --repo outreachmagic/claude-code-outreachmagic

# Raw files resolve (Hermes example)
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/${TAG}/scripts/VERSION" | head -1
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/${TAG}/update-manifest.json" | head -1

# Optional: smoke-test update on a Hermes install
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --tag "$TAG"
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

Check `GITHUB_REPO` in the installed script after update:

```bash
grep '^GITHUB_REPO' ~/.hermes/skills/outreachmagic/scripts/pipeline.py
# Expected: GITHUB_REPO = "outreachmagic/hermes-outreachmagic"
```

Hermes install layout (symlinks, not profile copies):

```bash
curl -fsSI "https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/${TAG}/install.sh" | head -1
readlink ~/.hermes/profiles/*/skills/outreachmagic   # → ../../../skills/outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py paths
```

See [hermes-skills-layout.md](hermes-skills-layout.md).

### What CI does automatically

- Validates (tests + SkillScan)
- Builds a release tarball on the **private** repo
- Pushes assembled bundles to all three **public** platform repos (tag + `main`)
- Creates a GitHub **Release** on each public repo ← required for `pipeline.py update`

### Manual release (if CI is broken)

```bash
gh auth login
TAG=v1.20.9

gh release create "$TAG" --repo outreachmagic/hermes-outreachmagic --title "$TAG" --notes "Release $TAG"
gh release create "$TAG" --repo outreachmagic/cursor-outreachmagic --title "$TAG" --notes "Release $TAG"
gh release create "$TAG" --repo outreachmagic/claude-code-outreachmagic --title "$TAG" --notes "Release $TAG"
```

Re-run the `publish-platforms.yml` assemble/sed steps locally if you need to rebuild `staging/` before pushing.

---

## Companion skills (lead-enrich, lead-email)

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

### lead-email

Published to **outreachmagic/lead-email** (create public repo first):

```bash
python3 scripts/generate-lead-email-manifest.py
git tag lead-email-v1.0.0
git push origin lead-email-v1.0.0
```

Workflow: `.github/workflows/publish-lead-email.yml`.
Domains: `docs/hermeshub-reviewed-domains-lead-email.md`.

Both companions vend `scripts/companion_common.py` in manifests. Regenerate manifests before every companion tag.

### How `enrich.py` / `lead_email.py update` works

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

If `update` fails on an **old** install, use `--tag` with a version you confirmed on GitHub (Hermes):

`https://github.com/outreachmagic/hermes-outreachmagic/releases`

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
| `Update failed: HTTP Error 404` | Installed `pipeline.py` still uses monorepo `GITHUB_REPO` + `skills/outreachmagic/…` paths against a tag that only exists on **hermes-outreachmagic** | Run `update --tag vX.Y.Z` on **v1.20.8+** (infers platform repo), or bootstrap from `raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/<tag>/scripts/…` |
| `No GitHub release found` | Checking wrong repo (`outreachmagic/outreachmagic-skill` is private) or release not created on platform repo | `gh release view vX.Y.Z --repo outreachmagic/hermes-outreachmagic`; fix CI / `PUBLISH_TOKEN` |
| Tag on GitHub but update 404 | Release on **private** repo or wrong repo name | User needs **hermes-outreachmagic** (etc.), not monorepo |
| `update --check` says no update but GitHub has newer tag | Same wrong `GITHUB_REPO` | Fix install source; verify with `grep GITHUB_REPO …/pipeline.py` |
| Checksum mismatch | Stale `update-manifest.json` in monorepo before tag, or manual edit of published files | Regenerate manifest before tag; re-run publish job |
| `enrich.py update` manifest error | Stale lead-enrich manifest | `python3 scripts/generate-lead-enrich-manifest.py` before `lead-enrich-v*` tag |
| CI publish fails | `PUBLISH_TOKEN` missing/expired | Rotate secret; re-run workflow |
| User on old version forever | Never got one successful platform update | See 404 row; ship fix + doc; verify with post-release checklist above |

### Bootstrap one stuck Hermes install (last resort)

Only if `update` cannot run at all. Replace files from the **Hermes** public repo for a known good tag:

```bash
TAG=v1.20.8
BASE="https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/${TAG}"
DEST="$HOME/.hermes/skills/outreachmagic"
# Download manifest + verify each file in manifest["files"] (see update_skill in pipeline.py)
```

Prefer `pipeline.py update --tag "$TAG"` once any copy with platform inference (v1.20.8+) is on disk.

---

## Secrets required

| Secret | Purpose | Scope |
|--------|---------|-------|
| `PUBLISH_TOKEN` | Push to `outreachmagic/*` platform repos + create releases | GitHub PAT with `repo` for `outreachmagic` org |
