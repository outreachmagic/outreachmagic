#!/usr/bin/env bash
# Install .githooks as the active hooks directory for this repo.
#
# This tells git to use the version-controlled hooks in .githooks/ instead of
# the (untracked) .git/hooks/ directory.  Run once per clone.
#
# Usage:  bash scripts/setup-hooks.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_PATH=".githooks"

cd "$REPO_ROOT"

current="$(git config core.hooksPath || true)"

if [ "$current" = "$HOOKS_PATH" ]; then
    echo "✔ hooks already configured: core.hooksPath = $HOOKS_PATH"
else
    git config core.hooksPath "$HOOKS_PATH"
    echo "✔ git config core.hooksPath = $HOOKS_PATH"
    echo "  Active hooks:"
    for hook in "$HOOKS_PATH"/*; do
        [ -x "$hook" ] && echo "   - $(basename "$hook")"
    done
fi
