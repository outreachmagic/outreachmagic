#!/usr/bin/env bash
# Install Outreach Magic skill suite (outreachmagic + lead-enrich + email-finder).
set -euo pipefail

OM_REPO="https://github.com/outreachmagic/outreachmagic.git"
LE_REPO="https://github.com/outreachmagic/lead-enrich.git"
EF_REPO="https://github.com/outreachmagic/email-finder.git"

PLATFORM=""
ALL_PROFILES=0
NO_PROFILES=0
LOCAL=0
DRY_RUN=0
YES=0
UNINSTALL=0
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
    git -c advice.detachedHead=false clone --depth 1 --progress --branch "$tag" "$OM_REPO" "$tmp"
  else
    git -c advice.detachedHead=false clone --depth 1 --progress "$OM_REPO" "$tmp"
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

Installs outreachmagic, lead-enrich, and email-finder from github.com/outreachmagic/outreachmagic.
Hermes: real files under ~/.hermes/skills/; profiles get symlinks only.

Options:
  --platform NAME          Required: hermes, cursor, or claude
  --local                  Install from this repo checkout (dev) instead of cloning
  --no-profiles            Hermes only: skip profile symlinks
  --all-profiles           Hermes only: symlink all existing profiles (default when profiles/ exists)
  --profile NAME           Hermes only: symlink one profile (repeatable)
  --tag TAG                outreachmagic release tag (e.g. v1.38.7)
  --lead-enrich-tag TAG    lead-enrich release tag (default from skill-suite.json)
  --email-finder-tag TAG   email-finder release tag (default from skill-suite.json)
  --dry-run                Print planned actions without writing
  --yes                    Skip interactive prompts (non-interactive init)
  --uninstall              Remove installed skills for this platform
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --local) LOCAL=1; shift ;;
    --all-profiles) ALL_PROFILES=1; shift ;;
    --no-profiles) NO_PROFILES=1; shift ;;
    --profile) PROFILES+=("$2"); shift 2 ;;
    --tag) OM_TAG="$2"; shift 2 ;;
    --lead-enrich-tag) LE_TAG="$2"; shift 2 ;;
    --email-finder-tag) EF_TAG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --with-lead-enrich|--with-email-finder|--migrate|--migrate-hermes-profiles)
      echo "warning: $1 is no longer required (full suite always installs)" >&2
      shift
      ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$PLATFORM" ]]; then
  echo "error: --platform is required (hermes, cursor, or claude)" >&2
  usage
  exit 1
fi

_resolve_companion_tag() {
  local skill="$1"
  local tag=""
  tag="$(_read_suite_install_tag "$skill" 2>/dev/null || true)"
  if [[ -z "$tag" && -n "${_here:-}" && -f "$_here/skill-suite.json" ]]; then
    tag="$(python3 -c "
import json, sys
skill, path = sys.argv[1], sys.argv[2]
print(json.load(open(path, encoding='utf-8'))['skills'][skill]['install_default_tag'])
" "$skill" "$_here/skill-suite.json" 2>/dev/null || true)"
  fi
  if [[ -z "$tag" ]]; then
    echo "error: install tag for $skill not found (skill-suite.json install_default_tag)" >&2
    return 1
  fi
  printf '%s' "$tag"
}

LE_TAG="${LE_TAG:-$(_resolve_companion_tag lead-enrich)}"
EF_TAG="${EF_TAG:-$(_resolve_companion_tag email-finder)}"

if [[ -z "$OM_TAG" && $UNINSTALL -eq 0 ]]; then
  if [[ -f "$_here/skills/outreachmagic/scripts/VERSION" ]]; then
    OM_TAG="v$(tr -d '[:space:]' < "$_here/skills/outreachmagic/scripts/VERSION")"
  fi
fi

case "$PLATFORM" in
  hermes)
    HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
    SKILLS_DIR="$HERMES_HOME/skills"
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
    git -c advice.detachedHead=false clone --depth 1 --progress --branch "$tag" "$repo" "$dest"
  else
    git -c advice.detachedHead=false clone --depth 1 --progress "$repo" "$dest"
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

_plan_uninstall() {
  echo "[dry-run] Would remove:"
  echo "  $SKILLS_DIR/outreachmagic"
  echo "  $SKILLS_DIR/lead-enrich"
  echo "  $SKILLS_DIR/email-finder"
  if [[ "$PLATFORM" == "hermes" && ${#PROFILES[@]} -gt 0 ]]; then
    local profile skill
    for profile in "${PROFILES[@]}"; do
      for skill in outreachmagic lead-enrich email-finder; do
        echo "  $HERMES_HOME/profiles/$profile/skills/$skill (symlink)"
      done
    done
  fi
}

_do_uninstall() {
  local skill
  for skill in outreachmagic lead-enrich email-finder; do
    if [[ -d "$SKILLS_DIR/$skill" ]]; then
      _log_step "Removing $SKILLS_DIR/$skill"
      rm -rf "$SKILLS_DIR/$skill"
    fi
  done
  if [[ "$PLATFORM" == "hermes" ]]; then
    local profile
    for profile in "$HERMES_HOME/profiles"/*/; do
      [[ -d "$profile" ]] || continue
      for skill in outreachmagic lead-enrich email-finder; do
        local link="${profile}skills/$skill"
        if [[ -L "$link" ]]; then
          rm -f "$link"
        fi
      done
    done
  fi
  echo "✓ Uninstall complete for platform: $PLATFORM"
}

install_outreachmagic() {
  local tmp=""
  local skill_src=""
  if [[ $DRY_RUN -eq 1 ]]; then
    _log_step "[dry-run] Would install outreachmagic to $SKILLS_DIR/outreachmagic"
    _log_step "[dry-run] Source: ${OM_REPO}${OM_TAG:+ @ $OM_TAG}${LOCAL:+ (local checkout)}"
    _log_step "[dry-run] Would run: pipeline.py init --from-tag ${OM_TAG:-main}"
  fi
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

  if [[ $DRY_RUN -eq 1 ]]; then
    return 0
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
  local db_path="$SKILLS_DIR/outreachmagic/databases/outreachmagic.db"
  if [[ $YES -eq 0 && -t 0 ]]; then
    echo "Initialize local SQLite database at $db_path?"
    echo "  (Stores pipeline contacts and event history locally — no data sent to servers during init.)"
    read -r -p "Run pipeline.py init? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
      echo "Skipping init. Run later:"
      echo "  python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py init"
      return 0
    fi
  else
    _log_step "Initializing local SQLite database at $db_path"
    echo "  (Stores pipeline contacts and event history locally — no data sent to servers during init.)"
  fi
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
    echo "→ Removing stale profile path: $link"
    rm -rf "$link"
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

if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -eq 0 ]] && [[ $ALL_PROFILES -eq 1 ]]; then
  while IFS= read -r profile; do
    PROFILES+=("$profile")
  done < <(discover_profiles)
fi

mkdir -p "$SKILLS_DIR"

if [[ $UNINSTALL -eq 1 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    _plan_uninstall
    exit 0
  fi
  _do_uninstall
  exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "=== Outreach Magic install plan (dry-run) ==="
  echo "Platform:     $PLATFORM"
  echo "Skills dir:   $SKILLS_DIR"
  echo "outreachmagic tag: ${OM_TAG:-main}"
  echo "lead-enrich tag:   ${LE_TAG:-main}"
  echo "email-finder tag:  ${EF_TAG:-main}"
  install_outreachmagic
  _log_step "[dry-run] Would install lead-enrich to $SKILLS_DIR/lead-enrich"
  _log_step "[dry-run] Would install email-finder to $SKILLS_DIR/email-finder"
  if [[ "$PLATFORM" == "hermes" && ${#PROFILES[@]} -gt 0 ]]; then
    _log_step "[dry-run] Would link profiles: ${PROFILES[*]}"
  fi
  echo "=== End dry-run (no changes made) ==="
  exit 0
fi

install_outreachmagic
install_lead_enrich
install_email_finder

if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -gt 0 ]]; then
  echo "→ Profile symlinks"
  link_profiles outreachmagic "${PROFILES[@]}"
  link_profiles lead-enrich "${PROFILES[@]}"
  link_profiles email-finder "${PROFILES[@]}"
fi

echo ""
echo "✓ Installation complete. Login step is ready — ask your agent to connect."
echo "  outreachmagic: $SKILLS_DIR/outreachmagic"
echo "  lead-enrich:   $SKILLS_DIR/lead-enrich"
echo "  email-finder:  $SKILLS_DIR/email-finder"
echo "  Paths:   python3 $SKILLS_DIR/outreachmagic/scripts/pipeline.py paths"
if [[ "$PLATFORM" == "hermes" ]] && [[ ${#PROFILES[@]} -eq 0 ]]; then
  echo ""
  echo "  New Hermes profile:"
  echo "    bash install.sh --platform hermes --profile <name>"
fi
