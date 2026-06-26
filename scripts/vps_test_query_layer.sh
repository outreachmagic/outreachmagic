#!/usr/bin/env bash
# Smoke-test query layer on a Hermes VPS after rsync/deploy.
# Usage: bash scripts/vps_test_query_layer.sh [workspace_slug]
set -euo pipefail
WS="${1:-popcam}"
OM="${OUTREACHMAGIC_SCRIPTS:-$HOME/.hermes/skills/outreachmagic/scripts}"
PY="${PYTHON:-python3}"

echo "== version =="
"$PY" "$OM/pipeline.py" version

echo "== query engagement (48h) =="
/usr/bin/time -p "$PY" "$OM/pipeline.py" query engagement --workspace "$WS" --since 48h --json \
  | "$PY" -c "import sys,json; d=json.load(sys.stdin); print('preset',d.get('preset'),'rows',d['row_count'],'elapsed_ms',d.get('elapsed_ms'))"

echo "== view check =="
"$PY" -c "
import sqlite3, os
db = os.path.expanduser('~/.hermes/skills/outreachmagic/databases/outreachmagic.db')
c = sqlite3.connect(db)
r = c.execute(\"SELECT name FROM sqlite_master WHERE type='view' AND name='v_inbound_events_by_campaign'\").fetchone()
print('view ok' if r else 'view MISSING — run pull or init to migrate')
"

echo "OK"
