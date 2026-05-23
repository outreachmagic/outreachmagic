#!/usr/bin/env bash
# Outreach Magic — One-command install for Hermes
# curl -fsSL https://raw.githubusercontent.com/outreachmagic/outreach-magic-skill/main/scripts/install.sh | bash

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_DIR="$HERMES_HOME/skills/sales/outreach-magic"

echo "  Outreach Magic — Pipeline visibility for Hermes"
echo "  Installing..."

mkdir -p "$SKILL_DIR/scripts" "$SKILL_DIR/references" "$SKILL_DIR/templates"

# Clone or copy files
if [ -d ".git" ]; then
    # Running from repo
    cp scripts/pipeline.py "$SKILL_DIR/scripts/pipeline.py" 2>/dev/null || \
    cp pipeline/pipeline.py "$SKILL_DIR/scripts/pipeline.py"
    cp pipeline/server.py "$SKILL_DIR/scripts/server.py" 2>/dev/null || true
    cp skill/SKILL.md "$SKILL_DIR/SKILL.md" 2>/dev/null || \
    cp SKILL.md "$SKILL_DIR/SKILL.md" 2>/dev/null || true
    cp references/schema.md "$SKILL_DIR/references/schema.md" 2>/dev/null || true
else
    # Direct install from URL
    REPO="https://raw.githubusercontent.com/outreachmagic/outreach-magic-skill/main"
    curl -fsSL "$REPO/pipeline/pipeline.py" -o "$SKILL_DIR/scripts/pipeline.py"
    curl -fsSL "$REPO/pipeline/server.py" -o "$SKILL_DIR/scripts/server.py"
    curl -fsSL "$REPO/skill/SKILL.md" -o "$SKILL_DIR/SKILL.md"
    curl -fsSL "$REPO/references/schema.md" -o "$SKILL_DIR/references/schema.md"
fi

chmod +x "$SKILL_DIR/scripts/pipeline.py" 2>/dev/null || true
chmod +x "$SKILL_DIR/scripts/server.py" 2>/dev/null || true

# Initialize database
python3 "$SKILL_DIR/scripts/pipeline.py" init

echo
echo "  Installed to: $SKILL_DIR"
echo "  Database:     $HERMES_HOME/outreach_magic.db"
echo
echo "  Quick start:"
echo "    hermes skills install outreach-magic"
echo "    hermes -s outreach-magic"
echo "    python3 $SKILL_DIR/scripts/pipeline.py show"
echo
echo "  Connect sequencers:"
echo "    python3 $SKILL_DIR/scripts/pipeline.py connect --key YOUR_KEY"
echo
echo "  Your Hermes agent will now auto-log all outreach activity."