#!/usr/bin/env bash
# Install outreachmagic (+ optional companions) under ~/.cursor/skills/.
set -euo pipefail

SKILLS_DIR="$HOME/.cursor/skills"
OM_REPO="https://github.com/outreachmagic/cursor-outreachmagic.git"
LE_REPO="https://github.com/outreachmagic/lead-enrich.git"
EF_REPO="https://github.com/outreachmagic/email-finder.git"

WITH_LEAD_ENRICH=0
WITH_EMAIL_FINDER=0
OM_TAG=""
LE_TAG=""
EF_TAG=""

_here="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$_here/install-companions.sh" ]]; then
  source "$_here/install-companions.sh"
elif [[ -f "$_here/../common/install-companions.sh" ]]; then
  source "$_here/../common/install-companions.sh"
else
  echo "error: install-companions.sh not found" >&2
  exit 1
fi

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --with-lead-enrich       Also install lead-enrich
  --with-email-finder      Also install email-finder (implies --with-lead-enrich)
  --tag TAG                outreachmagic release tag
  --lead-enrich-tag TAG    lead-enrich release tag
  --email-finder-tag TAG   email-finder release tag
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-lead-enrich) WITH_LEAD_ENRICH=1; shift ;;
    --with-email-finder) WITH_LEAD_ENRICH=1; WITH_EMAIL_FINDER=1; shift ;;
    --tag) OM_TAG="$2"; shift 2 ;;
    --lead-enrich-tag) LE_TAG="$2"; shift 2 ;;
    --email-finder-tag) EF_TAG="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

clone_repo() {
  local repo=$1
  local tag=$2
  local dest=$3
  rm -rf "$dest"
  mkdir -p "$dest"
  if [[ -n "$tag" ]]; then
    git clone --depth 1 --branch "$tag" "$repo" "$dest"
  else
    git clone --depth 1 "$repo" "$dest"
  fi
}

install_outreachmagic() {
  local tmp
  tmp="$(mktemp -d -t om-cursor-XXXXXX)"
  trap 'rm -rf "$tmp"' RETURN
  echo "→ Installing outreachmagic to $SKILLS_DIR/outreachmagic"
  clone_repo "$OM_REPO" "$OM_TAG" "$tmp"
  mkdir -p "$SKILLS_DIR/outreachmagic"
  for item in SKILL.md README.md LICENSE SECURITY.md update-manifest.json outreachmagic.mdc scripts references; do
    if [[ -e "$tmp/$item" ]]; then
      rm -rf "$SKILLS_DIR/outreachmagic/$item"
      cp -a "$tmp/$item" "$SKILLS_DIR/outreachmagic/"
    fi
  done
  chmod +x "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" 2>/dev/null || true
  echo "→ Initializing database..."
  python3 "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" init
}

mkdir -p "$SKILLS_DIR"
install_outreachmagic
if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
  install_lead_enrich
fi
if [[ $WITH_EMAIL_FINDER -eq 1 ]]; then
  install_email_finder
fi

echo ""
echo "✓ Done."
echo "  outreachmagic: $SKILLS_DIR/outreachmagic"
if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
  echo "  lead-enrich:   $SKILLS_DIR/lead-enrich"
fi
if [[ $WITH_EMAIL_FINDER -eq 1 ]]; then
  echo "  email-finder:  $SKILLS_DIR/email-finder"
fi
echo "  Connect: python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py login"
