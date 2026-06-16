# Agent install guide — Outreach Magic skill suite

Agent-readable install guide for **outreachmagic**, **lead-enrich**, and **email-finder**.
Human setup portal: https://app.outreachmagic.io/onboarding

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

## Agent behavior before running the installer

Before executing any install command:

1. Show the user the **exact full command** you plan to run (`--platform` and optional `--tag`).
2. Ask for **explicit confirmation**. Do not run `install.sh` without user approval of the full command string.
3. Prefer **download → inspect → run** (below), not piping a remote script directly into `bash`.

The installer always installs the **full suite** (outreachmagic + lead-enrich + email-finder).

After download (Step 1 below), preview without writing:

```bash
bash "${INSTALL_DIR}/install.sh" --dry-run --platform <PLATFORM> --tag "${OM_VERSION}"
```

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

Pin a **release tag** (recommended). Check latest: `pipeline.py update --check` or GitHub releases.

```bash
OM_VERSION=v1.38.8
INSTALL_DIR=$(mktemp -d)

# Step 1 — download (does not execute)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/install.sh" \
  -o "${INSTALL_DIR}/install.sh"

# Step 2 — verify integrity (recommended)
curl -fsSL "https://github.com/outreachmagic/outreachmagic/releases/download/${OM_VERSION}/SHA256SUMS" \
  -o "${INSTALL_DIR}/SHA256SUMS"
grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)

# Step 3 — optional: inspect before running
less "${INSTALL_DIR}/install.sh"

# Step 4 — run from local copy (--yes required for non-interactive / agent installs)
bash "${INSTALL_DIR}/install.sh" --platform <PLATFORM> --tag "${OM_VERSION}" --yes
```

**Read-only platform detection** (no install side effects):

```bash
python3 <SKILLS>/outreachmagic/scripts/detect_platform.py
# → {"platform": "cursor", "skills_dir": "~/.cursor/skills"}
```

The installer clones all three skills, initializes SQLite at
`<SKILLS>/outreachmagic/databases/outreachmagic.db`, and on Hermes symlinks skills into
each profile under `~/.hermes/profiles/`.

## Windows

1. PowerShell (Admin): `wsl --install` → restart.
2. In Ubuntu (WSL), run the Linux prerequisites and install commands above.
3. Run your agent from the WSL terminal so it can read `~/.claude/skills/`, etc.

## Post-install: connect the account (all platforms)

After install completes, ask the user:

> Would you like me to run the login step for you now? It'll open a browser window where you can sign into your Outreach Magic account.

Once they confirm:

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py login
```

Wait for the user to complete sign-in in the browser, then verify with:

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py whoami --json
```

Never paste secrets into chat.

**CI / automation:** after one interactive `login`, set `OUTREACHMAGIC_AGENT_KEY` in CI secrets. Never commit the key.

## After login: configure providers and connect sequencers

Once signed in at https://app.outreachmagic.io, open **Settings** to:

- Connect sequencer tools (Smartlead, Instantly, Heyreach, PlusVibe, EmailBison, Prosp, MasterInbox, Calendly)
- Enable email-finder providers (TryKitt, Icypeas) and set API keys
- Enable lead research (Serper) and set your API key
- Optionally add MillionVerifier for bulk email re-verification

API keys are stored in the portal and synced locally on `pipeline.py sync-secrets` (writes
`<SKILLS>/outreachmagic/config/agent_secrets.env`). **Do not** ask users for raw API keys in chat or set shell env vars for interactive installs.

Verify keys:

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py sync-secrets --check
python3 <SKILLS>/email-finder/scripts/email_finder.py config
# *_api_key_source should be "agent_secrets"
```

## Common agent intents

| User says | Correct command |
|-----------|-----------------|
| "export leads to Google Sheets" | `pipeline.py sheets export --workspace W` (see share email below) |
| "export leads to Google Sheets for [client]" | `pipeline.py sheets export --workspace W --title "…"` |
| "export and share with [email]" | `pipeline.py sheets export --share-email addr` |
| "share failed / test email" | `pipeline.py sheets export --anyone-with-link` (unlisted URL can edit) |
| "refresh an existing sheet" | `pipeline.py sheets export --workspace W --sheet-id SHEET_ID` |
| "export only leads with email" | `pipeline.py sheets export --require-domain` |
| "export leads that haven't been contacted" | `pipeline.py sheets export --never-contacted` |
| "import a CSV of leads" | `import-profiles` then `sync` (auto-sync runs by default) |

**Sheets export — share email, then `--anyone-with-link` fallback:**

1. Resolve share email from OM identity; confirm with the user if it looks like a test address (`+` alias or internal domain).
2. Try `--share-email`. If Google rejects delivery (`share_email_undeliverable`), retry with `--anyone-with-link` and warn that anyone with the URL can edit.
3. Re-export to the same URL with `--sheet-id` (from prior export JSON) instead of creating a new sheet.
4. Never silently fall back to local CSV.

```bash
SHARE_EMAIL=$(python3 <SKILLS>/outreachmagic/scripts/pipeline.py whoami --json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['share_email'])")

python3 <SKILLS>/outreachmagic/scripts/pipeline.py sheets export \
  --workspace <WORKSPACE> \
  --title "Lead Export — YYYY-MM-DD" \
  --share-email "$SHARE_EMAIL" \
  --detail full

# If share fails — anyone with link can edit (no email delivery):
python3 <SKILLS>/outreachmagic/scripts/pipeline.py sheets export \
  --workspace <WORKSPACE> --title "Lead Export" --anyone-with-link --detail full
```

Use `pipeline.py sheets export` — **not** `review export` (dedup workflow), `gspread`, browser automation, or manual CSV for Google Sheets.

## After import-profiles

Import marks leads `cloud_pending` until pushed to the relay. Auto-sync runs after import unless `--no-sync`:

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py import-profiles --file leads.csv --workspace W
# sync runs automatically; or explicitly:
python3 <SKILLS>/outreachmagic/scripts/pipeline.py sync
```

## Hermes after install

```bash
hermes -s outreachmagic
```

**Profiles:** real files live under `~/.hermes/skills/`. Each profile symlinks into that tree
(`../../../skills/outreachmagic`). Re-run install to link a new profile:

```bash
bash "${INSTALL_DIR}/install.sh" --platform hermes --profile <name>
```

## Verify

```bash
ls <SKILLS>/outreachmagic/SKILL.md
python3 <SKILLS>/outreachmagic/scripts/pipeline.py paths
python3 <SKILLS>/outreachmagic/scripts/pipeline.py version
```

## Updates (after install)

Ongoing skill updates use **GitHub releases** (not the moving `main` branch):

```bash
python3 <SKILLS>/outreachmagic/scripts/pipeline.py update
python3 <SKILLS>/outreachmagic/scripts/pipeline.py update --check
```

Pin: `pipeline.py update --tag vX.Y.Z`. Power users: `pipeline.py update --channel main`.

Rollback after a bad update: `pipeline.py rollback` (restores scripts snapshot from before the last update).

Re-run the install script only for a full reinstall (broken layout, new platform overlay).

## Uninstall

```bash
bash "${INSTALL_DIR}/install.sh" --uninstall --dry-run --platform <PLATFORM>
bash "${INSTALL_DIR}/install.sh" --uninstall --platform <PLATFORM>
```

## Stop on errors

If any step fails, show the user the full command output and do not continue silently.
