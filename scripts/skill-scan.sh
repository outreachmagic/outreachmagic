#!/usr/bin/env bash
# Run HermesHub SkillScan against skills/outreachmagic/SKILL.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCANNER="${SKILL_SCAN_SCRIPT:-/tmp/scan-skill.py}"
SKILL_DIR="$ROOT/skills/outreachmagic"

if [[ ! -f "$SCANNER" ]]; then
  echo "Downloading HermesHub scan-skill.py..."
  curl -fsSL "https://raw.githubusercontent.com/amanning3390/hermeshub/main/scripts/scan-skill.py" \
    -o "$SCANNER"
fi

echo "Scanning $SKILL_DIR/SKILL.md"
python3 "$SCANNER" "$SKILL_DIR/SKILL.md" "$@"
