#!/usr/bin/env bash
set -euo pipefail

AGENT_KEY="${1:-}"
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

if [ -n "$AGENT_KEY" ]; then
  echo "→ Connecting agent key..."
  python3 "$SKILL_DIR/scripts/pipeline.py" setup --key "$AGENT_KEY"
  echo ""
  echo "✓ OutreachMagic installed and connected."
  echo "  Restart Claude Code, then ask: \"show me my pipeline\""
else
  echo ""
  echo "✓ OutreachMagic installed."
  echo "  Connect your agent key:"
  echo "    python3 $SKILL_DIR/scripts/pipeline.py setup --key om_agent_YOUR_KEY"
fi
