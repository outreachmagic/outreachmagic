#!/usr/bin/env bash
# Outreach Magic — Dev sync: copy skills/outreachmagic from this repo into ~/.hermes
# For local development only. End users should install via:
#   See docs/install.md (git clone from github.com/outreachmagic/hermes-skill)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_SRC="$ROOT/skills/outreachmagic"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_DIR="$HERMES_HOME/skills/outreachmagic"

if [[ ! -f "$SKILL_SRC/SKILL.md" ]]; then
  echo "error: expected skill at $SKILL_SRC" >&2
  exit 1
fi

echo "  Outreach Magic — sync local skill to Hermes"
echo "  Source: $SKILL_SRC"
echo "  Target: $SKILL_DIR"
echo

mkdir -p "$SKILL_DIR/scripts" "$SKILL_DIR/references" "$SKILL_DIR/databases" "$SKILL_DIR/config"

cp "$SKILL_SRC/SKILL.md" "$SKILL_DIR/SKILL.md"
cp "$SKILL_SRC/scripts/"*.py "$SKILL_DIR/scripts/"
cp "$SKILL_SRC/scripts/VERSION" "$SKILL_DIR/scripts/VERSION"
if [[ -f "$SKILL_SRC/references/schema.md" ]]; then
  cp "$SKILL_SRC/references/schema.md" "$SKILL_DIR/references/schema.md"
fi

chmod +x "$SKILL_DIR/scripts/pipeline.py" 2>/dev/null || true

VER="$(cat "$SKILL_DIR/scripts/VERSION" 2>/dev/null || echo "0.0.0")"
if [[ -f "$SKILL_DIR/SKILL.md" ]]; then
  perl -i -pe "s/^version: .*/version: $VER/" "$SKILL_DIR/SKILL.md"
fi

python3 "$SKILL_DIR/scripts/pipeline.py" init

chmod 700 "$SKILL_DIR/databases" "$SKILL_DIR/config" 2>/dev/null || true
chmod 600 "$SKILL_DIR/databases/outreachmagic.db" "$SKILL_DIR/config/outreachmagic_config.json" 2>/dev/null || true

echo
echo "  Synced to: $SKILL_DIR"
echo "  Database:  $SKILL_DIR/databases/outreachmagic.db"
echo "  Version:   $VER"
echo
echo "  Hermes:"
echo "    git clone https://github.com/outreachmagic/hermes-skill.git /tmp/om-hermes"
echo "    cp -r /tmp/om-hermes/{SKILL.md,scripts,references} ~/.hermes/skills/outreachmagic/"
echo "    rm -rf /tmp/om-hermes"
echo "    hermes -s outreachmagic"
echo
echo "  After git pull, re-run:"
echo "    bash scripts/sync-local.sh"
echo
echo "  Connect relay:"
echo "    python3 $SKILL_DIR/scripts/pipeline.py setup --key om_agent_YOUR_KEY"
