###############################################################################
# SentinalAI — single unified image
# Includes: Python agent + OpenTelemetry Collector
#
# Usage:
#   cp .env.template .env   # fill in your values
#   docker compose up --build
#
# The OTEL collector starts automatically when SPLUNK_HEC_TOKEN and
# SPLUNK_HEC_ENDPOINT are set.  Otherwise it is skipped gracefully.
###############################################################################

# --- Stage 1: grab the OTEL collector binary ---
FROM otel/opentelemetry-collector-contrib:0.146.0 AS otel-collector

# --- Stage 2: build the final image ---
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements-agentcore.txt requirements-agentcore.txt
RUN pip install --no-cache-dir -r requirements-agentcore.txt

# Copy OTEL collector binary + config into /otel/
COPY --from=otel-collector /otelcol-contrib /otel/otelcol-contrib
COPY otel-collector-config.yaml /otel/config.yaml

# Create non-root user (AgentCore best practice)
RUN useradd -m -u 1000 bedrock_agentcore \
    && chown -R bedrock_agentcore:bedrock_agentcore /otel

# Copy application code
COPY supervisor/ supervisor/
COPY workers/ workers/
COPY database/ database/
COPY knowledge/ knowledge/
COPY agentcore_runtime.py .

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER bedrock_agentcore

EXPOSE 8080
# OTEL collector OTLP HTTP (internal, not usually published)
EXPOSE 4318

# Health check — agent /ping endpoint
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
