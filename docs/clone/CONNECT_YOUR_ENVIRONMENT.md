# Connect your environment (tool layer)

Clone-facing configuration for MCP tools and YAML playbooks. This is **not**
the investigation LLM / model layer (`LLM_ENABLED` / `LLM_PROVIDER`).

No code fork is required to point SentinalAI at your Moogsoft, Splunk,
ServiceNow, or other MCP targets.

## 1. Gateway URL

Clones should set:

```bash
export GATEWAY_MODE=live
export MCP_GATEWAY_URL=https://your-gateway.example/mcp
```

`MCP_GATEWAY_URL` is an alias for `AGENTCORE_GATEWAY_URL`. Either name works.
If both are set, `AGENTCORE_GATEWAY_URL` wins (existing compose / tests).

`GATEWAY_MODE=stub` (compose default) still forces in-process fixtures even when
a URL is set. See `.env.example`.

Compose files continue to set `AGENTCORE_GATEWAY_URL` — do not change them
unless you intend to.

## 2. MCP target mapping (`AGENTCORE_TARGET_*`)

Workers call dotted tool names (`splunk.search_oneshot`). `McpGateway` rewrites
those to AgentCore gateway names `{Target}___{operation}` using
`_SERVER_TO_TARGET` in `workers/mcp_client.py`.

Override the **target** string to match what your gateway registered — do not
rename workers or fork the client:

| MCP server (internal) | Env var | Default target |
|---|---|---|
| moogsoft | `AGENTCORE_TARGET_MOOGSOFT` | `MoogsoftTarget` |
| splunk | `AGENTCORE_TARGET_SPLUNK` | `SplunkTarget` |
| sysdig | `AGENTCORE_TARGET_SYSDIG` | `SysdigTarget` |
| signalfx | `AGENTCORE_TARGET_SIGNALFX` | `SignalFxTarget` |
| dynatrace | `AGENTCORE_TARGET_DYNATRACE` | `DynatraceTarget` |
| servicenow | `AGENTCORE_TARGET_SERVICENOW` | `ServiceNowTarget` |
| github | `AGENTCORE_TARGET_GITHUB` | `GitHubTarget` |
| confluence | `AGENTCORE_TARGET_CONFLUENCE` | `ConfluenceTarget` |
| kubernetes | `AGENTCORE_TARGET_KUBERNETES` | `KubernetesTarget` |

Example: your gateway registered Splunk as `AcmeLogs` instead of `SplunkTarget`:

```bash
export AGENTCORE_TARGET_SPLUNK=AcmeLogs
# splunk.search_oneshot → AcmeLogs___search_oneshot
```

Auth, rate limits, and stubs still go through `McpGateway.invoke()`. Python
workers do not read `SPLUNK_MCP_URL` / `SERVICENOW_MCP_URL` — those are for the
compose / agentcore-runtime sidecar.

## 3. YAML playbooks (flag-gated)

Hardcoded `INCIDENT_PLAYBOOKS` in `supervisor/tool_selector.py` is the source of
truth when the flag is off (default):

```bash
YAML_PLAYBOOKS_ENABLED=false   # identical to today
```

When on, `get_playbook()` loads `config/playbooks/*.yaml` (or `PLAYBOOKS_DIR`),
merges over the hardcoded types, and falls back to hardcoded on any load error:

```bash
export YAML_PLAYBOOKS_ENABLED=true
# export PLAYBOOKS_DIR=/etc/sentinalai/playbooks
```

Do not turn this on in tests that expect the hardcoded registry.

## 4. Vendor-neutral worker aliases

YAML `steps[].worker` may use a logical name. Aliases resolve **only** on the
YAML load path (`supervisor/playbook_loader.py`). Hardcoded playbooks are never
rewritten.

| Alias (examples) | Canonical worker | Typical MCP server |
|---|---|---|
| `logs`, `splunk`, `elk`, `loki` | `log_worker` | splunk |
| `metrics`, `prometheus`, `sysdig` | `metrics_worker` | sysdig |
| `apm`, `dynatrace`, `signalfx` | `apm_worker` | dynatrace / signalfx |
| `itsm`, `servicenow` | `itsm_worker` | servicenow |
| `ops`, `moogsoft` | `ops_worker` | moogsoft |
| `devops`, `github` | `devops_worker` | github |

Canonical names (`log_worker`, …) pass through. Unknown names are left as-is.

Checked-in playbooks already use canonical names — you do not need to rewrite
them. To add team-specific names, edit `config/worker_aliases.yaml` or set
`WORKER_ALIASES_PATH` to your overlay. That is a different layer from
`AGENTCORE_TARGET_*` (tool routing vs playbook step names).

## 5. OSS validation (Prometheus / Loki / Alertmanager)

`GATEWAY_MODE=live` still expects **AgentCore-shaped** tool names
(`SplunkTarget___search_oneshot`). Pointing `MCP_GATEWAY_URL` at Grafana MCP
or raw Prometheus/Loki HTTP does not work.

For a clone that wants to validate the live MCP path against open-source
backends (no Moogsoft/Splunk/Dynatrace), use the name-shim + compose stack:

- Guide: [`OSS_VALIDATION.md`](OSS_VALIDATION.md)
- Compose: `deploy/oss-validation/docker-compose.yaml`
- Env: `.env.oss-validation.example` (`GATEWAY_MODE=live`,
  `MCP_GATEWAY_URL=http://127.0.0.1:9080/mcp`)
- Demo incident: `INC-OSS-001` (not the stub `INC12345`)

Stubs remain the zero-infra proof. The OSS stack is **not** production
AgentCore; ServiceNow/Confluence/GitHub stay skip/empty.

## Files

- `workers/mcp_client.py` — URL alias, `AGENTCORE_TARGET_*`, `McpGateway.invoke()`
- `supervisor/playbook_loader.py` — YAML load + alias map
- `supervisor/tool_selector.py` — `YAML_PLAYBOOKS_ENABLED` gate, `INCIDENT_PLAYBOOKS`
- `config/playbooks/*.yaml` — optional playbook source
- `config/worker_aliases.yaml` — optional alias overlay
- `oss_validation_gateway/` — OSS MCP name-shim (see `OSS_VALIDATION.md`)
- `.env.example` — copy to `.env`
- `.env.oss-validation.example` — live OSS shim env
