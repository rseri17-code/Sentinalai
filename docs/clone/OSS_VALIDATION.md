# OSS validation gateway (clone-facing)

Minimal live MCP path against **open-source backends** (Prometheus, Loki,
Alertmanager, optional Kubernetes). This is **not** production AgentCore and
does **not** replace `GATEWAY_MODE=stub` + `INC12345`.

Pointing `MCP_GATEWAY_URL` at Grafana MCP (or Prometheus/Loki HTTP) does
**not** work: Sentinal workers call AgentCore-shaped names
(`SplunkTarget___search_oneshot`). This shim translates those names and
returns JSON shapes `McpGateway` / workers already tolerate.

| What | Stubs (`GATEWAY_MODE=stub`) | This path (`GATEWAY_MODE=live`) |
|---|---|---|
| Network | none (in-process fixtures) | streamable HTTP MCP → this shim → OSS HTTP APIs |
| Incident id | `INC12345` (fixture) | `INC-OSS-001` (seeded Alertmanager alert) |
| Logs | canned Splunk-shaped dict | Loki LogQL → Splunk-shaped `logs.results` |
| Metrics | empty/canned Sysdig dict | Prometheus PromQL → `metrics.metrics` |
| Ops / alerts | canned Moogsoft dict | Alertmanager `/api/v2/alerts` → `incident` |
| APM golden signals | empty Dynatrace stub | Prometheus demo gauges (p95 vs baseline) |
| ServiceNow / Confluence / GitHub | empty stubs | **skipped** (honest empty / no fake PRs) |
| Kubernetes writes | stub success | **disabled** unless `OSS_KUBE_MUTATIONS=true` (still not a full kubectl MCP) |

## Bring up the stack

From the repo root (Docker required):

```bash
docker compose -f deploy/oss-validation/docker-compose.yaml up --build
```

Wait until `oss-gateway` is healthy and Prometheus has scraped `seed:9100`
(~10s). Optional Grafana:

```bash
docker compose -f deploy/oss-validation/docker-compose.yaml --profile grafana up --build
```

Ports: shim `9080`, Prometheus `9090`, Loki `3100`, Alertmanager `9093`,
seed metrics `9100`, Grafana `3000` (profile).

## Point Sentinal at the shim

```bash
set -a && source .env.oss-validation.example && set +a
# GATEWAY_MODE=live
# MCP_GATEWAY_URL=http://127.0.0.1:9080/mcp
```

`mcp` + `strands-agents` must be installed (see `pyproject.toml`). Without
them, `McpGateway` cannot speak streamable HTTP and falls back to stubs.

Live vs stub is **`GATEWAY_MODE`**, not merely a URL. Compose defaults to
`stub` even when `AGENTCORE_GATEWAY_URL` is set. You must export
`GATEWAY_MODE=live` in the shell that runs the investigation.

Target names default to `MoogsoftTarget` / `SplunkTarget` / `SysdigTarget` /
… — the same strings `workers/mcp_client.py` already rewrites. Override with
`AGENTCORE_TARGET_*` only if you changed them; the shim honors the same env
vars.

## Run an investigation

```bash
python scripts/run_investigation.py INC-OSS-001 -v
```

That id is the `incident_id` label on the seeded Alertmanager alert
(`payment-service` request **timeout** so classification picks the timeout
playbook). Seed logs include `timeout waiting for connection: payment-db` so
log extraction sees a downstream service.

AGUI: start the BFF with the same `GATEWAY_MODE` / `MCP_GATEWAY_URL` exports
and investigate `INC-OSS-001` from the workspace. Do not leave
`GATEWAY_MODE=stub` in `docker-compose.yml` / `docker-compose.agui.yaml`.

### Optional YAML playbook (aliases)

Hardcoded `INCIDENT_PLAYBOOKS` already hit logs + APM + metrics; you do
**not** need YAML for the path above. To exercise `loki` / `prometheus`
aliases:

```bash
export YAML_PLAYBOOKS_ENABLED=true
export PLAYBOOKS_DIR=deploy/oss-validation/playbooks
python scripts/run_investigation.py INC-OSS-001
```

Default `YAML_PLAYBOOKS_ENABLED=false` is unchanged. Do not point
`PLAYBOOKS_DIR` at this folder in tests that expect `config/playbooks`.

## What the shim covers

| Internal server | Gateway tool (default) | OSS backend |
|---|---|---|
| moogsoft | `MoogsoftTarget___get_incident_by_id` (and list/alerts) | Alertmanager |
| splunk | `SplunkTarget___search_oneshot` / `search_export` | Loki |
| splunk | `SplunkTarget___get_change_data` | empty `changes: []` (no change feed) |
| sysdig | `SysdigTarget___query_metrics` / `golden_signals` / `get_events` | Prometheus / Alertmanager |
| dynatrace / signalfx | `…___get_metrics` / `query_signalfx_metrics` | Prometheus golden signals (so timeout playbooks do not go empty) |
| kubernetes | `KubernetesTarget___get_deployment_status` / `get_pod_logs` | kube API when `KUBERNETES_API_URL` + token are set |
| github / servicenow / confluence | listed in `tools/list` | skipped — see limitations |

Protocol: JSON-RPC Streamable HTTP on `POST /mcp` (also `POST /`). This is
the transport `McpGateway._get_mcp_client` uses
(`mcp.client.streamable_http.streamablehttp_client`). `GET /health` is for
compose, not for workers.

## Limitations (honest)

- Not production AgentCore (no OAuth2 audience mapping, no Lambda targets, no
  Bedrock gateway).
- Operation coverage is the subset timeout/error playbooks actually call, plus
  list/alerts and read-only Kubernetes. Other ops return empty skip payloads.
- ServiceNow / Confluence stay skip/empty. GitHub never fabricates a PR.
- Alertmanager is not Moogsoft: no situation room, no closed-incident history
  beyond what AM still holds, no correlation engine.
- Kubernetes mutations default **off**. Even when `OSS_KUBE_MUTATIONS=true`,
  rollback/scale are not implemented (use a real k8s MCP for that).
- Demo metrics are gauges on the seed exporter (`demo_latency_p95_ms`, …),
  not a production histogram/recording-rule layout.
- No live network in CI. Unit tests fake HTTP. Compose e2e is manual:
  `OSS_VALIDATION_E2E=1` (see `tests/test_oss_validation_gateway_e2e.py`).

## Files

- `oss_validation_gateway/` — FastAPI MCP shim
- `deploy/oss-validation/` — compose, seed, optional playbook
- `.env.oss-validation.example` — live env template
- `tests/test_oss_validation_gateway.py` — name mapping + shaping (offline)
