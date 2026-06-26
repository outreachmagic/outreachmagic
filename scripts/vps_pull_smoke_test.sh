#!/usr/bin/env bash
# Smoke-test all relay pull kinds on Hermes VPS (incremental, one page each).
# Usage: bash scripts/vps_pull_smoke_test.sh [TAG]
set -euo pipefail

TAG="${1:-v1.24.10}"
OM_HOME="${OM_HOME:-$HOME/hermes/instances/magic/data/skills/outreachmagic}"
if [[ -x /home/hermes/.hermes/skills/outreachmagic/scripts/pipeline.py ]]; then
  OM_HOME="/home/hermes/.hermes/skills/outreachmagic"
fi
PIPE="${OM_HOME}/scripts/pipeline.py"
export HERMES_PROFILE="${HERMES_PROFILE:-magic}"

echo "=== Outreach Magic VPS pull smoke (tag ${TAG}) ==="
echo "OM_HOME=${OM_HOME}"

if pgrep -f "pipeline.py pull" >/dev/null 2>&1; then
  echo "ERROR: another pull is running — stop it first" >&2
  exit 1
fi

python3 "${PIPE}" update --tag "${TAG}"
python3 "${PIPE}" pull --probe

run_kind() {
  local kind="$1"
  local log="/tmp/om-pull-smoke-${kind}.log"
  echo "--- pull --kind ${kind} (120s cap) ---"
  if timeout 120 python3 "${PIPE}" pull --kind "${kind}" >"${log}" 2>&1; then
    echo "OK ${kind}"
    tail -8 "${log}"
  else
    local ec=$?
    echo "FAIL ${kind} exit=${ec}" >&2
    tail -30 "${log}" >&2
    return "${ec}"
  fi
}

for kind in events core workspace company; do
  run_kind "${kind}" || exit 1
done

echo "--- pull --skip-snapshots (120s cap) ---"
SKIP_LOG="/tmp/om-pull-smoke-skip-snapshots.log"
if timeout 120 python3 "${PIPE}" pull --skip-snapshots >"${SKIP_LOG}" 2>&1; then
  echo "OK skip-snapshots"
  tail -8 "${SKIP_LOG}"
else
  ec=$?
  echo "FAIL skip-snapshots exit=${ec}" >&2
  tail -30 "${SKIP_LOG}" >&2
  exit "${ec}"
fi

echo "=== smoke complete ==="
