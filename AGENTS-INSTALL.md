# Agent install guide — Outreach Magic skill suite

Agent-readable install guide for **outreachmagic**, **lead-enrich**, and **email-finder**.
Human setup portal: https://app.outreachmagic.io/setup/agent

## Ask the user first

Before installing, confirm:

- **Platform:** Claude Code / Cursor / Hermes
- **OS:** macOS / Linux / Windows (Windows → WSL; see below)

| Platform    | `--platform` flag | Skills directory        |
|-------------|-------------------|-------------------------|
| Claude Code | `claude`          | `~/.claude/skills/`     |
| Cursor      | `cursor`          | `~/.cursor/skills/`     |
| Hermes      | `hermes`          | `~/.hermes/skills/`     |

Replace `<PLATFORM>` below with that flag. Replace `<SKILLS>` with the matching path
(e.g. `~/.cursor/skills`).

## Prerequisites

Install **Python 3** and **Git** only. Outreach Magic stores data in a **local SQLite**
database (`pipeline.py init` — run automatically by the installer).

**macOS:**

```bash
brew install python3 git
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get update && sudo apt-get install -y python3 git
```

**Verify:**

```bash
python3 --version
git --version
```

## Install (macOS / Linux)

**Default — full suite** (outreachmagic + lead-enrich + email-finder):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh \
  | bash -s -- --platform <PLATFORM> --with-lead-enrich --with-email-finder --migrate
```

**Outreach Magic only** (pipeline / relay / SQLite — no Serper or email find):

```bash
curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreachmagic/main/install.sh \
  | bash -s -- --platform <PLATFORM> --migrate
```

On Hermes, omit `--migrate` only if you have no existing Hermes profiles to fix.

The installer clones skills, runs `pipeline.py init`, and on Hermes links profile
symlinks (never full copies under `profiles/`).

## Windows

1. PowerShell (Admin): `wsl --install` → restart.
2. In Ubuntu (WSL), run the Linux prerequisites and install commands above.
3. Run your agent from the WSL terminal so it can read `~/.claude/skills/`, etc.

## Connect (Outreach Magic)

Run **in the user's terminal**, not in chat. Do not paste secrets into chat.

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py login
```

Browser opens for sign-in / device authorization. Key setup:
https://app.outreachmagic.io/setup/agent

**CI / automation:** after one interactive `login`, you may set `OUTREACHMAGIC_AGENT_KEY`
in CI secrets (overrides config). Never commit the key.

## Hermes after install

```bash
hermes -s outreachmagic
```

**Profiles:** real files live under `~/.hermes/skills/outreachmagic/`. Each profile
symlinks into that tree (`../../../skills/outreachmagic`). If a profile has a full
copy instead of a symlink, re-run install with `--migrate` or:

```bash
bash install.sh --platform hermes --migrate --all-profiles
```

## Third-party API keys (companions)

Add to `~/.bashrc`, `~/.zshrc`, or your agent environment (not chat):

```bash
export SERPER_API_KEY=""          # lead-enrich (Google Search)
export TRYKITT_API_KEY=""         # email-finder primary
export ICYPEAS_API_KEY=""         # email-finder waterfall fallback
export MILLIONVERIFIER_API_KEY="" # email-finder verify (optional)
```

| Key                       | Skill         | Required?                         |
|---------------------------|---------------|-----------------------------------|
| Outreach Magic (login)    | outreachmagic | Yes — `pipeline.py login`         |
| `SERPER_API_KEY`          | lead-enrich   | Yes, if using lead-enrich         |
| `TRYKITT_API_KEY`         | email-finder  | One of trykitt / icypeas for find |
| `ICYPEAS_API_KEY`         | email-finder  | One of trykitt / icypeas for find |
| `MILLIONVERIFIER_API_KEY` | email-finder  | Optional — verify commands only   |

Get keys:

- Outreach Magic: https://app.outreachmagic.io/setup/agent
- Serper: https://serper.dev
- TryKitt: https://trykitt.ai
- Icypeas: https://icypeas.com
- Million Verifier: https://millionverifier.com

## Verify

```bash
ls <SKILLS>/outreachmagic/SKILL.md
# If full suite:
ls <SKILLS>/lead-enrich/SKILL.md
ls <SKILLS>/email-finder/SKILL.md

python3 <SKILLS>/outreachmagic/scripts/pipeline.py paths
python3 <SKILLS>/outreachmagic/scripts/pipeline.py version
```

## Updates (after install)

Install tracks `main` on the public repo. Ongoing skill updates use releases:

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py update
python3 <SKILLS>/outreachmagic/scripts/pipeline.py update --check
```

Pin a release: `pipeline.py update --tag vX.Y.Z`

Re-run `curl | bash` only for a full reinstall (e.g. broken layout or new platform overlay).

Companion updates: `enrich.py update` / `email_finder.py update` in each skill's `scripts/`.

## Stop on errors

If any step fails, show the user the full command output and do not continue silently.
