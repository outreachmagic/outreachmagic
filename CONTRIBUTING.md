# Contributing to Outreach Magic

Outreach Magic syncs your outbound pipeline into one local SQLite database your agent can query. No more CSV stitching across Smartlead, Instantly, and HeyReach. If you run GTM outreach through AI coding agents and know Python, you can help make this better.

## Dev setup

```bash
git clone https://github.com/outreachmagic/outreachmagic
cd outreachmagic
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Everything else is local. No cloud database, no Docker. Just Python and SQLite.

## Running tests

```bash
bash scripts/run-tests.sh          # Full test suite
bash scripts/skill-scan.sh          # HermesHub SkillScan validation (or: hermes skills publish --dry-run)
make layer1                         # Fast pre-tag gate (pull, billing, install contract)
```

Tests run on every PR. If you're adding a feature, add tests for it.

## Project structure

Where to look when you're fixing something:

| What you're changing | Where |
|----------------------|-------|
| Pipeline CLI, sync, stats | `skills/outreachmagic/scripts/pipeline.py` |
| Email verification, bounce handling | `skills/outreachmagic/scripts/bounces.py` |
| Lead sync back to CRMs | `skills/outreachmagic/scripts/lead_sync.py` |
| Email waterfall finder | `skills/email-finder/scripts/email_finder.py` |
| Lead enrichment via Serper | `skills/lead-enrich/scripts/enrich.py` |
| Shared companion code | `skills/email-finder/scripts/companion_common.py` |
| Install script | `install.sh` |
| CI workflows | `.github/workflows/` |

## Pull request workflow

1. One logical change per PR. Don't bundle a bug fix with a refactor.
2. Run `make release-check` before pushing. This regenerates manifests and runs the full gate.
3. If your change touches companion code, run `bash scripts/sync-companion-common.sh --check` to make sure the shared module is in sync.
4. Write commit messages that explain why, not what. The diff shows what changed.
5. Tests pass, SkillScan passes, manifest check passes.

## Code style

- **Python:** Black for formatting, isort for imports. Column width 120. Type hints on new functions.
- **Shell:** ShellCheck. No bashisms in scripts that are sourced by POSIX shells.
- **Markdown:** Prettier. Sentence-per-line in docs when it improves diffs.
- **No secrets:** Must-load from environment variables or the portal. Never hardcoded.

## Companion skills

Email finder and lead enrich share code in `companion_common.py`. The companion repos (`outreachmagic/email-finder`, `outreachmagic/lead-enrich`) are read-only mirrors published by CI. If you change companion code:

1. Edit the source file in this repo
2. Run `bash scripts/sync-companion-common.sh`
3. Run `python3 scripts/generate_skill_manifest.py --all`
4. The CI publishes to the mirrors on tag

## Release process

Releases are created via git tags. See [docs/RELEASING.md](docs/RELEASING.md) for the full workflow.

Quick version:

```bash
echo "X.Y.Z" > skills/outreachmagic/scripts/VERSION
python3 -c "import sys; sys.path.insert(0,'skills/outreachmagic/scripts'); import pipeline as om; om.sync_skill_md_version()"
python3 scripts/sync_install_docs.py
make release-check
git add -A && git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

CI creates the GitHub Release with install assets.

## Getting help

Open a [GitHub issue](https://github.com/outreachmagic/outreachmagic/issues). Use the bug report or feature request template. For questions, use [GitHub Discussions](https://github.com/outreachmagic/outreachmagic/discussions).
