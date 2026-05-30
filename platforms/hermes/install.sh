#!/usr/bin/env bash
# Install outreachmagic (and optionally lead-enrich) under ~/.hermes/skills/.
# Hermes profiles get symlinks to ../../../skills/<name> — never full copies.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILLS_DIR="$HERMES_HOME/skills"
OM_REPO="https://github.com/outreachmagic/hermes-outreachmagic.git"
LE_REPO="https://github.com/outreachmagic/lead-enrich.git"

WITH_LEAD_ENRICH=0
MIGRATE=0
ALL_PROFILES=0
NO_PROFILES=0
OM_TAG=""
LE_TAG=""
PROFILES=()

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  Installs skills into ~/.hermes/skills/ (real files).
  Profiles use symlinks: profiles/<name>/skills/<skill> → ../../../skills/<skill>

  By default, symlinks every existing profile under ~/.hermes/profiles/ when present.

Options:
  --with-lead-enrich     Also install lead-enrich
  --no-profiles          Skip profile symlinks (global ~/.hermes/skills/ only)
  --all-profiles         Symlink all profiles (default when profiles/ exists)
  --profile NAME         Symlink one profile (repeatable)
  --migrate              Replace profile copies with symlinks (removes duplicates)
  --tag TAG              outreachmagic release tag (e.g. v1.20.15)
  --lead-enrich-tag TAG  lead-enrich release tag (e.g. v1.2.2)
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-lead-enrich) WITH_LEAD_ENRICH=1; shift ;;
    --migrate) MIGRATE=1; shift ;;
    --all-profiles) ALL_PROFILES=1; shift ;;
    --no-profiles) NO_PROFILES=1; shift ;;
    --profile) PROFILES+=("$2"); shift 2 ;;
    --tag) OM_TAG="$2"; shift 2 ;;
    --lead-enrich-tag) LE_TAG="$2"; shift 2 ;;
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
  tmp="$(mktemp -d -t om-hermes-XXXXXX)"
  trap 'rm -rf "$tmp"' RETURN
  echo "→ Installing outreachmagic to $SKILLS_DIR/outreachmagic"
  clone_repo "$OM_REPO" "$OM_TAG" "$tmp"
  mkdir -p "$SKILLS_DIR/outreachmagic"
  for item in SKILL.md README.md LICENSE SECURITY.md update-manifest.json scripts references; do
    if [[ -e "$tmp/$item" ]]; then
      rm -rf "$SKILLS_DIR/outreachmagic/$item"
      cp -a "$tmp/$item" "$SKILLS_DIR/outreachmagic/"
    fi
  done
  chmod +x "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" 2>/dev/null || true
  echo "→ Initializing database..."
  python3 "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" init
}

install_lead_enrich() {
  local tmp
  tmp="$(mktemp -d -t om-le-XXXXXX)"
  trap 'rm -rf "$tmp"' RETURN
  echo "→ Installing lead-enrich to $SKILLS_DIR/lead-enrich"
  clone_repo "$LE_REPO" "$LE_TAG" "$tmp"
  mkdir -p "$SKILLS_DIR/lead-enrich"
  for item in SKILL.md README.md SECURITY.md config.example.json default.env .gitignore references scripts; do
    if [[ -e "$tmp/$item" ]]; then
      rm -rf "$SKILLS_DIR/lead-enrich/$item"
      cp -a "$tmp/$item" "$SKILLS_DIR/lead-enrich/"
    fi
  done
  chmod +x "$SKILLS_DIR/lead-enrich/scripts/enrich.py" 2>/dev/null || true
  if [[ -n "${TRYKITT_API_KEY:-}" ]]; then
    local env_file="$HERMES_HOME/.env"
    touch "$env_file"
    if ! grep -q '^TRYKITT_API_KEY=' "$env_file" 2>/dev/null; then
      echo "TRYKITT_API_KEY=${TRYKITT_API_KEY}" >> "$env_file"
      echo "  → Added TRYKITT_API_KEY to $env_file"
    fi
  fi
}

profile_skill_link() {
  local profile=$1
  local skill=$2
  local prof_skills="$HERMES_HOME/profiles/$profile/skills"
  local link="$prof_skills/$skill"
  local rel="../../../skills/$skill"

  if [[ ! -d "$SKILLS_DIR/$skill" ]]; then
    echo "error: $SKILLS_DIR/$skill not installed" >&2
    exit 1
  fi

  mkdir -p "$prof_skills"

  if [[ -L "$link" ]]; then
    rm -f "$link"
  elif [[ -e "$link" ]]; then
    if [[ $MIGRATE -eq 1 ]]; then
      echo "→ Removing profile copy: $link"
      rm -rf "$link"
    else
      echo "error: $link exists and is not a symlink. Re-run with --migrate" >&2
      exit 1
    fi
  fi

  ln -sf "$rel" "$link"
  echo "  linked $link → $rel"
}

link_profiles() {
  local skill=$1
  shift
  local profiles=("$@")
  for profile in "${profiles[@]}"; do
    if [[ ! -d "$HERMES_HOME/profiles/$profile" ]]; then
      echo "warning: profile not found: $profile" >&2
      continue
    fi
    profile_skill_link "$profile" "$skill"
  done
}

discover_profiles() {
  local p
  for p in "$HERMES_HOME/profiles"/*/; do
    [[ -d "$p" ]] || continue
    basename "$p"
  done
}

has_hermes_profiles() {
  [[ -d "$HERMES_HOME/profiles" ]] || return 1
  local p
  for p in "$HERMES_HOME/profiles"/*/; do
    [[ -d "$p" ]] && return 0
  done
  return 1
}

if [[ $NO_PROFILES -eq 0 ]] && [[ $ALL_PROFILES -eq 0 ]] && [[ ${#PROFILES[@]} -eq 0 ]]; then
  if has_hermes_profiles; then
    ALL_PROFILES=1
    echo "→ Found Hermes profiles; linking skills into each (use --no-profiles to skip)"
  fi
fi

mkdir -p "$SKILLS_DIR"
install_outreachmagic
if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
  install_lead_enrich
fi

if [[ $ALL_PROFILES -eq 1 ]]; then
  while IFS= read -r profile; do
    PROFILES+=("$profile")
  done < <(discover_profiles)
fi

if [[ ${#PROFILES[@]} -gt 0 ]]; then
  echo "→ Profile symlinks"
  link_profiles outreachmagic "${PROFILES[@]}"
  if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
    link_profiles lead-enrich "${PROFILES[@]}"
  fi
fi

echo ""
echo "✓ Done."
echo "  outreachmagic: $SKILLS_DIR/outreachmagic"
if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
  echo "  lead-enrich:   $SKILLS_DIR/lead-enrich"
fi
echo "  Connect: python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py login"
echo "  Paths:   python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py paths"
if [[ ${#PROFILES[@]} -eq 0 ]]; then
  echo ""
  echo "  New profile:"
  echo "    mkdir -p $HERMES_HOME/profiles/<name>/skills"
  echo "    ln -sf ../../../skills/outreachmagic $HERMES_HOME/profiles/<name>/skills/outreachmagic"
  echo "    ln -sf ../../../skills/lead-enrich $HERMES_HOME/profiles/<name>/skills/lead-enrich"
  echo "  Or: bash install.sh --profile <name>"
fi
