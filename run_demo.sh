#!/usr/bin/env bash
# SentinalAI Demo Server
# ─────────────────────────────────────────────────────────────────────────────
# Starts the full-stack BFF + React UI on port 8081.
#
# Access:
#   Real dashboard  : http://localhost:8081/?invite=<TOKEN>
#   Self-learning   : http://localhost:8081/demo
#   API docs        : http://localhost:8081/api/docs
#   Health          : http://localhost:8081/ping
#
# Anyone without a valid invite token sees a convincing fake dashboard.
#
# To generate an invite link (after server starts):
#   curl -s -X POST "http://localhost:8081/api/v1/admin/invite" \
#        -H "Authorization: Bearer $(python scripts/gen_token.py admin)"
#
# Or set AGUI_INVITE_TOKENS to a comma-separated list of static tokens.
# ─────────────────────────────────────────────────────────────────────────────
set -e

# ── Build React UI if dist is missing ────────────────────────────────────────
if [ ! -d "ui/dist" ]; then
  echo "Building React UI..."
  cd ui && npm install --silent && npx vite build && cd ..
fi

# ── Config ────────────────────────────────────────────────────────────────────
export AGUI_AUTH_REQUIRED=false          # API auth (JWT) — off for local demo
export AGUI_HONEYPOT=true               # Gate: invite tokens required
export AGUI_BFF_PORT=8081
export ONLINE_EVAL_ENABLED=true
export STRATEGY_EVOLVER_ENABLED=true
export SELF_CRITIQUE_ENABLED=true

# Generate secrets if not set
export AGUI_SESSION_SECRET="${AGUI_SESSION_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export AGUI_INVITE_SECRET="${AGUI_INVITE_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

# ── Print invite link ─────────────────────────────────────────────────────────
HOST="${AGUI_PUBLIC_URL:-http://localhost:8081}"
INVITE_TOKEN=$(python3 -c "
import sys, os, time, hmac, hashlib
secret = os.environ['AGUI_INVITE_SECRET']
ts = str(int(time.time()))
payload = f'invite:{ts}'
sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
print(f'invite:{ts}:{sig}')
")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              SentinalAI Self-Learning Demo                   ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Invite link  : %-47s║\n" "${HOST}/?invite=${INVITE_TOKEN:0:20}..."
echo "║                                                              ║"
printf "║  Full URL     : %-47s║\n" "${HOST}/?invite=${INVITE_TOKEN}"
echo "║                                                              ║"
echo "║  Self-learning: ${HOST}/demo"
echo "║  API docs     : ${HOST}/api/docs"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Unauthorized visitors see: convincing fake dashboard        ║"
echo "║  Authorized visitors see:  real live investigations          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Share this link with your audience:"
echo ""
echo "  ${HOST}/?invite=${INVITE_TOKEN}"
echo ""

# ── Start server ──────────────────────────────────────────────────────────────
python -m uvicorn agui.main:app --host 0.0.0.0 --port "${AGUI_BFF_PORT}" --reload
