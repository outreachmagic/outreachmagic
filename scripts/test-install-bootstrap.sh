#!/usr/bin/env bash
# Smoke-test install.sh bootstrap paths (no network).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d -t om-install-test-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

cp "$ROOT/install.sh" "$TMP/install.sh"
chmod +x "$TMP/install.sh"

out="$("$TMP/install.sh" --local --platform hermes 2>&1)" && {
  echo "error: expected --local to fail without companions" >&2
  exit 1
}
if [[ "$out" == *"install-companions.sh missing"* ]]; then
  echo "ok: --local without companions fails clearly"
else
  echo "error: unexpected --local output: $out" >&2
  exit 1
fi

if [[ -f "$ROOT/platforms/common/install-companions.sh" ]]; then
  if "$ROOT/install.sh" --help 2>&1 | grep -q "Usage: install.sh"; then
    echo "ok: monorepo install.sh --help"
  else
    echo "error: install.sh --help failed in monorepo" >&2
    exit 1
  fi
fi

echo "install bootstrap tests passed"
