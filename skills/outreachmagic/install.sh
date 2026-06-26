#!/usr/bin/env bash
#
# install.sh — Post-install setup for Outreach Magic
#
# Run this after `npx skills add outreachmagic/outreachmagic` to choose
# which agent tool you use.  This prevents duplicate databases by ensuring
# all skill copies share one data directory.
#
# Usage:
#   bash install.sh                    # interactive mode (recommended)
#   bash install.sh --agent cursor    # non-interactive mode
#
set -euo pipefail

SKILL_NAME="outreachmagic"
HOME_DIR="${HOME}"

# Known agent skill directories
declare -A AGENT_DIRS
AGENT_DIRS[cursor]="${HOME_DIR}/.cursor/skills/${SKILL_NAME}"
AGENT_DIRS[claude]="${HOME_DIR}/.claude/skills/${SKILL_NAME}"
AGENT_DIRS[hermes]="${HOME_DIR}/.hermes/skills/${SKILL_NAME}"

# ── Detect which agent directories have the skill installed ──

INSTALLED=()
for name in cursor agents claude hermes; do
    dir="${AGENT_DIRS[$name]}"
    if [[ -d "$dir" && -f "$dir/SKILL.md" ]]; then
        INSTALLED+=("$name")
    fi
done

if [[ ${#INSTALLED[@]} -eq 0 ]]; then
    echo "❌ Outreach Magic does not appear to be installed in any agent directory."
    echo "   Run \`npx skills add outreachmagic/outreachmagic\` first."
    exit 1
fi

# ── Parse --agent flag ──

AGENT_CHOICE=""
if [[ $# -ge 2 && "$1" == "--agent" ]]; then
    AGENT_CHOICE="$2"
    found=0
    for name in "${INSTALLED[@]}"; do
        if [[ "$name" == "$AGENT_CHOICE" ]]; then
            found=1
            break
        fi
    done
    if [[ $found -eq 0 ]]; then
        echo "❌ Agent \"$AGENT_CHOICE\" is not installed."
        echo "   Installed: ${INSTALLED[*]}"
        exit 1
    fi
fi

# ── Prompt user if not specified ──

if [[ -z "$AGENT_CHOICE" ]]; then
    if [[ ${#INSTALLED[@]} -eq 1 ]]; then
        AGENT_CHOICE="${INSTALLED[0]}"
        echo "📦 Outreach Magic is installed in one agent directory: ${AGENT_CHOICE}"
    else
        echo ""
        echo "📦 Outreach Magic is installed in multiple agent directories:"
        echo ""
        for i in "${!INSTALLED[@]}"; do
            idx=$((i + 1))
            echo "  $idx) ${INSTALLED[$i]}  (${AGENT_DIRS[${INSTALLED[$i]}]})"
        done
        echo ""
        while true; do
            read -rp "Which agent do you use? Enter a number (1-${#INSTALLED[@]}): " choice
            if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#INSTALLED[@]} )); then
                AGENT_CHOICE="${INSTALLED[$((choice - 1))]}"
                break
            fi
            echo "  Please enter a number between 1 and ${#INSTALLED[@]}."
        done
    fi
fi

CANONICAL_DIR="${AGENT_DIRS[$AGENT_CHOICE]}"
echo ""
echo "✓ Using ${AGENT_CHOICE}: ${CANONICAL_DIR}"

# ── Write data_root to every copy's config ──
# This ensures that running pipeline.py from ANY copy resolves to the same DB.

DATA_ROOT="${HOME_DIR}/.${AGENT_CHOICE}"
CONFIG_SUBDIR="config/outreachmagic_config.json"

for name in "${INSTALLED[@]}"; do
    dir="${AGENT_DIRS[$name]}"
    config_file="${dir}/${CONFIG_SUBDIR}"
    config_dir="$(dirname "$config_file")"
    mkdir -p "$config_dir"

    # Read existing config if it exists
    if [[ -f "$config_file" ]]; then
        # Use python3 to safely update JSON
        python3 -c "
import json
path = '$config_file'
try:
    with open(path) as f:
        cfg = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    cfg = {}
cfg['data_root'] = '$DATA_ROOT'
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
"
    else
        echo "{\"data_root\": \"$DATA_ROOT\"}" >"$config_file"
    fi
    chmod 600 "$config_file" 2>/dev/null || true
    echo "  ✓ data_root written to ${name} config"
done

# ── Offer to symlink other copies ──

if [[ ${#INSTALLED[@]} -gt 1 ]]; then
    echo ""
    echo "Other copies can be replaced with symlinks to share scripts too."
    SYMLINK_OTHERS=""
    while true; do
        read -rp "Replace other copies with symlinks? (y/n): " SYMLINK_OTHERS
        case "$SYMLINK_OTHERS" in
            [Yy]* ) SYMLINK_OTHERS="yes"; break;;
            [Nn]* ) SYMLINK_OTHERS="no"; break;;
            * ) echo "  Please answer y or n.";;
        esac
    done

    if [[ "$SYMLINK_OTHERS" == "yes" ]]; then
        for name in "${INSTALLED[@]}"; do
            if [[ "$name" == "$AGENT_CHOICE" ]]; then
                continue
            fi
            dir="${AGENT_DIRS[$name]}"
            echo "  Replacing ${name} copy with symlink..."
            rm -rf "$dir"
            ln -s "$CANONICAL_DIR" "$dir"
            echo "    ✓ ${dir} → ${CANONICAL_DIR}"
        done
    fi
fi

echo ""
echo "✅ Outreach Magic setup complete."
echo "   Run \`python3 ${CANONICAL_DIR}/scripts/pipeline.py init\` to create the database."
echo "   Or run \`python3 ${CANONICAL_DIR}/scripts/pipeline.py login\` to connect your account."
