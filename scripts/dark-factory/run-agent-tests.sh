#!/usr/bin/env bash
# Layer 3: Hermes agent catalog tests via docker exec on VPS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_JSON="${ROOT}/test-config.local.json"
[[ -f "$CONFIG_JSON" ]] || CONFIG_JSON="${ROOT}/test-config.example.json"

SKILLS_FILTER="${SKILLS_FILTER:-}"
TAGS_FILTER="${TAGS_FILTER:-}"
IDS_FILTER="${IDS_FILTER:-}"
EXCLUDE_FILTER="${EXCLUDE_FILTER:-}"
TIMESTAMP="${TIMESTAMP:-$(date -u +%Y%m%d-%H%M%S)}"

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

tests_data_path() {
  python3 - "$CONFIG_JSON" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
vps = cfg["vps"]
print(vps.get("tests_data_path") or vps["skills_path"].replace("/skills", "/dark-factory-tests"))
PY
}

SSH_HOST="$(read_cfg vps.ssh_host)"
SSH_KEY="$(read_cfg vps.ssh_key)"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
PROFILE="$(read_cfg vps.profile)"
CONTAINER="$(read_cfg vps.agent_container)"
TESTS_DATA_PATH="$(tests_data_path)"
TIMEOUT="$(read_cfg timeout_seconds)"

# Agent container mounts instance data at /home/hermes/.hermes
CATALOG_CONTAINER="/home/hermes/.hermes/dark-factory-tests/catalog.json"
RESULTS_CONTAINER="/home/hermes/.hermes/dark-factory-tests/results/hermes-${TIMESTAMP}.json"
RESULTS_HOST="${TESTS_DATA_PATH}/results/hermes-${TIMESTAMP}.json"
FILTER_DESC="skills=${SKILLS_FILTER:-all} tags=${TAGS_FILTER:-all}"

PROMPT_FILE="$(mktemp -t df-prompt.XXXXXX)"
trap 'rm -f "$PROMPT_FILE"' EXIT
cat >"$PROMPT_FILE" <<EOF
You are in TEST MODE. Follow the test-harness skill exactly.

Read catalog JSON at: ${CATALOG_CONTAINER}

Run ONLY test cases where mode is "agent". Do not invent tests.
Apply filters: ${FILTER_DESC}
$([[ -n "$IDS_FILTER" ]] && echo "Only IDs: $IDS_FILTER")
$([[ -n "$EXCLUDE_FILTER" ]] && echo "Exclude IDs: $EXCLUDE_FILTER")

For each matching case (use catalog id, prompt, expect fields):
  1. TEST [id]: Running...
  2. Execute the prompt exactly as written in the catalog
  3. Validate against every field in expect
  4. TEST [id]: PASS or TEST [id]: FAIL — reason

Write results JSON to: ${RESULTS_CONTAINER}
Format: {"environment":"hermes","timestamp":"ISO-8601","passed":N,"failed":M,"results":[{"id","status","prompt","actual","reason"},...]}

Final line: PASS: N / FAIL: M
EOF

REMOTE_PROMPT="${TESTS_DATA_PATH}/prompt-${TIMESTAMP}.txt"

echo "== Layer 3 Hermes agent tests (${TIMESTAMP}) =="

ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" "mkdir -p ${TESTS_DATA_PATH}/results"
scp -i "$SSH_KEY" -o BatchMode=yes "$PROMPT_FILE" "${SSH_HOST}:${REMOTE_PROMPT}"

ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" bash -s \
  "$CONTAINER" "$PROFILE" "$REMOTE_PROMPT" "$RESULTS_HOST" "$RESULTS_CONTAINER" "$TIMEOUT" "$(read_cfg vps.instance)" <<'REMOTE'
set -euo pipefail
CONTAINER="$1"
PROFILE="$2"
PROMPT_HOST="$3"
RESULTS_HOST="$4"
RESULTS_CONTAINER="$5"
TIMEOUT="$6"
INSTANCE="$7"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "ERROR: container $CONTAINER not running." >&2
  exit 1
fi

DATA_ENV="/home/deploy/hermes/instances/${INSTANCE}/data/.env"
AGENT_SECRETS="/home/deploy/hermes/instances/${INSTANCE}/data/skills/outreachmagic/config/agent_secrets.env"
if [[ -f "$DATA_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$DATA_ENV"
  set +a
fi
if [[ -f "$AGENT_SECRETS" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$AGENT_SECRETS"
  set +a
fi

PROMPT="$(cat "$PROMPT_HOST")"
mkdir -p "$(dirname "$RESULTS_HOST")"
docker exec "$CONTAINER" mkdir -p "$(dirname "$RESULTS_CONTAINER")"

# Source synced companion keys inside the container so Hermes agent shell sees them.
docker exec \
  -e HERMES_PROFILE="$PROFILE" \
  -e OUTREACHMAGIC_AGENT_KEY="${OUTREACHMAGIC_AGENT_KEY:-}" \
  -e TRYKITT_API_KEY="${TRYKITT_API_KEY:-}" \
  -e ICYPEAS_API_KEY="${ICYPEAS_API_KEY:-}" \
  -e SERPER_API_KEY="${SERPER_API_KEY:-}" \
  -e MILLIONVERIFIER_API_KEY="${MILLIONVERIFIER_API_KEY:-}" \
  "$CONTAINER" \
  bash -lc 'set -a; [ -f /home/hermes/.hermes/skills/outreachmagic/config/agent_secrets.env ] && . /home/hermes/.hermes/skills/outreachmagic/config/agent_secrets.env; [ -f /home/hermes/.hermes/.env ] && . /home/hermes/.hermes/.env; set +a; exec timeout '"$TIMEOUT"' /opt/hermes/bin/hermes -z '"$(printf '%q' "$PROMPT")"' --skills test-harness,outreachmagic,lead-enrich,email-finder --yolo' \
  2>&1 | tee "/tmp/dark-factory-hermes-${PROFILE}.log"

if [[ -f "$RESULTS_HOST" ]]; then
  echo "Results (host): $RESULTS_HOST"
elif docker exec "$CONTAINER" test -f "$RESULTS_CONTAINER"; then
  docker cp "${CONTAINER}:${RESULTS_CONTAINER}" "$RESULTS_HOST"
  echo "Results (docker cp): $RESULTS_HOST"
else
  echo "WARNING: no results at $RESULTS_HOST or $RESULTS_CONTAINER" >&2
  exit 1
fi
REMOTE

mkdir -p "${ROOT}/tests/dark-factory/results"
scp -i "$SSH_KEY" -o BatchMode=yes \
  "${SSH_HOST}:${RESULTS_HOST}" \
  "${ROOT}/tests/dark-factory/results/hermes-${TIMESTAMP}.json"

# Deterministic re-validation against catalog
POST_RC=0
ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" \
  "python3 ${TESTS_DATA_PATH}/post-validate.py \
    --catalog ${TESTS_DATA_PATH}/catalog.json \
    --results ${RESULTS_HOST} \
    --output ${RESULTS_HOST}" || POST_RC=$?

scp -i "$SSH_KEY" -o BatchMode=yes \
  "${SSH_HOST}:${RESULTS_HOST}" \
  "${ROOT}/tests/dark-factory/results/hermes-${TIMESTAMP}.json"

echo "  local copy: tests/dark-factory/results/hermes-${TIMESTAMP}.json"
exit "$POST_RC"
