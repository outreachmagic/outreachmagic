# Install & update — website copy (before / after)

Use this page to update [app.outreachmagic.io/setup/agent](https://app.outreachmagic.io/setup/agent) and any docs that still reference the three platform-specific repos.

**What changed:** One public repo ([outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic)) replaces `hermes-outreachmagic`, `cursor-outreachmagic`, and `claude-code-outreachmagic`. Install uses `install.sh --platform <name>`. Update command is unchanged for users (`pipeline.py update`); downloads now come from the unified repo.

**Install URL:** Use `main` (no release tag) so users always get the latest `install.sh` and skill files from the default branch:

```text
https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh
```

Omit `--tag`, `--lead-enrich-tag`, and `--email-finder-tag` on the install command for the same reason — each repo’s latest `main` is cloned.

**Updates after install:** `pipeline.py update` (no flags) pulls the latest **GitHub Release** on `outreachmagic/outreachmagic`. That is separate from install: install tracks `main`; update tracks releases.

---

## Optional companion skills

**outreachmagic alone is enough** for pipeline tracking, relay sync, and lead management. These two companions are optional add-ons:

| Skill | What it does | Requires |
|-------|----------------|----------|
| **lead-enrich** | Researches a person via Serper (Google Search): company domain, website, LinkedIn URL, job title, etc. Checks your local Outreach Magic DB first so you do not burn Serper credits on leads you already have. Saves enriched fields back into the pipeline. | `SERPER_API_KEY` ([serper.dev](https://serper.dev)) |
| **email-finder** | Finds work emails (trykitt → Icypeas). Checks Outreach Magic first. Saves email + verification to the pipeline. Optional MillionVerifier bulk verify. | `TRYKITT_API_KEY` and/or `ICYPEAS_API_KEY` ([trykitt.ai](https://trykitt.ai), [Icypeas](https://app.icypeas.com)) |

Add `--with-lead-enrich` and/or `--with-email-finder` to the install command when you want them. `--with-email-finder` implies `--with-lead-enrich`.

---

## Hermes

### Before

```bash
git clone https://github.com/outreachmagic/hermes-outreachmagic.git /tmp/om-hermes && \
  cp -r /tmp/om-hermes/{SKILL.md,scripts,references} ~/.hermes/skills/outreachmagic/ && \
  rm -rf /tmp/om-hermes
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

Optional one-liner (older curl install):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/main/install.sh | bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
```

### After — outreachmagic only

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes --migrate
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

### After — with optional companions

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform hermes --migrate --with-lead-enrich --with-email-finder
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login
hermes -s outreachmagic
```

**Notes for Hermes page:**
- Real files still live under `~/.hermes/skills/outreachmagic/`
- Profiles still use symlinks only (`--migrate` fixes old full copies)
- `hermes -s outreachmagic` unchanged after install

---

## Cursor

### Before

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/main/install.sh | bash
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

### After — outreachmagic only

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform cursor
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

### After — with optional companions

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform cursor --with-lead-enrich --with-email-finder
python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py login
```

**Notes for Cursor page:**
- Skill still installs to `~/.cursor/skills/outreachmagic/`
- Invoke in Agent chat with `/outreachmagic` or ask about your pipeline in plain English
- Optional `.mdc` rule is copied into the skill directory at install

---

## Claude Code

### Before

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/main/install.sh | bash
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

### After — outreachmagic only

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform claude
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

### After — with optional companions

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh | bash -s -- \
  --platform claude --with-lead-enrich --with-email-finder
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py login
```

**Notes for Claude page:**
- Skill still installs to `~/.claude/skills/outreachmagic/`
- `SKILL.md` is the source of truth (legacy `CLAUDE_SNIPPET.md` is optional)

---

## Updating (all platforms)

### Before

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
# or ~/.cursor/skills/... / ~/.claude/skills/... on other platforms
```

Downloads came from the platform-specific repo (`hermes-outreachmagic`, `cursor-outreachmagic`, or `claude-code-outreachmagic`).

### After

```bash
python3 <skill-path>/scripts/pipeline.py update
```

Same command and install path per platform. Downloads the latest **release** from **outreachmagic/outreachmagic**.

| Platform | Install path | Update command |
|----------|--------------|----------------|
| Hermes | `~/.hermes/skills/outreachmagic/` | `python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update` |
| Cursor | `~/.cursor/skills/outreachmagic/` | `python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update` |
| Claude Code | `~/.claude/skills/outreachmagic/` | `python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update` |

Check for updates without installing:

```bash
python3 <skill-path>/scripts/pipeline.py update --check
```

Pin a specific release (optional):

```bash
python3 <skill-path>/scripts/pipeline.py update --tag v1.21.0
```

---

## Pinning a version (optional)

If you ever need a fixed release instead of latest `main` at install time:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform hermes --migrate --tag v1.21.0
```

Most website copy should use **`main` with no tags** so install always tracks the latest.

---

## Repo URLs to retire on the website

| Old | New |
|-----|-----|
| `github.com/outreachmagic/hermes-outreachmagic` | `github.com/outreachmagic/outreachmagic` |
| `github.com/outreachmagic/cursor-outreachmagic` | *(same)* |
| `github.com/outreachmagic/claude-code-outreachmagic` | *(same)* |
| `raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/...` | `raw.githubusercontent.com/outreachmagic/outreachmagic/main/...` |
| `raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/...` | *(same)* |
| `raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/...` | *(same)* |

---

## One-line summary for marketing copy

> **One repo, every platform.** Install with `install.sh --platform hermes|cursor|claude` from [github.com/outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic). Same local paths, same `pipeline.py update`. Add `--with-lead-enrich` / `--with-email-finder` only if you want research and email-finding companions.
