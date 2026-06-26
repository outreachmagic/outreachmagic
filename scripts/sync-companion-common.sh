#!/usr/bin/env bash
# Keep lead-enrich companion_common.py identical to email-finder (canonical copy).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CANONICAL="$ROOT/skills/email-finder/scripts/companion_common.py"
TARGET="$ROOT/skills/lead-enrich/scripts/companion_common.py"

if [[ ! -f "$CANONICAL" ]]; then
  echo "error: missing canonical companion_common: $CANONICAL" >&2
  exit 1
fi

MODE="${1:-}"
if [[ "$MODE" == "--check" ]]; then
  if cmp -s "$CANONICAL" "$TARGET"; then
    echo "companion_common.py: email-finder and lead-enrich are in sync"
    exit 0
  fi
  echo "error: companion_common.py diverged (canonical: email-finder)" >&2
  diff -u "$TARGET" "$CANONICAL" | head -40 >&2 || true
  exit 1
fi

cp "$CANONICAL" "$TARGET"
echo "Synced $TARGET from email-finder"
