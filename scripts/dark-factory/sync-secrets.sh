#!/usr/bin/env bash
# Pull dashboard API keys into dark-factory instance before tests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_JSON="${ROOT}/test-config.local.json"
[[ -f "$CONFIG_JSON" ]] || CONFIG_JSON="${ROOT}/test-config.example.json"

read_cfg() {
  python3 - "$CONFIG_JSON" "$1" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
key = sys.argv[2].split(".")
v = cfg
for k in key:
    v = v[k]
print(v)
PY
}

SSH_HOST="$(read_cfg vps.ssh_host)"
SSH_KEY="$(read_cfg vps.ssh_key)"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
INSTANCE="$(read_cfg vps.instance)"

echo "== Syncing dashboard API keys on ${SSH_HOST} (${INSTANCE}) =="
ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" bash -s "$INSTANCE" <<'REMOTE'
set -euo pipefail
INSTANCE="$1"
DATA="$HOME/hermes/instances/${INSTANCE}/data"
PIPE="$DATA/skills/outreachmagic/scripts/pipeline.py"
set -a
# shellcheck disable=SC1090
source "$DATA/.env"
set +a
python3 "$PIPE" sync-secrets --json
python3 "$PIPE" sync-secrets --check --json
REMOTE
