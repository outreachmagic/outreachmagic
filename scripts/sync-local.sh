#!/usr/bin/env bash
# Dev sync: copy monorepo skills into ~/.hermes/skills/ (Hermes canonical path).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
OM_DIR="$HERMES_HOME/skills/outreachmagic"
LE_DIR="$HERMES_HOME/skills/lead-enrich"

sync_skill() {
  local name=$1
  local src="$ROOT/skills/$name"
  local dest="$HERMES_HOME/skills/$name"
  if [[ ! -f "$src/SKILL.md" ]]; then
    echo "error: missing $src/SKILL.md" >&2
    exit 1
  fi
  mkdir -p "$dest/scripts" "$dest/references" "$dest/databases" "$dest/config"
  cp "$src/SKILL.md" "$dest/SKILL.md"
  cp "$src/scripts/"*.py "$dest/scripts/"
  cp "$src/scripts/VERSION" "$dest/scripts/VERSION" 2>/dev/null || true
  if [[ -f "$src/references/schema.md" ]]; then
    mkdir -p "$dest/references"
    cp "$src/references/schema.md" "$dest/references/schema.md"
  fi
  chmod +x "$dest/scripts/"*.py 2>/dev/null || true
  if [[ -f "$dest/SKILL.md" ]] && [[ -f "$dest/scripts/VERSION" ]]; then
    ver="$(cat "$dest/scripts/VERSION")"
    perl -i -pe "s/^version: .*/version: $ver/" "$dest/SKILL.md" 2>/dev/null || true
  fi
  echo "  synced $name → $dest"
}

echo "Outreach Magic — dev sync to $HERMES_HOME/skills/"
sync_skill outreachmagic
if [[ -d "$ROOT/skills/lead-enrich" ]]; then
  mkdir -p "$LE_DIR/scripts"
  cp "$ROOT/skills/lead-enrich/SKILL.md" "$LE_DIR/SKILL.md"
  cp "$ROOT/skills/lead-enrich/scripts/enrich.py" "$LE_DIR/scripts/enrich.py"
  chmod +x "$LE_DIR/scripts/enrich.py" 2>/dev/null || true
  echo "  synced lead-enrich → $LE_DIR"
fi

python3 "$OM_DIR/scripts/pipeline.py" init

chmod 700 "$OM_DIR/databases" "$OM_DIR/config" 2>/dev/null || true
chmod 600 "$OM_DIR/databases/outreachmagic.db" "$OM_DIR/config/outreachmagic_config.json" 2>/dev/null || true

if [[ "${1:-}" == "--all-profiles" ]]; then
  bash "$ROOT/platforms/hermes/install.sh" --all-profiles --with-lead-enrich --migrate
fi

VER="$(cat "$OM_DIR/scripts/VERSION" 2>/dev/null || echo "?")"
echo ""
echo "  Version: $VER"
echo "  Paths:   python3 $OM_DIR/scripts/pipeline.py paths"
echo "  Login:   python3 $OM_DIR/scripts/pipeline.py login"
