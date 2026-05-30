#!/usr/bin/env bash
# Run HermesHub SkillScan against all skills/*/SKILL.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCANNER="${SKILL_SCAN_SCRIPT:-/tmp/scan-skill.py}"

if [[ ! -f "$SCANNER" ]]; then
  echo "Downloading HermesHub scan-skill.py..."
  curl -fsSL "https://raw.githubusercontent.com/amanning3390/hermeshub/main/scripts/scan-skill.py" \
    -o "$SCANNER"
fi

failed=0
for skill_md in "$ROOT"/skills/*/SKILL.md; do
  [[ -f "$skill_md" ]] || continue
  echo "Scanning $skill_md"
  if ! python3 "$SCANNER" "$skill_md" "$@"; then
    failed=1
  fi
done

exit "$failed"
