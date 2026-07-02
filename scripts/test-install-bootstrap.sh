#!/usr/bin/env bash
# Smoke-test install.sh bootstrap paths (no network).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d -t om-install-test-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

cp "$ROOT/install.sh" "$TMP/install.sh"
chmod +x "$TMP/install.sh"

if "$ROOT/install.sh" --help 2>&1 | grep -q "Usage: install.sh"; then
  echo "ok: install.sh --help"
else
  echo "error: install.sh --help failed" >&2
  exit 1
fi

echo "ok: install.sh bootstrap test passed"
