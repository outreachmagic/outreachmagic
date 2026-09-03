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
# This step used to ban 'neutral' as a legacy value (8216742, 2026-06-29). Nine
# days later 73bc2fc made it a valid sentiment across the CLI, stats, the GHL
# driver and lead review, and nobody came back for the guard -- so the gate sat
# red on main while the code was correct. A ban on a value the product supports
# is not integrity, it is a stale opinion.
#
# What is worth guarding is that the enumerations agree. LEAD_SENTIMENT_VALUES
# is the canonical set; every other place that lists sentiments must match it,
# or a value gets accepted on write and dropped on read -- which is exactly what
# happened to `neutral` in reporting_queries.SENTIMENT_KEYS.
${PYTHON:-python3} scripts/check_sentiment_values.py

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
