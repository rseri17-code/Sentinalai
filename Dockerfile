FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements-agentcore.txt requirements-agentcore.txt
RUN pip install --no-cache-dir -r requirements-agentcore.txt

# Create non-root user (AgentCore best practice)
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

# Copy application code
COPY supervisor/ supervisor/
COPY workers/ workers/
COPY database/ database/
COPY agentcore_runtime.py .

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

CMD ["python", "-m", "agentcore_runtime"]
