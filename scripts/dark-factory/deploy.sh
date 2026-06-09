#!/usr/bin/env bash
# Rsync monorepo skills + dark-factory tests to the VPS dark-factory instance.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_JSON="${ROOT}/test-config.local.json"
if [[ ! -f "$CONFIG_JSON" ]]; then
  CONFIG_JSON="${ROOT}/test-config.example.json"
fi

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
SKILLS_PATH="$(read_cfg vps.skills_path)"
TESTS_DATA_PATH="$(python3 - "$CONFIG_JSON" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
vps = cfg["vps"]
print(vps.get("tests_data_path") or vps["skills_path"].replace("/skills", "/dark-factory-tests"))
PY
)"

REMOTE="${SSH_HOST}"
RSYNC_SSH="ssh -i ${SSH_KEY} -o BatchMode=yes"
skills_remote_rsync="${SKILLS_PATH}"
tests_remote_rsync="${TESTS_DATA_PATH}"

echo "== Dark factory deploy → ${REMOTE} =="

RSYNC_EXCLUDES=(
  --exclude 'databases/'
  --exclude 'config/'
  --exclude 'export/'
  --exclude '.DS_Store'
  --exclude '__pycache__/'
  --exclude '*.pyc'
)

for skill in outreachmagic lead-enrich email-finder; do
  echo "  skills/${skill}"
  rsync -az --delete "${RSYNC_EXCLUDES[@]}" -e "$RSYNC_SSH" \
    "${ROOT}/skills/${skill}/" \
    "${REMOTE}:${skills_remote_rsync}/${skill}/"
done

echo "  test-harness"
rsync -az --delete -e "$RSYNC_SSH" \
  "${ROOT}/tests/dark-factory/harness-hermes/" \
  "${REMOTE}:${skills_remote_rsync}/test-harness/"

echo "  dark-factory-tests → ${tests_remote_rsync}"
rsync -az --chmod=Du=rwx,go=rx,Fu=rw,go=r -e "$RSYNC_SSH" \
  "${ROOT}/tests/dark-factory/catalog.json" \
  "${ROOT}/scripts/dark-factory/run-script-tests.py" \
  "${ROOT}/scripts/dark-factory/validate.py" \
  "${ROOT}/scripts/dark-factory/post-validate.py" \
  "${REMOTE}:${tests_remote_rsync}/"
rsync -az --delete --chmod=Du=rwx,go=rx,Fu=rw,go=r -e "$RSYNC_SSH" \
  "${ROOT}/tests/dark-factory/fixtures/" \
  "${REMOTE}:${tests_remote_rsync}/fixtures/"

ssh -i "$SSH_KEY" -o BatchMode=yes "$REMOTE" bash -s "$SKILLS_PATH" "$TESTS_DATA_PATH" <<'REMOTE'
set -euo pipefail
SKILLS_PATH="$1"
TESTS_PATH="$2"
chmod +x "$SKILLS_PATH"/*/scripts/*.py 2>/dev/null || true
mkdir -p "$TESTS_PATH/results"
# Agent container may create this tree as uid hermes; keep deploy user able to rsync.
if [[ -d "$TESTS_PATH" ]]; then
  chown -R "$(whoami):$(whoami)" "$TESTS_PATH" 2>/dev/null || sudo chown -R "$(whoami):$(whoami)" "$TESTS_PATH"
fi
REMOTE

echo "  deploy complete"
