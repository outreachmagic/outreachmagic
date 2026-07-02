#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/test-install-bootstrap.sh"
bash "$ROOT/scripts/sync-companion-common.sh" --check
${PYTHON:-python3} -m pytest "$ROOT/tests/" -q
