#!/usr/bin/env bash
# Optional Layer 1b: wbhk-billing policy tests (sibling repo).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BILLING_REPO="${WBHK_BILLING_PATH:-${ROOT}/../wbhk-billing}"

if [[ ! -f "${BILLING_REPO}/package.json" ]]; then
  echo "Layer 1b: skip wbhk-billing (not found at ${BILLING_REPO})"
  exit 0
fi

echo "Layer 1b wbhk-billing gate:"
(cd "${BILLING_REPO}" && npm test)
