#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/tests/test_workspace_routing.py"
python3 "$ROOT/tests/test_lead_enrich.py"
