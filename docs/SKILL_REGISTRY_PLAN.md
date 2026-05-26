# Skill Registry & Security Rollout Plan

Plan for securing the Outreach Magic Hermes skill and getting it approved on HermesHub and other registries. Tracks completed work and remaining phases.

**Last updated:** Phase 1 complete (repo restructure)

---

## External domains

| Domain | Role | Status |
|--------|------|--------|
| `api.outreachmagic.io` | Cloudflare Worker relay (webhooks, pull, ack) | Production — keep |
| `dev.outreachmagic.io` | User portal (tokens, billing, routing config API) | Dev — replace with `app.outreachmagic.io` at production launch |
| `raw.githubusercontent.com` | Tagged release downloads (`pipeline.py update`) | Used only for explicit user-triggered updates pinned to GitHub release tags |

---

## Phase 0 — Pre-submission hygiene

**Goal:** Pass SkillScan and meet HermesHub Day 0 checklist.

| Task | Status |
|------|--------|
| Add `LICENSE` (MIT) | **Done** |
| Add `SECURITY.md` (data boundaries, external calls, vuln reporting) | **Done** |
| Add `docs/install.md` | **Done** |
| Open Reviewed Domains issue on `amanning3390/hermeshub` for `api.outreachmagic.io` and `dev.outreachmagic.io` | **Ready** — copy from [hermeshub-reviewed-domains-issue.md](./hermeshub-reviewed-domains-issue.md) |
| Run HermesHub `scan-skill.py` locally; fix CRITICAL findings | **Done** — SKILL.md verified (0 findings); use `bash scripts/skill-scan.sh` |
| Set GitHub repo topics (hermes-skill, agent-skill, agentskills, etc.) | **Ready** — run `bash scripts/set-repo-topics.sh` or see list below |
| Align SKILL.md frontmatter description with registry listing copy | **Done** |

**Last updated:** Phase 0 complete (pre-submission hygiene)

### GitHub repo topics (if not yet applied)

```
hermes-skill agent-skill agentskills cold-email outreach smartlead instantly lemlist claude-code sales-automation b2b-sales lead-generation mcp sqlite gtm
```

---

## Phase 1 — Standard skill layout & install path

**Goal:** One canonical install path through Hermes's built-in installer (scanned at install).

| Task | Status |
|------|--------|
| Restructure to `skills/outreachmagic/` (SKILL.md + scripts/ + references/) | **Done** |
| Move Python CLI from `pipeline/` to `skills/outreachmagic/scripts/` | **Done** |
| Update `DEFAULT_UPDATE_BASE` to new GitHub raw path | **Done** |
| Update `OUTREACHMAGIC_DEV_REPO` path to `skills/outreachmagic/scripts/` | **Done** |
| Replace `scripts/install.sh` with dev-only `scripts/sync-local.sh` (no curl pipe) | **Done** |
| Add `skills/outreachmagic/references/schema.md` | **Done** |
| Update README with `hermes skills install outreachmagic/hermes-skill` | **Done** |
| Set portal default to `https://dev.outreachmagic.io` (was sandbox Cloud Run URL) | **Done** |
| Remove deprecated `pipeline/` and `skill/` directories | **Done** |

### Install commands (current)

```bash
# End users
hermes skills inspect outreachmagic/hermes-skill
hermes skills install outreachmagic/hermes-skill
hermes -s outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init

# Local development
bash scripts/sync-local.sh
```

---

## Phase 2 — Update model (registry-critical)

**Goal:** User-triggered updates, no silent code replacement. Required for HermesHub trust signals.

| Task | Status |
|------|--------|
| Default `auto_update: false` in config | **Done** — set on every `update`; silent download removed |
| Remove `maybe_auto_update()` + `os.execv` re-exec from default CLI path | **Done** |
| Check-only by default: notify "update available", do not download | **Done** — `notify_update_available()` |
| Explicit `pipeline.py update` or `hermes skills update` as documented path | **Done** |
| Pin updates to GitHub release tags, not floating `main` | **Done** — latest release via GitHub API; `--tag` override |
| SHA256 checksums in release manifest | **Done** — `update-manifest.json` + verify on install |
| Remove or restrict `update_url` config override (dev-only env var) | **Done** — `OUTREACHMAGIC_UPDATE_URL` env only; `update_url` stripped from config |
| Document update behavior in SKILL.md and SECURITY.md | **Done** |

**Last updated:** Phase 2 complete (user-triggered updates)

### Release checklist (automated on tag push)

1. Bump `skills/outreachmagic/scripts/VERSION`
2. `python3 scripts/generate-update-manifest.py` and commit `update-manifest.json`
3. `git tag vX.Y.Z && git push origin main --tags`
4. GitHub Actions runs tests, SkillScan, builds tarball, publishes Release

Local dry run:

```bash
bash scripts/verify-release-tag.sh v1.4.5
bash scripts/build-release.sh v1.4.5
```

---

## Phase 3 — Harden scripts for SkillScan

**Goal:** Clean scan results and explicit security documentation in the skill.

| Task | Status |
|------|--------|
| Remove any `curl \| bash` references from comments and docs | **Done** (install.sh removed) |
| Document external domains in SKILL.md metadata | **Done** |
| Add Privacy & Security section to SKILL.md | **Done** |
| File permission hardening (700/600) in `pipeline.py init` (not only sync script) | **Remaining** |
| Document token storage (`config/outreachmagic_config.json`, mode 600) | **Remaining** |
| Switch portal default to `app.outreachmagic.io` at production launch | **Remaining** (dev URL intentional for now) |
| Optional: PEP 723 headers for `uv run` on scripts | **Remaining** |

---

## Phase 4 — CI/CD for releases

**Goal:** Immutable, auditable release artifacts.

| Task | Status |
|------|--------|
| `.github/workflows/skill-scan.yml` — SkillScan on every PR | **Done** |
| `.github/workflows/release.yml` — tests + scan on tag push | **Done** |
| GitHub Release tarball with VERSION + checksums | **Done** — `scripts/build-release.sh` |
| Point `pipeline.py update` at release tags (not `main`) | **Done** (Phase 2) — raw files at tag ref + manifest verify |

**Last updated:** Phase 4 complete (CI/CD)

### Release workflow

On push tag `vX.Y.Z`:

1. Verify tag matches `skills/outreachmagic/scripts/VERSION`
2. Verify `update-manifest.json` is committed and current
3. Run tests + SkillScan
4. Build `dist/outreachmagic-skill-X.Y.Z.tar.gz` + `.sha256`
5. Publish GitHub Release with tarball, checksum, and manifest

### Manual first release

```bash
python3 scripts/generate-update-manifest.py
git add skills/outreachmagic/update-manifest.json
git commit -m "chore: prepare release v1.4.5"
git tag v1.4.5
git push origin main --tags
```

---

## Phase 5 — Registry submission (Hermes)

**Goal:** Listed on HermesHub and federated registries via `outreachmagic/hermes-skill` public repo.

| Task | Status |
|------|--------|
| Day 0: Public repo with SKILL.md, SECURITY.md, LICENSE, README | **Done** — `outreachmagic/hermes-skill` |
| Day 1: Reviewed Domains issue on HermesHub | **Remaining** |
| Day 1: Local SkillScan clean | **Remaining** |
| Day 3–5: HermesHub PR (after domain review) | **Remaining** |
| Day 5–10: skills.sh submission | **Remaining** |
| Day 7: skilldock.io submission | **Remaining** |
| Day 10: awesome-hermes-agent PR | **Remaining** |
| Day 14: Verify agentskills.io auto-listing | **Remaining** |
| Day 14: LobeHub submission | **Remaining** |
| Day 30: Ship v1.0.1 patch release | **Remaining** |

### HermesHub PR target

Submit from `outreachmagic/hermes-skill` (SKILL.md at repo root). Install path: `hermes skills install outreachmagic/hermes-skill`.

### Reviewed Domains request (draft)

Request review for:

- **api.outreachmagic.io** — relay webhooks and authenticated pull; payloads pass through, not stored
- **dev.outreachmagic.io** — portal for token generation, billing, routing config API (will become app.outreachmagic.io)

---

## Phase 6 — Cross-platform distribution

**Goal:** Published and discoverable on Cursor and Claude Code directories when they exist.

| Task | Status |
|------|--------|
| Multi-platform repo structure (`platforms/hermes`, `platforms/cursor`, `platforms/claude-code`) | **Done** |
| Universal `om_paths.py` with `_infer_data_root()` | **Done** |
| `publish-platforms.yml` CI workflow | **Done** |
| `outreachmagic/hermes-skill` public repo | **Remaining** — create on GitHub |
| `outreachmagic/cursor-skill` public repo | **Remaining** — create on GitHub |
| `outreachmagic/claude-code-skill` public repo | **Remaining** — create on GitHub |
| `PUBLISH_TOKEN` secret on `magic-creators/outreachmagic-skill` | **Remaining** |
| First tagged release to populate all platform repos | **Remaining** |
| Submit to Cursor skill directory (when available) | **Future** |
| Submit to Claude Code directory (when available) | **Future** |

---

## Known issues / follow-ups

- **Test failure:** `test_workspace_routing.py::test_single_mode_routes_all_to_default` — **fixed** (config sync for single mode).
- **First GitHub release:** Push tag `v1.4.5` (or current VERSION) to trigger the release workflow.
- **Repo migration:** Dev repo moved from `outreachmagic/hermes-agent` to `magic-creators/outreachmagic-skill` (private). Public repos under `outreachmagic` org are CI-published artifacts.

---

## Priority order for next work

1. **Phase 6** — Create public repos, add PUBLISH_TOKEN, first release
2. **Phase 5** — Registry submissions (after Reviewed Domains + first release tag)
3. **Phase 3** — Remaining hardening items
4. **Phase 0 manual** — HermesHub domains issue, GitHub topics (`gh auth login`)
