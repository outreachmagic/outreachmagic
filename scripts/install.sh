#!/usr/bin/env bash
# Outreach Magic — Install / sync skill into Hermes (~/.hermes)
# curl -fsSL https://raw.githubusercontent.com/outreachmagic/hermes-agent/main/scripts/install.sh | bash

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_DIR="$HERMES_HOME/skills/sales/outreachmagic"
LEGACY_SKILL_DIR="$HERMES_HOME/skills/sales/outreach-magic"
REPO_RAW="${OUTREACHMAGIC_UPDATE_URL:-https://raw.githubusercontent.com/outreachmagic/hermes-agent/main}"

echo "  Outreach Magic — sync skill to Hermes"
echo "  Installing..."

# Migrate legacy skill folder name
if [[ -d "$LEGACY_SKILL_DIR" && ! -d "$SKILL_DIR" ]]; then
  mv "$LEGACY_SKILL_DIR" "$SKILL_DIR"
  echo "  Migrated $LEGACY_SKILL_DIR → $SKILL_DIR"
fi

mkdir -p "$SKILL_DIR/scripts" "$SKILL_DIR/references" "$SKILL_DIR/templates"

if [[ -d ".git" ]]; then
  # Running from a local clone
  cp pipeline/pipeline.py "$SKILL_DIR/scripts/pipeline.py"
  cp pipeline/relay_extractors.py "$SKILL_DIR/scripts/relay_extractors.py"
  cp pipeline/VERSION "$SKILL_DIR/scripts/VERSION" 2>/dev/null || true
  cp pipeline/server.py "$SKILL_DIR/scripts/server.py" 2>/dev/null || true
  cp skill/SKILL.md "$SKILL_DIR/SKILL.md" 2>/dev/null || cp SKILL.md "$SKILL_DIR/SKILL.md" 2>/dev/null || true
  cp references/schema.md "$SKILL_DIR/references/schema.md" 2>/dev/null || true
else
  base="$REPO_RAW/pipeline"
  curl -fsSL "$base/pipeline.py" -o "$SKILL_DIR/scripts/pipeline.py"
  curl -fsSL "$base/relay_extractors.py" -o "$SKILL_DIR/scripts/relay_extractors.py"
  curl -fsSL "$base/VERSION" -o "$SKILL_DIR/scripts/VERSION"
  curl -fsSL "$REPO_RAW/pipeline/server.py" -o "$SKILL_DIR/scripts/server.py" 2>/dev/null || true
  curl -fsSL "$REPO_RAW/skill/SKILL.md" -o "$SKILL_DIR/SKILL.md" 2>/dev/null || true
fi

chmod +x "$SKILL_DIR/scripts/pipeline.py" 2>/dev/null || true
chmod +x "$SKILL_DIR/scripts/server.py" 2>/dev/null || true

# Single version: pipeline/VERSION → scripts/VERSION + SKILL.md frontmatter
VER="$(cat "$SKILL_DIR/scripts/VERSION" 2>/dev/null || echo "0.0.0")"
if [[ -f "$SKILL_DIR/SKILL.md" ]]; then
  perl -i -pe "s/^version: .*/version: $VER/" "$SKILL_DIR/SKILL.md"
fi

python3 "$SKILL_DIR/scripts/pipeline.py" init

echo
echo "  Synced to: $SKILL_DIR"
echo "  Database:  $HERMES_HOME/outreachmagic.db"
echo "  Version:   $(cat "$SKILL_DIR/scripts/VERSION" 2>/dev/null || echo unknown)"
echo
echo "  Hermes:"
echo "    hermes skills install outreachmagic"
echo "    hermes -s outreachmagic"
echo
echo "  After git pull, re-run this script or:"
echo "    python3 $SKILL_DIR/scripts/pipeline.py update"
echo
echo "  Connect relay:"
echo "    python3 $SKILL_DIR/scripts/pipeline.py connect --key YOUR_KEY"
