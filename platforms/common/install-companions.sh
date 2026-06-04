# Shared companion install helpers for Hermes / Cursor / Claude Code install.sh.
# Caller must set: SKILLS_DIR, LE_REPO, EF_REPO, LE_TAG, EF_TAG

clone_companion_repo() {
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

_copy_companion_skill() {
  local name=$1
  local src=$2
  mkdir -p "$SKILLS_DIR/$name"
  for item in SKILL.md README.md SECURITY.md config.example.json default.env .gitignore references scripts update-manifest.json; do
    if [[ -e "$src/$item" ]]; then
      rm -rf "$SKILLS_DIR/$name/$item"
      cp -a "$src/$item" "$SKILLS_DIR/$name/"
    fi
  done
}

install_lead_enrich() {
  local tmp="" src=""
  if [[ "${LOCAL:-0}" -eq 1 && -n "${_here:-}" && -d "$_here/skills/lead-enrich/scripts" ]]; then
    src="$_here/skills/lead-enrich"
    echo "→ Installing lead-enrich (local) to $SKILLS_DIR/lead-enrich"
  else
    tmp="$(mktemp -d -t om-le-XXXXXX)"
    trap 'rm -rf "$tmp"' RETURN
    echo "→ Installing lead-enrich to $SKILLS_DIR/lead-enrich"
    clone_companion_repo "$LE_REPO" "$LE_TAG" "$tmp"
    src="$tmp"
  fi
  _copy_companion_skill lead-enrich "$src"
  chmod +x "$SKILLS_DIR/lead-enrich/scripts/enrich.py" 2>/dev/null || true
}

install_email_finder() {
  local tmp="" src=""
  if [[ "${LOCAL:-0}" -eq 1 && -n "${_here:-}" && -d "$_here/skills/email-finder/scripts" ]]; then
    src="$_here/skills/email-finder"
    echo "→ Installing email-finder (local) to $SKILLS_DIR/email-finder"
  else
    tmp="$(mktemp -d -t om-ef-XXXXXX)"
    trap 'rm -rf "$tmp"' RETURN
    echo "→ Installing email-finder to $SKILLS_DIR/email-finder"
    clone_companion_repo "$EF_REPO" "$EF_TAG" "$tmp"
    src="$tmp"
  fi
  _copy_companion_skill email-finder "$src"
  chmod +x "$SKILLS_DIR/email-finder/scripts/email_finder.py" 2>/dev/null || true
  if [[ -n "${TRYKITT_ENV_FILE:-}" && -n "${TRYKITT_API_KEY:-}" ]]; then
    touch "$TRYKITT_ENV_FILE"
    if ! grep -q '^TRYKITT_API_KEY=' "$TRYKITT_ENV_FILE" 2>/dev/null; then
      echo "TRYKITT_API_KEY=${TRYKITT_API_KEY}" >> "$TRYKITT_ENV_FILE"
      echo "  → Added TRYKITT_API_KEY to $TRYKITT_ENV_FILE"
    fi
  fi
}
