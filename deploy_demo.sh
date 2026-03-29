#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SentinalAI — One-command public demo deployment
#
# Requirements on YOUR machine:
#   - Docker (https://docs.docker.com/get-docker/)
#   - ngrok account (free) — https://ngrok.com  OR
#     cloudflared (no account) — https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/local/
#
# Usage:
#   chmod +x deploy_demo.sh
#   ./deploy_demo.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

CONTAINER_NAME="sentinalai-demo"
IMAGE_NAME="sentinalai-demo:latest"
PORT=8081

# Generate random secrets if not set
export AGUI_SESSION_SECRET="${AGUI_SESSION_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)}"
export AGUI_INVITE_SECRET="${AGUI_INVITE_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              SentinalAI Demo Launcher                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Build Docker image ────────────────────────────────────────────────
echo "▶ Building Docker image (this takes ~2 minutes first time)..."
docker build -f Dockerfile.demo -t "$IMAGE_NAME" . --quiet
echo "  ✓ Image built"

# ── Step 2: Stop existing container ──────────────────────────────────────────
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# ── Step 3: Start container ───────────────────────────────────────────────────
echo "▶ Starting container..."
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "${PORT}:8081" \
  -e AGUI_AUTH_REQUIRED=false \
  -e AGUI_HONEYPOT=true \
  -e AGUI_SESSION_SECRET="$AGUI_SESSION_SECRET" \
  -e AGUI_INVITE_SECRET="$AGUI_INVITE_SECRET" \
  "$IMAGE_NAME"

# Wait for health check
echo -n "  Waiting for server"
for i in $(seq 1 30); do
  sleep 1
  if curl -sf http://localhost:${PORT}/ping >/dev/null 2>&1; then
    echo " ✓"
    break
  fi
  echo -n "."
done

# ── Step 4: Generate invite token ─────────────────────────────────────────────
INVITE_TOKEN=$(python3 -c "
import os, time, hmac, hashlib
secret = os.environ['AGUI_INVITE_SECRET']
ts = str(int(time.time()))
payload = f'invite:{ts}'
sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
print(f'invite:{ts}:{sig}')
")

echo ""
echo "  ✓ Server running on http://localhost:${PORT}"

# ── Step 5: Start tunnel ──────────────────────────────────────────────────────
echo ""
echo "▶ Starting public tunnel..."
echo ""

# Try cloudflared first (no account needed)
if command -v cloudflared &>/dev/null; then
  echo "  Using cloudflared..."
  cloudflared tunnel --url "http://localhost:${PORT}" --no-autoupdate &
  TUNNEL_PID=$!
  sleep 6
  PUBLIC_URL=$(cloudflared tunnel info 2>/dev/null | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1)

# Then try ngrok
elif command -v ngrok &>/dev/null; then
  echo "  Using ngrok..."
  ngrok http "${PORT}" --log=stdout --log-format=json &
  TUNNEL_PID=$!
  sleep 4
  PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get('tunnels', []):
    if t.get('proto') == 'https':
        print(t['public_url'])
        break
" 2>/dev/null)

else
  echo "  ⚠ No tunnel tool found."
  echo "  Install one:"
  echo "    cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/"
  echo "    ngrok:       https://ngrok.com/download"
  PUBLIC_URL="http://localhost:${PORT}"
fi

# ── Print final summary ───────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                     SentinalAI Demo is LIVE                         ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║                                                                      ║"
printf "║  %-70s║\n" "Share this invite link with your audience:"
echo "║                                                                      ║"
printf "║  %-70s║\n" "  ${PUBLIC_URL}/?invite=${INVITE_TOKEN}"
echo "║                                                                      ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║  Unauthorized visitors: convincing fake dashboard (honeypot)         ║"
echo "║  Authorized visitors:   full live SentinalAI experience              ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
printf "║  Self-learning panel : %-47s║\n" "${PUBLIC_URL}/demo"
printf "║  API docs            : %-47s║\n" "${PUBLIC_URL}/api/docs"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
echo "To stop:  docker rm -f ${CONTAINER_NAME}"
echo "Logs:     docker logs -f ${CONTAINER_NAME}"
echo ""
echo "Invite token (valid 7 days):"
echo "  ${INVITE_TOKEN}"
echo ""

# Keep script alive (tunnel dies if script exits)
if [ -n "${TUNNEL_PID:-}" ]; then
  wait $TUNNEL_PID
fi
