#!/usr/bin/env bash
# CI helper: verify VERSION matches an expected tag (without v prefix).
set -euo pipefail

TAG="${1:?Usage: verify-release-tag.sh vX.Y.Z}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${TAG#v}"
FILE_VERSION="$(tr -d '[:space:]' < "$ROOT/skills/outreachmagic/scripts/VERSION")"

if [[ "$FILE_VERSION" != "$VERSION" ]]; then
  echo "error: tag ${TAG} does not match skills/outreachmagic/scripts/VERSION (${FILE_VERSION})" >&2
  exit 1
fi

PY="${PYTHON:-python3}"

"$PY" "$ROOT/scripts/generate_skill_manifest.py" --all
git diff --exit-code \
  "$ROOT/skills/outreachmagic/update-manifest.json"

"$PY" "$ROOT/tests/test_pipeline_import_smoke.py"

echo "Release tag ${TAG} matches VERSION ${FILE_VERSION} and manifest is current."
