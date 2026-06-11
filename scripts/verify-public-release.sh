#!/usr/bin/env bash
# Post-publish gate: verify release assets and documented SHA256 install flow.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${OM_PUBLIC_REPO:-outreachmagic/outreachmagic}"

if [[ -n "${1:-}" ]]; then
  TAG="$1"
else
  TAG="v$(tr -d '[:space:]' < "$ROOT/skills/outreachmagic/scripts/VERSION")"
fi

echo "== verify-public-release: $TAG on $REPO =="

if ! command -v gh >/dev/null 2>&1; then
  echo "warning: gh not installed; skipping release view" >&2
else
  gh release view "$TAG" --repo "$REPO"
fi

INSTALL_DIR="$(mktemp -d -t om-verify-release-XXXXXX)"
trap 'rm -rf "$INSTALL_DIR"' EXIT

BASE="https://github.com/${REPO}/releases/download/${TAG}"
curl -fsSL "${BASE}/install.sh" -o "${INSTALL_DIR}/install.sh"
curl -fsSL "${BASE}/SHA256SUMS" -o "${INSTALL_DIR}/SHA256SUMS"

grep ' install.sh$' "${INSTALL_DIR}/SHA256SUMS" | (cd "${INSTALL_DIR}" && shasum -a 256 --check)

chmod +x "${INSTALL_DIR}/install.sh"
bash "${INSTALL_DIR}/install.sh" --dry-run --platform cursor --tag "${TAG}"

curl -fsSI "https://raw.githubusercontent.com/${REPO}/${TAG}/skills/outreachmagic/scripts/VERSION" | head -1
curl -fsSI "https://raw.githubusercontent.com/${REPO}/${TAG}/skills/outreachmagic/update-manifest.json" | head -1

echo "verify-public-release: PASS ($TAG)"
