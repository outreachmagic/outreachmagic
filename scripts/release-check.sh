#!/usr/bin/env bash
# Pre-tag gate: manifests, validators, pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== Regenerate manifests from skill-suite.json =="
${PYTHON:-python3} scripts/generate_skill_manifest.py --all

echo "== Verify manifests committed =="
git diff --exit-code \
  skills/outreachmagic/update-manifest.json

echo "== Lint: undefined names (missing imports) =="
if command -v ruff >/dev/null 2>&1; then
  ruff check --select F821 skills/outreachmagic/scripts/
elif ${PYTHON:-python3} -m ruff --version >/dev/null 2>&1; then
  ${PYTHON:-python3} -m ruff check --select F821 skills/outreachmagic/scripts/
else
  ${PYTHON:-python3} -m pip install -q ruff 2>/dev/null && ${PYTHON:-python3} -m ruff check --select F821 skills/outreachmagic/scripts/ || {
    echo "  ruff not found and could not be pip-installed (externally-managed Python?)." >&2
    echo "  Install it with: brew install ruff   OR   pipx install ruff   OR   pip install --break-system-packages ruff" >&2
    exit 1
  }
fi

echo "== Sentiment integrity =="
for f in skills/outreachmagic/scripts/campaign_stats.py skills/outreachmagic/scripts/pipeline_lead_review.py skills/outreachmagic/scripts/pipeline.py; do
    if grep -q "'neutral'" "$f"; then
        echo "FAIL: $f contains legacy 'neutral' sentiment value"
        exit 1
    fi
done
echo "  canonical values: OK"

echo "== Pytest gate =="
bash scripts/run-tests.sh

echo "== Doc grep: no legacy ~/.hermes/.env in agent-facing install docs =="
if rg -n '~/.hermes/\.env' AGENTS-INSTALL.md docs/AGENT-INTENTS.md skills/*/SECURITY.md 2>/dev/null; then
  echo "error: remove legacy ~/.hermes/.env references from install/agent docs" >&2
  exit 1
fi

echo "== Install doc sync and pattern validation =="
${PYTHON:-python3} scripts/sync_install_docs.py --check

echo "release-check: PASS"
