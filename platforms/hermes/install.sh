#!/usr/bin/env bash
# Hermes install wrapper — delegates to repo-root install.sh
set -euo pipefail
exec "$(cd "$(dirname "$0")/../.." && pwd)/install.sh" --platform hermes "$@"
