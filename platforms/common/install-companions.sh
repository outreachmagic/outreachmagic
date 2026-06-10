# Shared companion install helpers for Hermes / Cursor / Claude Code install.sh.
# Caller must set: SKILLS_DIR, LE_REPO, EF_REPO, LE_TAG, EF_TAG, _here (repo root)

clone_companion_repo() {
  local repo=$1
  local tag=$2
  local dest=$3
  rm -rf "$dest"
  mkdir -p "$dest"
  if [[ -n "$tag" ]]; then
    git clone --depth 1 --progress --branch "$tag" "$repo" "$dest"
  else
    git clone --depth 1 --progress "$repo" "$dest"
  fi
}

_copy_companion_skill() {
  local name=$1
  local src=$2
  mkdir -p "$SKILLS_DIR/$name"
  for item in SKILL.md README.md SECURITY.md config.example.json default.env .gitignore requirements.txt references scripts update-manifest.json; do
    if [[ -e "$src/$item" ]]; then
      rm -rf "$SKILLS_DIR/$name/$item"
      cp -a "$src/$item" "$SKILLS_DIR/$name/"
    fi
  done
}

_suite_py() {
  [[ -n "${_here:-}" && -f "${_here}/scripts/skill_suite.py" ]] || return 1
  printf '%s' "${_here}/scripts/skill_suite.py"
}

_read_install_required() {
  local skill="$1" py
  py="$(_suite_py)" || return 1
  python3 "$py" install-required "$skill"
}

_read_suite_install_tag() {
  local skill="$1" py
  py="$(_suite_py)" || return 1
  python3 "$py" install-tag "$skill"
}

_verify_companion_install() {
  local name="$1" skill="$2" tag_opt="$3" tag_val="$4" py req
  py="$(_suite_py)"
  if [[ -z "$py" ]]; then
    echo "error: $name install check requires ${_here:-repo}/scripts/skill_suite.py" >&2
    return 1
  fi
  if [[ -z "$tag_val" ]]; then
    tag_val="$(python3 "$py" install-tag "$skill")"
  fi
  while IFS= read -r req; do
    [[ -n "$req" ]] || continue
    if [[ ! -f "$SKILLS_DIR/$name/$req" ]]; then
      echo "error: $name install incomplete — missing $req (try $tag_opt $tag_val)" >&2
      return 1
    fi
  done < <(_read_install_required "$skill")
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
  _verify_companion_install lead-enrich lead-enrich --lead-enrich-tag "$LE_TAG"
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
  _verify_companion_install email-finder email-finder --email-finder-tag "$EF_TAG"
  if [[ -n "${TRYKITT_ENV_FILE:-}" && -n "${TRYKITT_API_KEY:-}" ]]; then
    touch "$TRYKITT_ENV_FILE"
    if ! grep -q '^TRYKITT_API_KEY=' "$TRYKITT_ENV_FILE" 2>/dev/null; then
      echo "TRYKITT_API_KEY=${TRYKITT_API_KEY}" >> "$TRYKITT_ENV_FILE"
      echo "  → Added TRYKITT_API_KEY to $TRYKITT_ENV_FILE"
    fi
  fi
}
