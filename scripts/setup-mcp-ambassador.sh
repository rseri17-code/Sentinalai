#!/usr/bin/env bash
# =============================================================================
# scripts/setup-mcp-ambassador.sh
# One-time host setup for the MCP Ambassador Docker service.
#
# Creates mcp-ambassador/{data,cache} with correct permissions before the
# first `docker compose up`.  Run once from the repo root:
#
#   bash scripts/setup-mcp-ambassador.sh
#
# The ambassador container runs as UID 1000 (user mcpambassador).
# If your host user is also UID 1000, ownership is set automatically.
# Otherwise follow the sudo instructions printed by the script.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="$REPO_ROOT/mcp-ambassador/data"
CACHE_DIR="$REPO_ROOT/mcp-ambassador/cache"
CONTAINER_UID=1000

echo "==> Creating MCP Ambassador runtime directories..."
mkdir -p "$DATA_DIR"
mkdir -p "$CACHE_DIR"

# data/ is sensitive (TLS certs, SQLite DB, audit logs) — owner-only access
chmod 700 "$DATA_DIR"
# cache/ needs write access for npm/npx
chmod 755 "$CACHE_DIR"

CURRENT_UID="$(id -u)"
if [ "$CURRENT_UID" -eq "$CONTAINER_UID" ]; then
    chown "$CONTAINER_UID" "$DATA_DIR" "$CACHE_DIR"
    echo "    Ownership set to UID $CONTAINER_UID."
else
    echo ""
    echo "  NOTE: Your UID ($CURRENT_UID) differs from the container UID ($CONTAINER_UID)."
    echo "  Run the following as root so the container can write to these dirs:"
    echo ""
    echo "    sudo chown $CONTAINER_UID \"$DATA_DIR\" \"$CACHE_DIR\""
    echo ""
fi

echo ""
echo "==> Next steps:"
echo "  1. Copy and fill in environment values:"
echo "       cp .env.template .env"
echo "  2. Set AMBASSADOR_ADMIN_KEY in .env:"
echo "       node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\""
echo "  3. Start the stack:"
echo "       docker compose up --build"
echo ""
