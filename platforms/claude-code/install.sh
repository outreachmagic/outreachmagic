#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$HOME/.claude/skills/outreachmagic"
TMP_DIR="$(mktemp -d -t om-claude-XXXXXX)"
REPO_URL="https://github.com/outreachmagic/claude-code-outreachmagic.git"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

echo "→ Cloning OutreachMagic skill..."
git clone --depth 1 "$REPO_URL" "$TMP_DIR" >/dev/null 2>&1

mkdir -p "$SKILL_DIR"

echo "→ Installing to $SKILL_DIR..."
for item in SKILL.md README.md LICENSE SECURITY.md CLAUDE_SNIPPET.md update-manifest.json scripts references; do
  if [ -e "$TMP_DIR/$item" ]; then
    rm -rf "$SKILL_DIR/$item"
    cp -a "$TMP_DIR/$item" "$SKILL_DIR/"
  fi
done

echo "→ Initializing..."
python3 "$SKILL_DIR/scripts/pipeline.py" init

echo ""
echo "✓ OutreachMagic installed."
echo "  Connect your account:"
echo "    python3 $SKILL_DIR/scripts/pipeline.py login"
echo "  Restart Claude Code, then ask: \"show me my pipeline\""
