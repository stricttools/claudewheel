#!/usr/bin/env bash
# Post-release hook. Runs after a successful release (non-fatal).
# Environment: RLSBL_VERSION is set to the released version.
# Customize this for your project (e.g., local install, deploy, notify).

set -euo pipefail

echo "Post-release: v$RLSBL_VERSION"

if [ -f ~/Projects/.env ]; then
  set -a && source ~/Projects/.env && set +a
  export CLOUDFLARE_API_TOKEN="${CF_PAGES_API_TOKEN:-}"
  export CLOUDFLARE_ACCOUNT_ID="${CF_ACCOUNT_ID:-}"
fi
echo "Building and deploying docs..."
selfdoc build && selfdoc deploy
