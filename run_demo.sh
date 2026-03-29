#!/usr/bin/env bash
# SentinalAI Demo Server
# Starts the BFF on port 8081 with auth disabled for easy local demo.
# Access the dashboard at: http://localhost:8081/demo

set -e

export AGUI_AUTH_REQUIRED=false
export AGUI_BFF_PORT=8081
export ONLINE_EVAL_ENABLED=true
export STRATEGY_EVOLVER_ENABLED=true
export SELF_CRITIQUE_ENABLED=true

echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║          SentinalAI Self-Learning Demo            ║"
echo "╠═══════════════════════════════════════════════════╣"
echo "║  Dashboard : http://localhost:8081/demo           ║"
echo "║  API Docs  : http://localhost:8081/api/docs       ║"
echo "║  Health    : http://localhost:8081/ping           ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

python -m uvicorn agui.main:app --host 0.0.0.0 --port 8081 --reload
