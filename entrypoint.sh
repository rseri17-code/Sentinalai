#!/bin/sh
set -e

# SentinalAI unified entrypoint
# Starts the OTEL collector (if configured) and the agent in a single container.

# --- Start OTEL Collector in the background (only if Splunk HEC is configured) ---
if [ -n "$SPLUNK_HEC_TOKEN" ] && [ -n "$SPLUNK_HEC_ENDPOINT" ]; then
    echo "[entrypoint] Starting OpenTelemetry Collector..."
    /otel/otelcol-contrib --config=/otel/config.yaml &
    OTEL_PID=$!

    # Point the agent SDK at the local collector (unless user overrode it)
    export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
else
    echo "[entrypoint] SPLUNK_HEC_TOKEN or SPLUNK_HEC_ENDPOINT not set — OTEL collector disabled."
    OTEL_PID=""
fi

# --- Graceful shutdown: forward signals to both processes ---
cleanup() {
    echo "[entrypoint] Shutting down..."
    [ -n "$OTEL_PID" ] && kill "$OTEL_PID" 2>/dev/null || true
    kill "$AGENT_PID" 2>/dev/null || true
    wait
}
trap cleanup TERM INT

# --- Start the SentinalAI agent ---
echo "[entrypoint] Starting SentinalAI agent..."
python -m agentcore_runtime &
AGENT_PID=$!

# Wait for either process to exit
wait -n 2>/dev/null || wait $AGENT_PID
cleanup
