# Dark factory setup

Layer 3 agent tests run on an isolated **`dark-factory`** Hermes Docker instance on the Contabo VPS — not on `magic` or `jonathan`.

## Prerequisites

- SSH access: `deploy@84.247.188.189` (alias `hermes-vps` recommended)
- `hermes-vps` repo cloned at `~/Developer/hermes-vps`
- Test OM account: **dark-factor@outreachmagic.io**

## One-time VPS provision

Full steps: [hermes-vps/deploy/runbooks/dark-factory-provision.md](https://github.com/magic-creators/hermes-vps/blob/main/deploy/runbooks/dark-factory-provision.md)

Summary:

1. Cloudflare A record: `dark-factory.agent` → `84.247.188.189`
2. `./deploy/scripts/vps.sh provision dark-factory '<webui-password>'`
3. Copy `deploy/instance-env/dark-factory.env.example` → `dark-factory.env`, fill keys
4. `./deploy/scripts/vps.sh env-push dark-factory --restart`
5. `./deploy/scripts/vps.sh profiles-apply dark-factory`
6. Install `email-finder` on instance (see runbook)
7. `pipeline.py login` as dark-factor@ → copy `OUTREACHMAGIC_AGENT_KEY` → `env-push` again

## Local config

```bash
cp test-config.example.json test-config.local.json
# optional: set ssh_host to hermes-vps
```

## Run tests

```bash
bash scripts/dark-factory/run.sh --layer 3 --tags smoke
bash scripts/dark-factory/run.sh --release email_finder
bash scripts/dark-factory/run.sh --layer 2 --release dedup
```

Each run **syncs dashboard API keys** (`sync-secrets`) then deploys skills to `data/dark-factory-tests/` (agent-visible at `/home/hermes/.hermes/dark-factory-tests/` inside the container).

Instance **starts at the beginning** and **stops when idle** (unless `--no-stop`).

## SSH alias (recommended)

Add to `~/.ssh/config`:

```
Host hermes-vps
  HostName 84.247.188.189
  User deploy
  IdentityFile ~/.ssh/id_ed25519
```

## Cursor Layer 3 (manual)

When Cursor overlay (`.mdc`) changes, run agent-mode catalog cases manually in Cursor using `tests/dark-factory/harness-cursor/rules.md`.
