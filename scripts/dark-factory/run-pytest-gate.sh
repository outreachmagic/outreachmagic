#!/usr/bin/env bash
# Layer 1: fast local pytest for relay pull/sync paths (no VPS, no network).
# Catches control-flow bugs like UnboundLocalError when optional pull phases are skipped.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if ! python3 -m pytest --version >/dev/null 2>&1; then
  echo "Installing pytest..."
  python3 -m pip install -q pytest
fi

echo "Layer 1 pytest gate (pull / relay / sync / billing):"
python3 -m pytest \
  "${ROOT}/tests/test_pull_diagnostics.py" \
  "${ROOT}/tests/test_pull_flag_matrix.py" \
  "${ROOT}/tests/test_billing_contract.py" \
  "${ROOT}/tests/test_skill_install_contract.py" \
  "${ROOT}/tests/test_sync_cloud_pending.py" \
  "${ROOT}/tests/test_routing_sync.py" \
  "${ROOT}/tests/test_full_pull_replay.py" \
  "${ROOT}/tests/test_agent_sync_timestamp.py" \
  "${ROOT}/tests/test_relay_pull_bulk_dedupe.py" \
  "${ROOT}/tests/test_lead_source_relay_sync.py" \
  "${ROOT}/tests/test_companion_env_sources.py" \
  "${ROOT}/tests/test_security_install_docs.py" \
  "${ROOT}/tests/test_auth_error_summary.py" \
  "${ROOT}/tests/test_email_finder.py" \
  "${ROOT}/tests/test_apply_email_find_results.py" \
  "${ROOT}/tests/test_bug_report_20260611.py" \
  "${ROOT}/tests/test_lead_enrich.py" \
  -q --tb=short

bash "${ROOT}/scripts/dark-factory/run-billing-gate.sh"
