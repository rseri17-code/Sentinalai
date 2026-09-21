# OSS validation compose stack

Bring up Prometheus, Loki, Alertmanager, a demo seed, and the AgentCore
name-shim. This is **not** production AgentCore.

```bash
docker compose -f deploy/oss-validation/docker-compose.yaml up --build
```

Then follow [`docs/clone/OSS_VALIDATION.md`](../../docs/clone/OSS_VALIDATION.md).
