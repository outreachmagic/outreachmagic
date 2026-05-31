# Install & update — website copy (before / after)

Use this page to update [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent) and any docs that still reference the three platform-specific repos.

**What changed:** One public repo ([outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic)) replaces `hermes-outreachmagic`, `cursor-outreachmagic`, and `claude-code-outreachmagic`. Install uses `install.sh --platform <name>`. Update command is unchanged for users (`pipeline.py update`); downloads now come from the unified repo.

Replace `v1.21.0` with your latest release tag when publishing.

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

### After

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform hermes \
  --with-lead-enrich --with-email-finder --migrate \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
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

### After

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform cursor \
  --with-lead-enrich --with-email-finder \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
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

### After

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform claude \
  --with-lead-enrich --with-email-finder \
  --tag v1.21.0 \
  --lead-enrich-tag v2.0.2 \
  --email-finder-tag v1.0.2
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

Same command and install path per platform. Downloads now come from **outreachmagic/outreachmagic** (`skills/outreachmagic/` layout).

| Platform | Install path | Update command |
|----------|--------------|----------------|
| Hermes | `~/.hermes/skills/outreachmagic/` | `python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update` |
| Cursor | `~/.cursor/skills/outreachmagic/` | `python3 ~/.cursor/skills/outreachmagic/scripts/pipeline.py update` |
| Claude Code | `~/.claude/skills/outreachmagic/` | `python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py update` |

Check for updates without installing:

```bash
python3 <skill-path>/scripts/pipeline.py update --check
```

Install a specific version:

```bash
python3 <skill-path>/scripts/pipeline.py update --tag v1.21.0
```

**Existing installs:** Users on the old platform repos can still run `update` (fallback to legacy repos) until they reinstall from `outreachmagic/outreachmagic`.

---

## Minimal install (no companion skills)

Drop `--with-lead-enrich`, `--with-email-finder`, and companion tag flags if the page only covers outreachmagic:

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/v1.21.0/install.sh | bash -s -- \
  --platform hermes \
  --tag v1.21.0
```

Use `--platform cursor` or `--platform claude` as needed.

---

## Repo URLs to retire on the website

| Old | New |
|-----|-----|
| `github.com/outreachmagic/hermes-outreachmagic` | `github.com/outreachmagic/outreachmagic` |
| `github.com/outreachmagic/cursor-outreachmagic` | *(same)* |
| `github.com/outreachmagic/claude-code-outreachmagic` | *(same)* |
| `raw.githubusercontent.com/outreachmagic/hermes-outreachmagic/...` | `raw.githubusercontent.com/outreachmagic/outreachmagic/...` |
| `raw.githubusercontent.com/outreachmagic/cursor-outreachmagic/...` | *(same)* |
| `raw.githubusercontent.com/outreachmagic/claude-code-outreachmagic/...` | *(same)* |

---

## One-line summary for marketing copy

> **One repo, every platform.** Install with `install.sh --platform hermes|cursor|claude` from [github.com/outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic). Same local paths, same `pipeline.py update`.
