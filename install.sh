#!/usr/bin/env bash
# Install outreachmagic (+ optional companions) for Hermes, Cursor, or Claude Code.
# Real files live under <platform>/skills/outreachmagic/. Hermes profiles symlink only.
set -euo pipefail

OM_REPO="https://github.com/outreachmagic/outreachmagic.git"
LE_REPO="https://github.com/outreachmagic/lead-enrich.git"
EF_REPO="https://github.com/outreachmagic/email-finder.git"

PLATFORM=""
WITH_LEAD_ENRICH=0
WITH_EMAIL_FINDER=0
MIGRATE=0
ALL_PROFILES=0
NO_PROFILES=0
LOCAL=0
OM_TAG=""
LE_TAG=""
EF_TAG=""
PROFILES=()

_install_ts() {
  date "+%H:%M:%S"
}

_log_step() {
  echo "[$(_install_ts)] → $*"
}

_install_root() {
  local src="${BASH_SOURCE[0]:-$0}"
  case "$src" in
    ""|bash|/bin/bash|/bin/sh|dash|-)
      return 1
      ;;
  esac
  [[ -f "$src" ]] || return 1
  cd "$(dirname "$src")" && pwd
}

# curl … | bash has no on-disk script dir; clone the public repo and re-exec.
_bootstrap_repo_tag_from_args() {
  local tag="" arg
  for arg in "$@"; do
    case "$arg" in
      --local)
        echo "error: --local requires a full repo checkout (platforms/common/install-companions.sh missing)" >&2
        return 2
        ;;
    esac
  done
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tag)
        if [[ $# -lt 2 ]]; then
          echo "error: --tag requires a value" >&2
          return 2
        fi
        tag="$2"
        shift 2
        ;;
      *) shift ;;
    esac
  done
  printf '%s' "$tag"
}

_bootstrap_install_if_needed() {
  local root=""
  root="$(_install_root 2>/dev/null || true)"
  if [[ -n "$root" && -f "$root/platforms/common/install-companions.sh" ]]; then
    _here="$root"
    return 0
  fi

  local tag=""
  if ! tag="$(_bootstrap_repo_tag_from_args "$@")"; then
    exit 1
  fi

  local tmp
  tmp="$(mktemp -d -t om-install-bootstrap-XXXXXX)"
  _log_step "Fetching installer bundle from $OM_REPO${tag:+ @ $tag}"
  if [[ -n "$tag" ]]; then
    git clone --depth 1 --progress --branch "$tag" "$OM_REPO" "$tmp"
  else
    git clone --depth 1 --progress "$OM_REPO" "$tmp"
  fi
  if [[ ! -f "$tmp/platforms/common/install-companions.sh" ]]; then
    echo "error: cloned repo missing platforms/common/install-companions.sh" >&2
    rm -rf "$tmp"
    exit 1
  fi
  exec bash "$tmp/install.sh" "$@"
}

_here=""
_bootstrap_install_if_needed "$@"

# shellcheck source=platforms/common/install-companions.sh
source "$_here/platforms/common/install-companions.sh"

usage() {
  cat <<'EOF'
Usage: install.sh --platform <hermes|cursor|claude> [options]

Installs outreachmagic from github.com/outreachmagic/outreachmagic into the
platform skills directory. Hermes: real files under ~/.hermes/skills/; profiles
get symlinks only (never full copies).

Options:
  --platform NAME          Required: hermes, cursor, or claude
  --with-lead-enrich       Also install lead-enrich
  --with-email-finder      Also install email-finder (implies --with-lead-enrich)
  --local                  Install from this repo checkout (dev) instead of cloning
  --no-profiles            Hermes only: skip profile symlinks
  --all-profiles           Hermes only: symlink all existing profiles (default when profiles/ exists)
  --profile NAME           Hermes only: symlink one profile (repeatable)
  --migrate                Hermes only: replace profile copies with symlinks
  --tag TAG                outreachmagic release tag (e.g. v1.20.24)
  --lead-enrich-tag TAG    lead-enrich release tag
  --email-finder-tag TAG   email-finder release tag
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --with-lead-enrich) WITH_LEAD_ENRICH=1; shift ;;
    --with-email-finder) WITH_LEAD_ENRICH=1; WITH_EMAIL_FINDER=1; shift ;;
    --local) LOCAL=1; shift ;;
    --migrate) MIGRATE=1; shift ;;
    --all-profiles) ALL_PROFILES=1; shift ;;
    --no-profiles) NO_PROFILES=1; shift ;;
    --profile) PROFILES+=("$2"); shift 2 ;;
    --tag) OM_TAG="$2"; shift 2 ;;
    --lead-enrich-tag) LE_TAG="$2"; shift 2 ;;
    --email-finder-tag) EF_TAG="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$PLATFORM" ]]; then
  echo "error: --platform is required (hermes, cursor, or claude)" >&2
  usage
  exit 1
fi

# Pin companion releases from skill-suite.json when not specified.
# Fallback tags below are for public installer bundles before scripts/skill_suite.py shipped.
if [[ $WITH_LEAD_ENRICH -eq 1 && -z "$LE_TAG" ]]; then
  LE_TAG="$(_read_suite_install_tag lead-enrich 2>/dev/null || true)"
  [[ -n "$LE_TAG" ]] || LE_TAG="v2.1.6"
fi
if [[ $WITH_EMAIL_FINDER -eq 1 && -z "$EF_TAG" ]]; then
  EF_TAG="$(_read_suite_install_tag email-finder 2>/dev/null || true)"
  [[ -n "$EF_TAG" ]] || EF_TAG="v2.2.18"
fi

case "$PLATFORM" in
  hermes)
    HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
    SKILLS_DIR="$HERMES_HOME/skills"
    TRYKITT_ENV_FILE="$HERMES_HOME/.env"
    ;;
  cursor)
    SKILLS_DIR="$HOME/.cursor/skills"
    ;;
  claude)
    SKILLS_DIR="$HOME/.claude/skills"
    ;;
  *)
    echo "error: unknown platform: $PLATFORM (use hermes, cursor, or claude)" >&2
    exit 1
    ;;
esac

clone_repo() {
  local repo=$1
  local tag=$2
  local dest=$3
  local started=$SECONDS
  rm -rf "$dest"
  mkdir -p "$dest"
  if [[ -n "$tag" ]]; then
    git clone --depth 1 --progress --branch "$tag" "$repo" "$dest"
  else
    git clone --depth 1 --progress "$repo" "$dest"
  fi
  _log_step "Clone complete ($((SECONDS - started))s)"
}

copy_skill_tree() {
  local src=$1
  local dest=$2
  local count=0
  mkdir -p "$dest"
  for item in SKILL.md README.md LICENSE SECURITY.md update-manifest.json scripts references; do
    if [[ -e "$src/$item" ]]; then
      rm -rf "$dest/$item"
      cp -a "$src/$item" "$dest/"
      if [[ -d "$src/$item" ]]; then
        count=$((count + $(find "$src/$item" -type f | wc -l | tr -d ' ')))
      else
        count=$((count + 1))
      fi
    fi
  done
  _log_step "Copied ${count} file(s) to $dest"
}

install_outreachmagic() {
  local tmp=""
  local skill_src=""
  if [[ $LOCAL -eq 1 ]]; then
    skill_src="$_here/skills/outreachmagic"
    if [[ ! -d "$skill_src/scripts" ]]; then
      echo "error: --local requires skills/outreachmagic/scripts under $_here" >&2
      exit 1
    fi
  else
    tmp="$(mktemp -d -t om-install-XXXXXX)"
    trap 'rm -rf "$tmp"' RETURN
    _log_step "Cloning outreachmagic from $OM_REPO${OM_TAG:+ @ $OM_TAG} (may take 15-30s)"
    clone_repo "$OM_REPO" "$OM_TAG" "$tmp"
    skill_src="$tmp/skills/outreachmagic"
    if [[ ! -d "$skill_src/scripts" ]]; then
      echo "error: expected skills/outreachmagic/scripts in release checkout" >&2
      exit 1
    fi
  fi

  _log_step "Installing outreachmagic to $SKILLS_DIR/outreachmagic"
  copy_skill_tree "$skill_src" "$SKILLS_DIR/outreachmagic"

  if [[ "$PLATFORM" == "cursor" ]]; then
    local mdc="$_here/platforms/overlays/cursor/outreachmagic.mdc"
    if [[ -f "$mdc" ]]; then
      cp -a "$mdc" "$SKILLS_DIR/outreachmagic/outreachmagic.mdc"
    fi
  fi

  if [[ "$PLATFORM" == "claude" ]]; then
    local snippet="$_here/platforms/overlays/claude/CLAUDE_SNIPPET.md"
    if [[ -f "$snippet" ]]; then
      cp -a "$snippet" "$SKILLS_DIR/outreachmagic/CLAUDE_SNIPPET.md"
    fi
  fi

  chmod +x "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" 2>/dev/null || true
  _log_step "Initializing database..."
  local tag_label="${OM_TAG:-main}"
  python3 "$SKILLS_DIR/outreachmagic/scripts/pipeline.py" init --from-tag "$tag_label"
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

if [[ "$PLATFORM" == "hermes" ]] && [[ $NO_PROFILES -eq 0 ]] && [[ $ALL_PROFILES -eq 0 ]] && [[ ${#PROFILES[@]} -eq 0 ]]; then
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
if [[ $WITH_EMAIL_FINDER -eq 1 ]]; then
  install_email_finder
fi
if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -eq 0 ]] && [[ $ALL_PROFILES -eq 1 ]]; then
  while IFS= read -r profile; do
    PROFILES+=("$profile")
  done < <(discover_profiles)
fi

if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -gt 0 ]]; then
  echo "→ Profile symlinks"
  link_profiles outreachmagic "${PROFILES[@]}"
  if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
    link_profiles lead-enrich "${PROFILES[@]}"
  fi
  if [[ $WITH_EMAIL_FINDER -eq 1 ]]; then
    link_profiles email-finder "${PROFILES[@]}"
  fi
fi

echo ""
echo "✓ Installation complete. Login step is ready — ask your agent to connect."
echo "  outreachmagic: $SKILLS_DIR/outreachmagic"
if [[ $WITH_LEAD_ENRICH -eq 1 ]]; then
  echo "  lead-enrich:   $SKILLS_DIR/lead-enrich"
fi
if [[ $WITH_EMAIL_FINDER -eq 1 ]]; then
  echo "  email-finder:  $SKILLS_DIR/email-finder"
fi
echo "  Paths:   python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py paths"
if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -eq 0 ]]; then
  echo ""
  echo "  New Hermes profile:"
  echo "    bash install.sh --platform hermes --profile <name>"
fi
