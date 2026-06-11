#!/usr/bin/env bash
# Pre-tag gate: manifests, validators, companion sync, Layer 1 pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== Regenerate manifests from skill-suite.json =="
python3 scripts/generate_skill_manifest.py --all

echo "== Verify manifests committed =="
git diff --exit-code \
  skills/outreachmagic/update-manifest.json \
  skills/email-finder/update-manifest.json \
  skills/lead-enrich/update-manifest.json

echo "== Companion common sync =="
bash scripts/sync-companion-common.sh --check

echo "== Manifest validators =="
python3 scripts/dark-factory/validate-om-manifest.py
python3 scripts/validate-companion-manifests.py

echo "== Layer 1 pytest gate =="
bash scripts/dark-factory/run-pytest-gate.sh

echo "== Doc grep: no legacy ~/.hermes/.env in agent-facing install docs =="
if rg -n '~/.hermes/\.env' AGENTS-INSTALL.md docs/AGENT-INTENTS.md skills/*/SECURITY.md 2>/dev/null; then
  echo "error: remove legacy ~/.hermes/.env references from install/agent docs" >&2
  exit 1
fi

echo "release-check: PASS"
