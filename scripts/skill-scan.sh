#!/usr/bin/env bash
# Run HermesHub SkillScan against skills/*/SKILL.md (or one skill by name).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCANNER="${SKILL_SCAN_SCRIPT:-/tmp/scan-skill.py}"
SCANNER_URL="https://raw.githubusercontent.com/amanning3390/hermeshub/main/scripts/scan-skill.py"

if [[ ! -f "$SCANNER" ]]; then
  echo "Downloading HermesHub scan-skill.py..."
  if ! curl -fsSL "$SCANNER_URL" -o "$SCANNER" 2>/dev/null; then
    echo "WARNING: Could not download SkillScan script (${SCANNER_URL}). Skipping scan."
    exit 0
  fi
fi

scan_args=()
skill_paths=()
for arg in "$@"; do
  if [[ "$arg" == --* ]]; then
    scan_args+=("$arg")
  elif [[ -f "$arg" ]]; then
    skill_paths+=("$arg")
  elif [[ -f "$ROOT/skills/$arg/SKILL.md" ]]; then
    skill_paths+=("$ROOT/skills/$arg/SKILL.md")
  else
    echo "error: unknown skill or path: $arg" >&2
    exit 1
  fi
done

if [[ ${#skill_paths[@]} -eq 0 ]]; then
  for skill_md in "$ROOT"/skills/*/SKILL.md; do
    [[ -f "$skill_md" ]] && skill_paths+=("$skill_md")
  done
fi

failed=0
for skill_md in "${skill_paths[@]}"; do
  echo "Scanning $skill_md"
  if ! python3 "$SCANNER" "$skill_md" "${scan_args[@]}"; then
    failed=1
  fi
done

exit "$failed"
