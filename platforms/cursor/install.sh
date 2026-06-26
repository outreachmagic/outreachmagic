#!/usr/bin/env bash
# Cursor install wrapper — delegates to repo-root install.sh
set -euo pipefail
exec "$(cd "$(dirname "$0")/../.." && pwd)/install.sh" --platform cursor "$@"
