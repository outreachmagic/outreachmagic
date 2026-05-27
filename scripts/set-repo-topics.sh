#!/usr/bin/env bash
# Set GitHub topics on outreachmagic/hermes-outreachmagic (requires gh auth)
set -euo pipefail

TOPICS=(
  hermes-skill agent-skill agentskills cold-email outreach
  smartlead instantly lemlist claude-code sales-automation
  b2b-sales lead-generation mcp sqlite gtm
)

if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh CLI not installed" >&2
  exit 1
fi

gh repo edit outreachmagic/hermes-outreachmagic --add-topic "$(IFS=,; echo "${TOPICS[*]}")"
echo "Topics set: ${TOPICS[*]}"
