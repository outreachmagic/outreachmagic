#!/usr/bin/env bash
# Build outreachmagic-skill release tarball + SHA256 sidecar for GitHub Releases.
set -euo pipefail

TAG="${1:?Usage: build-release.sh vX.Y.Z}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
VERSION="${TAG#v}"
VERSION_FILE="$ROOT/skills/outreachmagic/scripts/VERSION"

if [[ ! -f "$VERSION_FILE" ]]; then
  echo "error: missing $VERSION_FILE" >&2
  exit 1
fi

FILE_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [[ "$FILE_VERSION" != "$VERSION" ]]; then
  echo "error: tag $TAG does not match VERSION file ($FILE_VERSION)" >&2
  exit 1
fi

mkdir -p "$DIST"
ARCHIVE="$DIST/outreachmagic-skill-${VERSION}.tar.gz"

tar -czf "$ARCHIVE" -C "$ROOT/skills" outreachmagic
(
  cd "$DIST"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256"
  else
    sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256"
  fi
)

cp "$ROOT/skills/outreachmagic/update-manifest.json" "$DIST/update-manifest.json"
cp "$ROOT/install.sh" "$DIST/install.sh"
(
  cd "$DIST"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 install.sh > SHA256SUMS
  else
    sha256sum install.sh > SHA256SUMS
  fi
)

echo "Built $ARCHIVE"
echo "Checksum: $(cat "${ARCHIVE}.sha256")"
