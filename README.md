# SentinalAI

**Deterministic, proof-driven SRE agent for autonomous incident root cause analysis.**

SentinalAI investigates production incidents end-to-end: it fetches alert metadata from Moogsoft, gathers evidence from Splunk, Sysdig, Dynatrace, SignalFx, ServiceNow, and GitHub via MCP tool servers routed through the Bedrock AgentCore gateway, generates and scores multiple root-cause hypotheses against hard evidence, and produces a structured RCA with confidence scoring -- all within a bounded execution budget. No human in the loop. No LLM choosing tools. Every investigation is reproducible.

```
Incident In -> [Fetch] -> [Classify] -> [Playbook] -> [Multi-Hypothesis Scoring] -> [Evidence-Weighted RCA] -> Structured Result Out
```

---

## Table of Contents

- [Architecture](#architecture)
- [Investigation Pipeline](#investigation-pipeline)
- [Hypothesis Engine](#hypothesis-engine)
- [Guardrails](#guardrails)
- [Institutional Knowledge Layer](#institutional-knowledge-layer)
- [Observability](#observability)
- [Workers & MCP Integration](#workers--mcp-integration)
- [LLM Integration](#llm-integration)
- [Memory](#memory)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Testing](#testing)
- [Deployment](#deployment)
- [Project Structure](#project-structure)
- [Contributing](#contributing)

---

## Architecture

```
+------------------------------------------------------------------+
|                      SentinalAI Supervisor                        |
|                                                                   |
|  investigate(incident_id) -> dict                                 |
|                                                                   |
|  +----------+  +----------+  +-----------+  +------------------+  |
|  |  FETCH   |->| CLASSIFY |->| PLAYBOOK  |->|     ANALYZE      |  |
|  |          |  |          |  | (3-5 tool |  | Multi-Hypothesis  |  |
|  | Moogsoft |  | Keyword  |  |  calls)   |  | Evidence-Weighted |  |
|  | incident |  | matching |  |           |  | LLM refinement   |  |
|  +----------+  +----------+  +-----------+  +------------------+  |
|       |              |             |                |              |
|       v              v             v                v              |
|  +-----------------------------------------------------------+   |
|  |                  Execution Guardrails                       |   |
|  |  Budget (20 calls) . Circuit Breaker . Timeout (30s)       |   |
|  |  Query Validation . Retry w/ Backoff . Phase Limits        |   |
|  +-----------------------------------------------------------+   |
|       |              |             |                |              |
|       v              v             v                v              |
|  +-----------------------------------------------------------+   |
|  |                     Worker Layer (7 workers)                |   |
|  |  OpsWorker   LogWorker   MetricsWorker       ApmWorker     |   |
|  |  (Moogsoft)  (Splunk)    (Sysdig)     (Dynatrace+SignalFx) |   |
|  |       KnowledgeWorker    ItsmWorker       DevopsWorker      |   |
|  |       (Memory)           (ServiceNow)     (GitHub)          |   |
|  +-----------------------------------------------------------+   |
|       |              |             |                |              |
|       v              v             v                v              |
|  +-----------------------------------------------------------+   |
|  |           AgentCore Gateway (OAuth2 authenticated)          |   |
|  |  MoogsoftTarget___get_incident_by_id                        |   |
|  |  SplunkTarget___search_oneshot  SplunkTarget___get_changes  |   |
|  |  SysdigTarget___query_metrics   DynatraceTarget___get_*     |   |
|  |  ServiceNowTarget___get_ci      GitHubTarget___get_deploys  |   |
|  +-----------------------------------------------------------+   |
|       |              |             |                |              |
|       v              v             v                v              |
|  +-----------------------------------------------------------+   |
|  |             MCP Tool Servers (Bedrock AgentCore)            |   |
|  |  Moogsoft . Splunk . Sysdig . Dynatrace . SignalFx         |   |
|  |  ServiceNow . GitHub                                        |   |
|  +-----------------------------------------------------------+   |
+------------------------------------------------------------------+
```

### Design Principles

| Principle | Implementation |
|---|---|
| **Deterministic** | Same incident, same input -> same output. No LLM chooses tools. Classification is keyword-based. Hypothesis scoring is rule-based. Tiebreaks are alphabetical. |
| **Proof-driven** | Confidence requires evidence. No causal artifact -> confidence stays below 80. Multi-source corroboration raises score. Missing sources penalize it. |
| **Bounded** | 20 tool calls max per investigation. 30s timeout per worker call. Circuit breakers trip after 3 failures. Phase-level budgets prevent runaway. |
| **Observable** | Every investigation emits OTEL spans with GenAI semantic conventions. 20+ metrics flow to Splunk. Every worker call produces a receipt. |
| **Graceful** | LLM refinement is optional -- deterministic path always works. MCP stubs enable local dev. Memory, knowledge graph, judge, ITSM, and DevOps enrichment are all non-blocking opt-ins. |

---

## Investigation Pipeline

Each investigation runs four sequential phases with optional enrichment:

### Phase 1: Fetch Incident

Calls `OpsWorker.get_incident_by_id()` via the Moogsoft MCP server. Extracts `summary` and `affected_service`. If no incident data is returned, the investigation short-circuits with an empty result.

**Phase 1b: ITSM Enrichment** (optional) -- Calls `ItsmWorker` for CI details (service tier, owner, SLA), known errors, and similar ServiceNow incidents. Enriches downstream analysis without blocking the critical path.

### Phase 2: Classify

`classify_incident(summary)` uses keyword matching to map the incident to one of 10 types:

| Type | Trigger Keywords |
|---|---|
| `timeout` | timeout, timed out, request timeout |
| `oomkill` | oomkill, oom, out of memory, killed |
| `error_spike` | error spike, error rate, exception, 500 |
| `latency` | latency, slow, response time |
| `saturation` | cpu, saturation, exhaustion, disk full |
| `network` | connectivity, connection refused, dns, network |
| `cascading` | cascading, cascade, multiple services |
| `missing_data` | degraded, missing data, partial |
| `flapping` | flapping, intermittent, sporadic |
| `silent_failure` | throughput drop, stale, silent |

No LLM involved. Default fallback: `error_spike`.

### Phase 3: Execute Playbook

Each incident type maps to a deterministic playbook of 3-5 worker calls:

```
timeout:        get_incident -> search_logs(timeout) -> golden_signals -> query_metrics -> get_change_data
oomkill:        get_incident -> search_logs(OOMKilled) -> query_metrics(memory) -> get_events -> search_logs(heap)
error_spike:    get_incident -> search_logs(error) -> golden_signals -> get_change_data -> get_events
latency:        get_incident -> search_logs(latency) -> golden_signals -> query_metrics -> get_change_data
saturation:     get_incident -> golden_signals -> query_metrics(cpu) -> search_logs(cpu) -> get_change_data
network:        get_incident -> search_logs(dns) -> golden_signals -> search_logs(connection) -> get_change_data
cascading:      get_incident -> search_logs(cascade) -> golden_signals -> query_metrics -> get_change_data
missing_data:   get_incident -> search_logs(connection) -> golden_signals -> get_events -> get_change_data
flapping:       get_incident -> search_logs(flapping) -> golden_signals -> query_metrics -> get_change_data
silent_failure: get_incident -> search_logs(pipeline) -> golden_signals -> search_logs(cache) -> query_metrics
```

Playbook steps execute sequentially. Each call is budget-checked, circuit-breaker-gated, timeout-wrapped, and receipt-tracked.

**Phase 3b: DevOps Enrichment** (proof-gated) -- If change data is found in Splunk/ITSM, calls `DevopsWorker` for GitHub deployment details, PR metadata, and CI/CD pipeline status. Only triggered when evidence of a deployment already exists.

**Phase 3c: Historical Context** (optional) -- If budget remains, queries `KnowledgeWorker.search_similar()` for past incidents on the same service via AgentCore Memory.

### Phase 4: Analyze Evidence

This is the core of the system. See [Hypothesis Engine](#hypothesis-engine) below.

---

## Hypothesis Engine

SentinalAI does not ask an LLM "what went wrong." It generates hypotheses deterministically, scores them against evidence, and optionally refines with an LLM.

### Multi-Hypothesis Generation (W2)

Each incident type has a dedicated analyzer that produces 1-3 `Hypothesis` objects:

```python
class Hypothesis:
    name: str              # "downstream_slow_queries"
    root_cause: str        # Deterministic RCA string
    base_score: float      # Initial confidence (0-100)
    evidence_refs: list    # ["logs:timeout", "signals:latency"]
    reasoning: str         # Causal explanation
```

10 type-specific analyzers inspect raw evidence (logs, signals, metrics, events, changes, ITSM context, DevOps context) for patterns. For example, `_analyze_error_spike()` correlates deployment timestamps from ITSM change records with error log spikes and optionally enriches with GitHub PR/commit details.

### Evidence-Weighted Confidence (W3)

Every hypothesis score is refined by `compute_confidence()`:

```
score = base
  + min(log_count, 5)               # +1 per log entry, max +5
  + 2 if anomaly_detected            # golden signals anomaly
  + 1 if metrics have pattern         # structured metric match
  + (source_count x 2)               # cross-signal corroboration
  + (corroborating_sources x 2)      # explicit evidence refs
  - 5 if signals missing              # penalty
  - 3 if metrics missing              # penalty
```

Result clamped to `[0, 100]`.

### Winner Selection

Hypotheses sort by `(-score, name)`. Highest score wins. Alphabetical tiebreak ensures determinism.

### LLM Refinement (Optional)

If `LLM_ENABLED=true`, two optional passes run:

1. **Hypothesis refinement** -- Sends all hypotheses + evidence to Claude via Bedrock Converse. The LLM re-ranks and adjusts scores. Deterministic path remains authoritative if LLM fails.
2. **Reasoning generation** -- Asks the LLM to write a human-readable causal explanation referencing specific evidence from the timeline.

Both are non-blocking. If Bedrock is unreachable, the deterministic result stands.

---

## Guardrails

Five layers of execution safety, referenced as W1-W5 in the codebase:

| Layer | Mechanism | Limits |
|---|---|---|
| **W1: Circuit Breaker** | Per-investigation `CircuitBreakerRegistry`. Trips after N consecutive failures to a worker. Recovery probe after configurable interval. | `threshold=3`, `recovery=60s` |
| **W2: Multi-Hypothesis** | Generates 1-3 hypotheses per incident type. Never relies on a single guess. Deterministic tiebreak. | 10 type-specific analyzers |
| **W3: Evidence-Weighted** | Confidence scoring penalizes missing sources and rewards multi-source corroboration. | Bounded `[0, 100]` |
| **W4: Timeout** | Every worker call wrapped in `ThreadPoolExecutor` with hard timeout. | `30s` default |
| **W5: Retry** | Exponential backoff: 10ms x 2^attempt. Only retries if budget remaining. | `max_retries=2` |

**Execution Budget**: 20 tool calls per investigation (configurable via `INVESTIGATION_BUDGET_MAX_CALLS`). Phase-level sub-budgets:

```
initial_context:    2 calls (fetch + classify)
itsm_enrichment:    3 calls (CI details, known errors, similar incidents)
evidence_gathering: 8 calls (playbook steps)
change_correlation: 3 calls (Splunk + ITSM changes)
devops_enrichment:  2 calls (deployments, CI/CD status -- proof-gated)
historical_context: 2 calls (knowledge worker search)
```

**Query Validation**: Splunk queries are validated against an allowlist and checked for injection patterns (`|`, `eval`, `lookup`, `delete`).

---

## Institutional Knowledge Layer

> Opt-in via `KNOWLEDGE_GRAPH_ENABLED=true`. Additive enhancement -- zero impact when disabled.

A lightweight graph-backed institutional memory that stores structured incident knowledge and enables metadata-filtered similarity retrieval.

```
knowledge/
  graph_backend_json.py   # JSONL file storage (nodes.jsonl, edges.jsonl)
  graph_store.py          # Domain API: persist investigations, query history
  metadata_filter.py      # Hard-filter by service/environment/time window
  retrieval_engine.py     # Structured similarity search + confidence boost
```

### How It Works

**After each successful investigation** (confidence >= 30), the graph store persists:

| Node Type | Content |
|---|---|
| `incident` | incident_id, type, service, root_cause, confidence, evidence_refs |
| `service` | service name |
| `causal_artifact` | root_cause, confidence, incident_type |

Plus directed edges: `incident -> affects_service -> service` and `incident -> proven_by -> causal_artifact`.

**During analysis**, the retrieval engine:

1. Queries all incident nodes
2. Applies **hard metadata filter** (service, environment, time window) -- if empty, retrieval is skipped entirely (no global search)
3. Scores candidates by incident type match (+0.5) and token overlap Jaccard (+0.5)
4. Returns top-K structured results: `{incident_id, root_cause, similarity_score}`

### Proof-Gated Integration

Retrieval results are consumed only in the analysis phase, and only when a winning hypothesis already exists:

- Retrieval **cannot** produce an RCA
- Retrieval **cannot** override the deterministic proof rule
- Confidence boost is `best_similarity x 10`, **capped at +10**
- Without a causal artifact, confidence **stays below 80** regardless of retrieval

### Storage Backend

JSONL files in a configurable directory (`KNOWLEDGE_STORAGE_DIR`). Each record includes `node_type`, `node_id`, `metadata`, and `timestamp`. Designed for single-process use with a clean migration path to Neptune or OpenSearch.

---

## Observability

### OTEL Tracing

Every investigation creates a root span with attributes following [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

```
gen_ai.system = "sentinalai"
gen_ai.operation.name = "investigate"
gen_ai.request.model = "anthropic.claude-sonnet-..."
gen_ai.usage.input_tokens = 1234
gen_ai.usage.output_tokens = 567
```

Plus custom evaluation attributes:

```
sentinalai.incident_type    sentinalai.confidence
sentinalai.service          sentinalai.root_cause
sentinalai.tool_calls       sentinalai.budget_remaining
sentinalai.hypothesis_count sentinalai.winner_hypothesis
sentinalai.evidence_sources
```

### Metrics Pipeline

20+ OTEL metrics emitted per investigation, exported to Splunk via the OTEL Collector sidecar:

| Metric | Type | Purpose |
|---|---|---|
| `sentinalai.investigations.total` | Counter | Investigation completions by type/service/status |
| `sentinalai.confidence.distribution` | Histogram | Confidence score distribution |
| `sentinalai.investigation.duration_ms` | Histogram | End-to-end latency |
| `sentinalai.worker_calls.total` | Counter | Per-worker call counts by status |
| `sentinalai.worker_call.duration_ms` | Histogram | Worker latency |
| `sentinalai.circuit_breaker.trips` | Counter | Circuit breaker state transitions |
| `sentinalai.budget.exhausted` | Counter | Budget exhaustion events |
| `sentinalai.evidence.completeness` | Counter | Per-source evidence availability |
| `sentinalai.hypotheses.count` | Histogram | Hypotheses generated per investigation |
| `sentinalai.judge.score` | Histogram | LLM-as-judge quality scores |
| `gen_ai.client.token.usage` | Histogram | LLM token usage (GenAI semconv) |

### Receipt Tracking

Every worker call produces a `Receipt` with:
- `correlation_id` (UUID), worker name, action, redacted params
- `start_ts`, `end_ts`, `elapsed_ms`
- `status` (success / error / timeout)
- `result_count` (heuristic item count)

Sensitive fields (`password`, `token`, `secret`, `api_key`, `authorization`) are automatically redacted before storage.

Receipts are aggregated into summary metrics and persisted in replay artifacts.

### Replay

Set `SENTINALAI_REPLAY_DIR` to persist full investigation artifacts (receipts, evidence, result). Load them back with `investigate(incident_id, replay=True)` for deterministic replay and regression testing.

---

## Workers & MCP Integration

Workers are thin wrappers around MCP tool servers. Tool calls are routed through the AgentCore gateway (preferred) or via legacy `invoke_inline_agent` (boto3 fallback):

| Worker | MCP Server | Actions |
|---|---|---|
| `OpsWorker` | Moogsoft | `get_incident_by_id` |
| `LogWorker` | Splunk | `search_logs`, `get_change_data` |
| `MetricsWorker` | Sysdig | `query_metrics`, `get_resource_metrics`, `get_events` |
| `ApmWorker` | Dynatrace + SignalFx | `get_golden_signals`, `check_latency` |
| `KnowledgeWorker` | AgentCore Memory | `search_similar`, `store_result` |
| `ItsmWorker` | ServiceNow | `get_ci_details`, `search_incidents`, `get_change_records`, `get_known_errors` |
| `DevopsWorker` | GitHub | `get_recent_deployments`, `get_pr_details`, `get_commit_diff`, `get_workflow_runs` |

All workers extend `BaseWorker`, which provides action dispatch, timing, and structured error handling.

### MCP Gateway Client

`workers/mcp_client.py` manages the connection to MCP tool servers:

- **AgentCore Gateway** (preferred) -- Single HTTPS endpoint for all targets. Tool names map to `{TargetName}___{operation}` format (e.g., `SplunkTarget___search_oneshot`)
- **OAuth2 authentication** -- Client credentials grant with automatic token refresh (configurable pre-expiry buffer). Supports Cognito, Secrets Manager, and static token fallback
- **Per-server rate limiting** -- Token bucket algorithm with configurable RPM per target (Splunk: unlimited, Moogsoft: 60 RPM, etc.)
- **Legacy fallback** -- `invoke_inline_agent` via boto3 when gateway is not configured
- **Stub responses** -- When neither gateway nor ARNs are configured, workers return empty/minimal responses for local development
- **401 retry** -- Automatic token invalidation and retry on authentication failures

---

## LLM Integration

### Bedrock Converse

`supervisor/llm.py` wraps the Bedrock Converse API for two operations:

1. **Hypothesis refinement** -- Re-ranks hypotheses with full evidence context. LLM adjustments applied only if valid JSON returned.
2. **Reasoning generation** -- Produces human-readable causal explanation (capped at 512 tokens).

Both operations gracefully degrade. The deterministic analysis is never blocked on LLM availability.

### LLM-as-Judge

`supervisor/llm_judge.py` evaluates investigation quality across six dimensions:

| Dimension | What It Measures |
|---|---|
| `root_cause_accuracy` | Does RCA match expected? |
| `causal_reasoning` | Is the causal chain clear? |
| `evidence_usage` | Are claims backed by data? |
| `timeline_quality` | Is timeline ordered and progressive? |
| `actionability` | Could an SRE act on this? |
| `overall` | Weighted composite score |

Falls back to rule-based scoring when LLM is unavailable. Scores are emitted as OTEL metrics for quality tracking over time.

---

## Memory

### AgentCore Memory (STM + LTM)

`supervisor/memory.py` integrates with Bedrock AgentCore's Memory service:

- **Short-Term Memory (STM)** -- Stores and retrieves recent investigation turns within a session
- **Long-Term Memory (LTM)** -- Persists completed investigations for semantic similarity search across sessions

Namespaced storage (`/incidents/`, `/services/{service}/`, `/patterns/{type}/`) enables targeted retrieval.

Opt-in via `BEDROCK_AGENTCORE_MEMORY_ID`. All calls are no-ops when the SDK is unavailable or unconfigured.

---

## Getting Started

### Prerequisites

- Python 3.11+
- AWS credentials (for Bedrock + AgentCore MCP servers)
- Docker (optional, for containerized deployment)

### Install

```bash
git clone <repo-url> && cd Sentinalai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Run Locally

Without MCP servers (stub mode -- returns empty evidence, useful for testing the pipeline):

```bash
python scripts/run_investigation.py --incident-id INC12345
```

With AgentCore Gateway (production):

```bash
export AGENTCORE_GATEWAY_URL="https://gateway.agentcore.us-east-1.amazonaws.com"
export GATEWAY_OAUTH2_CLIENT_ID="your-client-id"
export GATEWAY_OAUTH2_CLIENT_SECRET="your-client-secret"
export GATEWAY_OAUTH2_TOKEN_URL="https://your-domain.auth.us-east-1.amazoncognito.com/oauth2/token"
python scripts/run_investigation.py --incident-id INC12345
```

With legacy MCP ARNs (per-server routing):

```bash
export MCP_MOOGSOFT_TOOL_ARN="arn:aws:bedrock:us-east-1:..."
export MCP_SPLUNK_TOOL_ARN="arn:aws:bedrock:us-east-1:..."
export MCP_SYSDIG_TOOL_ARN="arn:aws:bedrock:us-east-1:..."
python scripts/run_investigation.py --incident-id INC12345
```

### Run with Docker

```bash
cp .env.template .env  # fill in actual values
docker compose up --build
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "INC12345"}'
```

### Run Eval Pipeline

```bash
python scripts/run_evals.py
```

Evaluates 10 test scenarios, scores each across 6 quality dimensions, and emits OTEL metrics.

---

## Configuration

All configuration via environment variables. See `.env.template` for the full reference.

### Core

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | LLM model for hypothesis refinement |
| `EVAL_JUDGE_MODEL_ID` | `anthropic.claude-haiku-4-5-20251001-v1:0` | LLM model for eval judge (cheaper) |
| `LLM_ENABLED` | `true` | Enable/disable LLM refinement |
| `LLM_TEMPERATURE` | `0.0` | Temperature (0 = deterministic) |
| `LLM_MAX_TOKENS` | `2048` | Max output tokens per LLM call |

### AgentCore Gateway (Preferred)

| Variable | Default | Description |
|---|---|---|
| `AGENTCORE_GATEWAY_URL` | -- | Gateway HTTPS endpoint (enables gateway mode) |
| `AGENTCORE_TARGET_MOOGSOFT` | `MoogsoftTarget` | Gateway target name for Moogsoft |
| `AGENTCORE_TARGET_SPLUNK` | `SplunkTarget` | Gateway target name for Splunk |
| `AGENTCORE_TARGET_SYSDIG` | `SysdigTarget` | Gateway target name for Sysdig |
| `AGENTCORE_TARGET_SIGNALFX` | `SignalFxTarget` | Gateway target name for SignalFx |
| `AGENTCORE_TARGET_DYNATRACE` | `DynatraceTarget` | Gateway target name for Dynatrace |
| `AGENTCORE_TARGET_SERVICENOW` | `ServiceNowTarget` | Gateway target name for ServiceNow |
| `AGENTCORE_TARGET_GITHUB` | `GitHubTarget` | Gateway target name for GitHub |

### OAuth2 Authentication

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_OAUTH2_CLIENT_ID` | -- | OAuth2 client ID |
| `GATEWAY_OAUTH2_CLIENT_SECRET` | -- | OAuth2 client secret |
| `GATEWAY_OAUTH2_TOKEN_URL` | -- | Token endpoint URL |
| `GATEWAY_OAUTH2_SCOPE` | -- | OAuth2 scope (optional) |
| `GATEWAY_OAUTH2_SECRET_ARN` | -- | AWS Secrets Manager ARN (alternative to inline secret) |
| `GATEWAY_TOKEN_REFRESH_BUFFER_SECONDS` | `600` | Pre-expiry refresh buffer |
| `GATEWAY_ACCESS_TOKEN` | -- | Static Bearer token (fallback) |

### MCP Tool ARNs (Legacy)

| Variable | Description |
|---|---|
| `MCP_MOOGSOFT_TOOL_ARN` | Moogsoft MCP server ARN |
| `MCP_SPLUNK_TOOL_ARN` | Splunk MCP server ARN |
| `MCP_SYSDIG_TOOL_ARN` | Sysdig MCP server ARN |
| `MCP_SIGNALFX_TOOL_ARN` | SignalFx MCP server ARN |
| `MCP_DYNATRACE_TOOL_ARN` | Dynatrace MCP server ARN |
| `MCP_SERVICENOW_TOOL_ARN` | ServiceNow MCP server ARN |
| `MCP_GITHUB_TOOL_ARN` | GitHub MCP server ARN |

When gateway is configured, individual ARNs are ignored. When neither is set, workers return stub responses.

### Guardrails

| Variable | Default | Description |
|---|---|---|
| `INVESTIGATION_BUDGET_MAX_CALLS` | `20` | Max tool calls per investigation |
| `MCP_CALL_TIMEOUT_SECONDS` | `30` | Per-call timeout |
| `MCP_MAX_RETRIES` | `2` | Retry attempts per call |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `3` | Consecutive failures before circuit opens |
| `CIRCUIT_BREAKER_RECOVERY_SECONDS` | `60` | Recovery probe interval |

### Observability

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | -- | OTLP HTTP endpoint (enables OTEL SDK) |
| `OTEL_SERVICE_NAME` | `sentinalai` | Service name for traces/metrics |
| `OTEL_DEPLOYMENT_ENV` | `dev` | Deployment environment tag |
| `SENTINALAI_REPLAY_DIR` | `/tmp/sentinalai_replays` | Replay artifact storage |

### Knowledge Layer

| Variable | Default | Description |
|---|---|---|
| `KNOWLEDGE_GRAPH_ENABLED` | `false` | Enable institutional knowledge graph |
| `KNOWLEDGE_STORAGE_DIR` | `knowledge/.knowledge_store` | JSONL storage directory |

### Memory

| Variable | Default | Description |
|---|---|---|
| `BEDROCK_AGENTCORE_MEMORY_ID` | -- | AgentCore Memory ID (enables memory) |
| `MEMORY_STM_LAST_K_TURNS` | `5` | Short-term memory window |
| `MEMORY_LTM_TOP_K` | `3` | Long-term memory results |
| `MEMORY_LTM_RELEVANCE_THRESHOLD` | `0.5` | Minimum relevance for LTM results |

### Database (Optional)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | -- | PostgreSQL connection string (enables DB features) |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size |
| `DATABASE_POOL_OVERFLOW` | `5` | Max overflow connections |
| `DATABASE_POOL_TIMEOUT` | `30` | Connection acquisition timeout |
| `DATABASE_POOL_RECYCLE` | `1800` | Connection recycling interval |

---

## Testing

```bash
# Full suite (1,047 tests across 33 test files)
python -m pytest

# Specific module
python -m pytest tests/test_supervisor_contracts.py -v

# With coverage
python -m pytest --cov=supervisor --cov=workers --cov=knowledge --cov=database --cov-report=term-missing

# Run eval pipeline (10 scenarios, 6 quality dimensions each)
python scripts/run_evals.py
```

### Test Structure

| Category | Files | What They Validate |
|---|---|---|
| **Core Pipeline** | `test_supervisor.py`, `test_integration.py`, `test_supervisor_contracts.py` | All 10 incident types produce valid RCA, end-to-end flow, schema enforcement |
| **Hypothesis & Voting** | `test_analyzer_branches.py`, `test_vote_logic_validation.py` | Multi-hypothesis generation, evidence-weighted scoring, deterministic tiebreaks |
| **Negative & Edge Cases** | `test_supervisor_negative.py`, `test_supervisor_receipts.py` | Malformed input, all workers failing, budget exhaustion, circuit breaker trips, replay determinism |
| **Tool Selection** | `test_tool_selector.py` | Classification coverage (10 types), playbook retrieval, keyword priority, case insensitivity |
| **Workers** | `test_workers.py`, `test_base_worker.py`, `test_itsm_worker.py`, `test_devops_worker.py` | Dispatch, determinism, parameter validation, error handling for all 7 workers |
| **MCP Client** | `test_mcp_client.py`, `test_mcp_client_coverage.py`, `test_mcp_gateway_coverage.py` | Gateway routing, OAuth2, tool name mapping, stub responses, 401 retry, rate limiting |
| **LLM** | `test_llm.py`, `test_llm_judge.py`, `test_llm_coverage.py` | Bedrock Converse, hypothesis refinement, judge scoring, rule-based fallback |
| **Observability** | `test_observability.py`, `test_observability_otel.py`, `test_observability_coverage.py` | OTEL spans, GenAI semantic conventions, metric emission |
| **Eval Pipeline** | `test_eval_pipeline.py`, `test_eval_metrics_coverage.py` | Judge scores to OTEL histograms, GenAI token metrics, full pipeline scoring |
| **Memory** | `test_memory.py`, `test_memory_coverage.py` | STM/LTM roundtrip, graceful degradation, search, error handling |
| **Knowledge** | `test_knowledge_layer.py`, `test_knowledge_worker_coverage.py` | Graph store, metadata filter, retrieval engine, confidence boost cap |
| **Infrastructure** | `test_database_connection.py`, `test_agentcore_runtime.py`, `test_receipt.py`, `test_replay.py`, `test_guardrails.py` | Connection pooling, HTTP adapter, receipt serialization, replay artifacts, budget/circuit breaker |
| **Branch Coverage** | `test_agent_coverage.py` | Supplementary edge-case paths in supervisor agent |

Coverage target: **80%** (enforced in `pyproject.toml`).

---

## Deployment

### AgentCore Runtime

`agentcore_runtime.py` exposes the standard AgentCore HTTP contract:

```
POST /invocations    -> Run investigation (accepts incident_id or prompt field)
GET  /ping           -> Health check
```

**Input validation**: Incident IDs must match `^[A-Za-z0-9_\-]{1,100}$`.

### Docker

```bash
docker build -t sentinalai .
docker run -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e AGENTCORE_GATEWAY_URL=https://gateway.agentcore.us-east-1.amazonaws.com \
  -e GATEWAY_OAUTH2_CLIENT_ID=... \
  -e GATEWAY_OAUTH2_CLIENT_SECRET=... \
  -e GATEWAY_OAUTH2_TOKEN_URL=... \
  sentinalai
```

The container runs as non-root (`bedrock_agentcore`, UID 1000), read-only rootfs, with a 30s health check interval.

### Docker Compose (with OTEL Collector)

```bash
cp .env.template .env  # fill in values
docker compose up --build
```

Includes an OTEL Collector sidecar (`otel/opentelemetry-collector-contrib:0.146.0`) that receives traces and metrics on port 4318 and exports to Splunk HEC.

Resource limits: 2GB memory / 2 CPUs for the agent, 512MB / 1 CPU for the collector.

### AgentCore Managed Deployment

```bash
agentcore configure --config agentcore.yaml
agentcore launch
```

See `agentcore.yaml` for the full deployment manifest including environment variable templating.

---

## Project Structure

```
Sentinalai/
  supervisor/
    agent.py              # Investigation pipeline + hypothesis engine (1,955 lines)
    tool_selector.py      # Incident classification + playbooks (422 lines)
    guardrails.py         # Budget, circuit breaker, query validation
    observability.py      # OTEL tracing + lightweight span fallback
    eval_metrics.py       # 20+ OTEL metric instruments (499 lines)
    llm.py                # Bedrock Converse client
    llm_judge.py          # LLM-as-judge quality scoring
    memory.py             # AgentCore Memory (STM + LTM)
    system_prompt.py      # System prompt for supervisor
    receipt.py            # Worker call receipts
    replay.py             # Investigation replay store
  workers/
    base_worker.py        # Action dispatch + timing
    ops_worker.py         # Moogsoft
    log_worker.py         # Splunk
    metrics_worker.py     # Sysdig
    apm_worker.py         # Dynatrace + SignalFx APM
    knowledge_worker.py   # AgentCore Memory search
    itsm_worker.py        # ServiceNow (CMDB, changes, known errors)
    devops_worker.py      # GitHub (deployments, PRs, CI/CD)
    mcp_client.py         # AgentCore gateway + OAuth2 (1,005 lines)
  knowledge/
    graph_backend_json.py # JSONL node/edge storage
    graph_store.py        # Domain-level graph API
    metadata_filter.py    # Hard metadata filter
    retrieval_engine.py   # Similarity retrieval + boost
  database/
    connection.py         # PostgreSQL + pgvector
  scripts/
    run_investigation.py  # CLI runner
    run_evals.py          # Eval pipeline (10 scenarios)
    init_database.py      # DB setup
  tests/                  # 33 test files, 1,047+ tests
  agentcore_runtime.py    # HTTP adapter (AgentCore / FastAPI)
  Dockerfile
  docker-compose.yaml
  otel-collector-config.yaml
  agentcore.yaml
  pyproject.toml
  .env.template
```

---

## Contributing

1. Install dev dependencies: `pip install -e ".[dev]"`
2. Run tests before submitting: `python -m pytest`
3. Coverage must stay above 80%: `python -m pytest --cov=supervisor --cov=workers --cov=knowledge --cov=database --cov-report=term-missing`
4. Follow the existing code patterns -- deterministic paths first, LLM/optional features as non-blocking enhancements
