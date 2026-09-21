FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "fastapi>=0.104.0,<1.0.0" "uvicorn>=0.24.0,<1.0.0"

COPY oss_validation_gateway/ /app/oss_validation_gateway/

RUN useradd -m -u 1000 ossgw && chown -R ossgw:ossgw /app
USER ossgw

EXPOSE 9080

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -f http://localhost:9080/health || exit 1

ENV OSS_GATEWAY_HOST=0.0.0.0
ENV OSS_GATEWAY_PORT=9080

CMD ["python", "-m", "oss_validation_gateway"]
