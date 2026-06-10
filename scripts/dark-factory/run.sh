#!/usr/bin/env bash
# Dark factory orchestrator: start VPS instance → deploy → test → report → stop.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAYER="all"
SKILLS=""
TAGS=""
IDS=""
EXCLUDE=""
RELEASE=""
NO_STOP=0
SKIP_DEPLOY=0
DEPLOY_ONLY=0

usage() {
  cat <<EOF
Usage: bash scripts/dark-factory/run.sh [options]

Options:
  --layer 2|3|all          Script tests (2), agent tests (3), or both (default: all)
  --skills a,b             Filter catalog skills
  --tags a,b               Filter catalog tags
  --ids a,b                Filter test IDs
  --exclude a,b            Exclude test IDs
  --release NAME           Use release_filters from config (v_star, lead_enrich, email_finder, companion_common)
  --no-stop                Leave dark-factory instance running after tests
  --skip-deploy            Skip rsync deploy (skills already current)
  --deploy-only            Deploy only, no tests

Examples:
  bash scripts/dark-factory/run.sh --layer 3 --tags smoke
  bash scripts/dark-factory/run.sh --release email_finder
  bash scripts/dark-factory/run.sh --release v_star --layer all
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --layer) LAYER="$2"; shift 2 ;;
    --skills) SKILLS="$2"; shift 2 ;;
    --tags) TAGS="$2"; shift 2 ;;
    --ids) IDS="$2"; shift 2 ;;
    --exclude) EXCLUDE="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --no-stop) NO_STOP=1; shift ;;
    --skip-deploy) SKIP_DEPLOY=1; shift ;;
    --deploy-only) DEPLOY_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

CONFIG_JSON="${ROOT}/test-config.local.json"
if [[ ! -f "$CONFIG_JSON" ]]; then
  if [[ -f "${ROOT}/test-config.example.json" ]]; then
    echo "Note: using test-config.example.json — copy to test-config.local.json to customize"
    CONFIG_JSON="${ROOT}/test-config.example.json"
  else
    echo "ERROR: missing test-config.local.json" >&2
    exit 1
  fi
fi

if [[ -n "$RELEASE" ]]; then
  eval "$(python3 - "$CONFIG_JSON" "$RELEASE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
name = sys.argv[2]
rf = cfg.get("release_filters", {}).get(name)
if not rf:
    raise SystemExit(f"unknown release filter: {name}")
skills = ",".join(rf.get("skills") or [])
tags = ",".join(rf.get("tags") or [])
print(f'SKILLS="{skills}"')
print(f'TAGS="{tags}"')
PY
)"
fi

read_cfg() {
  python3 - "$CONFIG_JSON" "$1" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
key = sys.argv[2].split(".")
v = cfg
for k in key:
    v = v[k]
if isinstance(v, bool):
    print("true" if v else "false")
else:
    print(v)
PY
}

SSH_HOST="$(read_cfg vps.ssh_host)"
SSH_KEY="$(read_cfg vps.ssh_key)"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
INSTANCE="$(read_cfg vps.instance)"
STOP_WHEN_IDLE="$(read_cfg vps.stop_when_idle)"
START_WAIT="$(read_cfg vps.start_wait_seconds)"
SKILLS_PATH="$(read_cfg vps.skills_path)"
WORKSPACE_PATH="$(read_cfg vps.workspace_path)"

HERMES_ENABLED="$(read_cfg environments.hermes.enabled)"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
RESULT_FILES=()
EXIT_CODE=0

ssh_cmd() {
  ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" "$@"
}

start_instance() {
  echo "== Starting instance: ${INSTANCE} =="
  ssh_cmd "cd ~/hermes/instances/${INSTANCE} && docker compose --project-name hermes-${INSTANCE} up -d"
  echo "  waiting ${START_WAIT}s for gateway..."
  sleep "$START_WAIT"
}

stop_instance() {
  if [[ "$NO_STOP" -eq 1 ]]; then
    echo "== Leaving instance running (--no-stop) =="
    return 0
  fi
  if [[ "$STOP_WHEN_IDLE" != "true" ]]; then
    return 0
  fi
  echo "== Stopping instance: ${INSTANCE} =="
  ssh_cmd "cd ~/hermes/instances/${INSTANCE} && docker compose --project-name hermes-${INSTANCE} down" || true
}

if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
  bash "${ROOT}/scripts/dark-factory/deploy.sh"
fi

if [[ "$DEPLOY_ONLY" -eq 1 ]]; then
  echo "Deploy only — done."
  exit 0
fi

# Ensure instance exists before start
if ! ssh_cmd "test -d ~/hermes/instances/${INSTANCE}"; then
  echo "ERROR: instance ${INSTANCE} not found on VPS." >&2
  echo "See hermes-vps/deploy/runbooks/dark-factory-provision.md" >&2
  exit 1
fi

start_instance
trap 'stop_instance' EXIT

bash "${ROOT}/scripts/dark-factory/sync-secrets.sh"

TESTS_DATA_PATH="$(python3 - "$CONFIG_JSON" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
vps = cfg["vps"]
print(vps.get("tests_data_path") or vps["skills_path"].replace("/skills", "/dark-factory-tests"))
PY
)"

export SKILLS_FILTER="$SKILLS"
export TAGS_FILTER="$TAGS"
export IDS_FILTER="$IDS"
export EXCLUDE_FILTER="$EXCLUDE"
export TIMESTAMP

if [[ "$LAYER" == "2" || "$LAYER" == "all" ]]; then
  echo "== Layer 2 pre-check: update manifest =="
  if python3 "${ROOT}/scripts/dark-factory/validate-om-manifest.py"; then
    :
  else
    EXIT_CODE=1
  fi
  echo "== Layer 2: script tests =="
  SCRIPT_RESULT="${TESTS_DATA_PATH}/results/script-${TIMESTAMP}.json"
  ssh_cmd "mkdir -p ${TESTS_DATA_PATH}/results"
  SYNC_ENV="set -a && source ~/hermes/instances/${INSTANCE}/data/.env && set +a"
  if ssh_cmd "${SYNC_ENV} && python3 ${TESTS_DATA_PATH}/run-script-tests.py \
      --catalog ${TESTS_DATA_PATH}/catalog.json \
      --skills-root ${SKILLS_PATH} \
      --skills '${SKILLS}' --tags '${TAGS}' --ids '${IDS}' --exclude '${EXCLUDE}' \
      --environment hermes-script \
      --output ${SCRIPT_RESULT}"; then
    :
  else
    EXIT_CODE=1
  fi
  ssh_cmd "python3 ${TESTS_DATA_PATH}/post-validate.py \
    --catalog ${TESTS_DATA_PATH}/catalog.json \
    --results ${SCRIPT_RESULT} \
    --output ${SCRIPT_RESULT}" || EXIT_CODE=1
  mkdir -p "${ROOT}/tests/dark-factory/results"
  scp -i "$SSH_KEY" -o BatchMode=yes \
    "${SSH_HOST}:${SCRIPT_RESULT}" \
    "${ROOT}/tests/dark-factory/results/script-${TIMESTAMP}.json" 2>/dev/null || EXIT_CODE=1
  RESULT_FILES+=("${ROOT}/tests/dark-factory/results/script-${TIMESTAMP}.json")

  echo "== Layer 2: migrate lock stress =="
  STRESS_RESULT="${TESTS_DATA_PATH}/results/migrate-stress-${TIMESTAMP}.json"
  if ssh_cmd "python3 ${TESTS_DATA_PATH}/migrate-lock-stress.py \
      --data-root ${TESTS_DATA_PATH}/fixtures/migrate/data-root" > /tmp/df-migrate-stress.log 2>&1; then
    ssh_cmd "python3 -c \"import json,datetime; print(json.dumps({'environment':'migrate-stress','timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'passed':1,'failed':0,'results':[{'id':'migrate-lock-stress','status':'pass'}]}))\" > ${STRESS_RESULT}"
  else
    EXIT_CODE=1
    ssh_cmd "cat /tmp/df-migrate-stress.log" || true
    ssh_cmd "python3 -c \"import json,datetime; print(json.dumps({'environment':'migrate-stress','timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'passed':0,'failed':1,'results':[{'id':'migrate-lock-stress','status':'fail'}]}))\" > ${STRESS_RESULT}" || true
  fi
  scp -i "$SSH_KEY" -o BatchMode=yes \
    "${SSH_HOST}:${STRESS_RESULT}" \
    "${ROOT}/tests/dark-factory/results/migrate-stress-${TIMESTAMP}.json" 2>/dev/null || true
  RESULT_FILES+=("${ROOT}/tests/dark-factory/results/migrate-stress-${TIMESTAMP}.json")
fi

if [[ "$LAYER" == "3" || "$LAYER" == "all" ]]; then
  if [[ "$HERMES_ENABLED" != "true" ]]; then
    echo "Hermes environment disabled in config — skipping Layer 3"
  else
    echo "== Layer 3: Hermes agent tests =="
    if bash "${ROOT}/scripts/dark-factory/run-agent-tests.sh"; then
      :
    else
      EXIT_CODE=1
    fi
    RESULT_FILES+=("${ROOT}/tests/dark-factory/results/hermes-${TIMESTAMP}.json")
  fi
fi

if [[ ${#RESULT_FILES[@]} -gt 0 ]]; then
  python3 "${ROOT}/scripts/dark-factory/report.py" "${RESULT_FILES[@]}" || EXIT_CODE=1
fi

exit "$EXIT_CODE"
