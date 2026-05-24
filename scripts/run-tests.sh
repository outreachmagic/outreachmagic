#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/skills/outreachmagic/scripts"
python3 test_workspace_routing.py
