# Install Outreach Magic

## Hermes Agent (recommended)

Install from GitHub (Hermes downloads the skill into `~/.hermes/skills/outreachmagic/`):

```bash
hermes skills inspect outreachmagic/outreachmagic-skill/skills/outreachmagic
hermes skills install outreachmagic/outreachmagic-skill/skills/outreachmagic
hermes -s outreachmagic
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py init
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py show
```

If the security scan flags env-var usage in `pipeline.py`, retry with `--force` after you have reviewed [SECURITY.md](../SECURITY.md):

```bash
hermes skills install outreachmagic/outreachmagic-skill/skills/outreachmagic --force
```

### Troubleshooting `hermes skills install`

| Error | Fix |
|-------|-----|
| **GitHub API rate limit** | Authenticate: `gh auth login` **or** add `GITHUB_TOKEN=ghp_...` to `~/.hermes/.env`, then retry install |
| Security scan blocked (env / exfiltration warnings) | Push latest `outreachmagic-skill` (no `os.environ` in scripts); retry install. Use `--force` only if still blocked on an old commit. |
| **Skill folder missing after install** | Install did not complete — use [local sync](#local-install-no-github-api) below |

Hermes hub installs need GitHub API access (60 req/hr unauthenticated, 5000/hr with a token).

## Local install (no GitHub API)

Use this on a new machine, when rate-limited, or while developing from a clone. **No `hermes skills install` required.**

```bash
git clone https://github.com/outreachmagic/outreachmagic-skill.git
cd outreachmagic-skill
bash scripts/sync-local.sh
hermes -s outreachmagic
```

If you already have the repo locally (e.g. `~/Developer/hermes-agent`):

```bash
cd ~/Developer/hermes-agent   # or your clone path
bash scripts/sync-local.sh
hermes -s outreachmagic
```

`sync-local.sh` copies `skills/outreachmagic/` → `~/.hermes/skills/outreachmagic/` and runs `pipeline.py init`.

Verify:

```bash
ls ~/.hermes/skills/outreachmagic/
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py version
```

## Cursor / Claude Code / Codex CLI

Copy `skills/outreachmagic/` into your agent's skills folder:

- **Cursor:** `~/.cursor/skills/outreachmagic/` or `.cursor/skills/outreachmagic/`
- **Claude Code:** `~/.claude/skills/outreachmagic/`

Set `HERMES_HOME=~/.claude` if you want the database next to the skill instead of under `~/.hermes`:

Or set in config after first `init`:

```json
{ "data_root": "/Users/you/.claude" }
```

```bash
export HERMES_HOME=~/.claude
python3 ~/.claude/skills/outreachmagic/scripts/pipeline.py init
```

## Connect sequencer relay (optional, paid)

Get a token at [dev.outreachmagic.io](https://dev.outreachmagic.io).

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py connect --key YOUR_TOKEN
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py pull
```

## Updates

```bash
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update --check
python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py update
```

Or reinstall from hub after `gh auth login` / `GITHUB_TOKEN` is set: `hermes skills update`

See [SECURITY.md](../SECURITY.md) and [SKILL_REGISTRY_PLAN.md](./SKILL_REGISTRY_PLAN.md).
