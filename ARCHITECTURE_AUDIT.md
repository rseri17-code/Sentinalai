# SentinalAI — Architectural Capability Assessment

**Audit Date:** 2026-03-07 (updated 2026-03-08)
**Auditor Role:** Principal Infrastructure Engineer
**Scope:** Enterprise AI Agent for Production SRE Operations — Automated Incident Root Cause Analysis
**Revision Note:** Updated 2026-03-08 to reflect remediations applied since initial audit.

---

## 1. Repository Architecture Summary

### Directory Structure

```
Sentinalai/
├── agentcore_runtime.py          # HTTP entry point (FastAPI + AgentCore SDK)
├── agentcore.yaml                # AgentCore deployment config
├── supervisor/                   # Orchestration layer
│   ├── agent.py                  # Main investigation supervisor (~2198 lines)
│   ├── system_prompt.py          # LLM system prompt
│   ├── tool_selector.py          # Incident classification + playbook routing
│   ├── guardrails.py             # Execution budgets, circuit breakers, query validation
│   ├── observability.py          # OTEL tracing + lightweight fallback spans
│   ├── eval_metrics.py           # OTEL metric instrumentation (deep eval)
│   ├── llm.py                    # Bedrock Converse API client
│   ├── llm_judge.py              # LLM-as-judge eval scorer
│   ├── memory.py                 # AgentCore Memory (STM + LTM)
│   ├── receipt.py                # Evidence receipt model (with policy_ref, trace_id, wall-clock)
│   ├── remediation.py            # Remediation guidance engine
│   ├── replay.py                 # Investigation replay persistence
│   ├── severity.py               # Severity detection + budget scaling
│   ├── confidence_calibrator.py  # Learned confidence calibration model
│   ├── rca_report.py             # Structured RCA report generation + markdown rendering
│   ├── ground_truth_eval.py      # Ground truth evaluation metrics
│   └── incident_model.py         # Canonical incident normalization (Moogsoft, ServiceNow, PagerDuty)
├── workers/                      # MCP tool workers
│   ├── base_worker.py            # Base class with dispatch + logging
│   ├── mcp_client.py             # MCP Gateway (OAuth2, rate limiting, transport)
│   ├── ops_worker.py             # Moogsoft incidents
│   ├── log_worker.py             # Splunk logs + change data
│   ├── metrics_worker.py         # Sysdig metrics + events
│   ├── apm_worker.py             # Dynatrace + SignalFx golden signals
│   ├── itsm_worker.py            # ServiceNow CMDB + changes
│   ├── devops_worker.py          # GitHub CI/CD + code changes
│   └── knowledge_worker.py       # Historical incident search (Memory LTM)
├── knowledge/                    # Institutional knowledge graph
│   ├── graph_store.py
│   ├── graph_backend_json.py
│   ├── retrieval_engine.py
│   └── metadata_filter.py
├── database/                     # PostgreSQL + pgvector persistence
│   ├── connection.py
│   ├── persistence.py            # Investigation, tool usage, and knowledge persistence
│   └── schema.sql
├── scripts/                      # Operational scripts
│   ├── run_investigation.py
│   ├── run_evals.py
│   └── init_database.py
└── tests/                        # 45 test files, 1646 tests, 96% coverage
```

### Agent Architecture (Inferred from Code)

The system follows a **supervisor-worker pattern** with **playbook-driven orchestration**:

```
Incident Input (POST /invocations)
        │
        ▼
┌─────────────────────────────────────────────────────┐
│          AgentCore Runtime (agentcore_runtime.py)    │
│  - Auth validation (Bearer token + Agent ID)        │
│  - Payload validation (incident_id)                 │
│  - Request routing                                  │
└───────────────┬─────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────┐
│     SentinalAISupervisor (supervisor/agent.py)      │
│                                                     │
│  1. Fetch Incident (ops_worker → Moogsoft)          │
│  2. Classify Incident (keyword + LLM fallback)      │
│  3. ITSM Context (itsm_worker → ServiceNow)        │
│  4. Execute Playbook (3-6 targeted tool calls)      │
│  5. DevOps Enrichment (proof-gated → GitHub)        │
│  6. Historical Context (knowledge_worker → Memory)  │
│  7. Analyze Evidence (multi-hypothesis scoring)     │
│  8. LLM Refinement (Bedrock Converse, optional)     │
│  9. Generate RCA Result                             │
│                                                     │
│  Guardrails: Budget, Circuit Breakers, Timeout,     │
│              Wall-clock Deadline                    │
│  Observability: OTEL Spans + Metrics + Receipts     │
└───────────────┬─────────────────────────────────────┘
                │
     ┌──────────┼──────────┬──────────┐
     ▼          ▼          ▼          ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│OpsWorker│ │LogWorker│ │Metrics  │ │APM      │
│(Moogsoft)│ │(Splunk) │ │Worker   │ │Worker   │
│         │ │         │ │(Sysdig) │ │(Dynatr.)│
└────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘
     │           │           │           │
     └─────┬─────┴─────┬─────┴─────┬─────┘
           ▼                       ▼
    ┌──────────────┐      ┌──────────────┐
    │  McpGateway  │      │ItsmWorker    │
    │  (unified)   │      │(ServiceNow)  │
    │              │      ├──────────────┤
    │ OAuth2 Auth  │      │DevopsWorker  │
    │ Rate Limiting│      │(GitHub)      │
    │ Stub Fallback│      ├──────────────┤
    └──────┬───────┘      │KnowledgeWkr │
           │              │(Memory LTM) │
           ▼              └──────────────┘
    AgentCore Gateway
    (MCP Protocol over
     Streamable HTTP)
           │
    ┌──────┼──────┬──────┬──────┬──────┬──────┐
    ▼      ▼      ▼      ▼      ▼      ▼      ▼
 Moogsoft Splunk Sysdig SignalFx Dynatr. SNOW GitHub
```

---

## 2. Capability Implementation Matrix

### Capability 1: Incident Intake Layer

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| HTTP endpoint | Implemented | `agentcore_runtime.py:264` — `POST /invocations` |
| AgentCore SDK mode | Implemented | `agentcore_runtime.py:207-213` — `BedrockAgentCoreApp` entrypoint |
| Payload validation | Implemented | `agentcore_runtime.py:122-134` — regex-validated `incident_id` |
| Authentication | Implemented | `agentcore_runtime.py:49-70` — Bearer token auth (optional) |
| Agent identity whitelist | Implemented | `agentcore_runtime.py:73-85` — `ALLOWED_AGENT_IDS` |
| Moogsoft intake | Implemented | `workers/ops_worker.py:23-32` — `get_incident_by_id` via MCP |
| ServiceNow intake | Implemented | `supervisor/incident_model.py` — `Incident.from_servicenow()` normalizes ServiceNow incident payloads |
| PagerDuty intake | Implemented | `supervisor/incident_model.py` — `Incident.from_pagerduty()` normalizes PagerDuty incident payloads |
| Manual trigger | Implemented | `scripts/run_investigation.py` |
| Incident normalization | Implemented | `supervisor/incident_model.py` — canonical `Incident` dataclass with `from_moogsoft()`, `from_servicenow()`, `from_pagerduty()` factory methods; `supervisor/intake.py` — multi-source intake layer with validation |

**Files:** `agentcore_runtime.py`, `workers/ops_worker.py`, `supervisor/agent.py`, `supervisor/incident_model.py`, `supervisor/intake.py`

---

### Capability 2: Investigation Orchestrator

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Task sequencing | Implemented | Playbook-driven sequential execution (`agent.py:699-737`) |
| State tracking | Implemented | `ReceiptCollector` tracks all calls; `ExecutionBudget` tracks progress |
| Lifecycle management | Implemented | Full lifecycle: fetch → classify → enrich → execute → analyze → report |
| Parallel execution | Implemented | `_execute_playbook()` groups steps by worker and submits groups concurrently via `_parallel_executor` (`agent.py:898-916`); sequential fallback via `_execute_playbook_sequential()` |
| Phase-based orchestration | Implemented | Phase 1 (ITSM), Phase 2 (evidence), Phase 3 (changes/DevOps), Phase 4 (historical) |
| Replay support | Implemented | `supervisor/replay.py` — save/load investigation artifacts |
| Severity-based budget scaling | Implemented | `supervisor/severity.py` — `detect_severity()` and `get_budget_for_severity()` called from `investigate()` at `agent.py:300-301` |

**Files:** `supervisor/agent.py:219-530`, `supervisor/replay.py`, `supervisor/severity.py`

---

### Capability 3: MCP Tool Invocation Layer

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Unified gateway | Implemented | `workers/mcp_client.py:520-578` — `McpGateway` singleton |
| MCP protocol transport | Implemented | `streamablehttp_client` via `strands.tools.mcp.MCPClient` |
| OAuth2 authentication | Implemented | `OAuth2CredentialProvider` with client_credentials grant, token caching, auto-refresh |
| Secrets Manager integration | Implemented | `_fetch_secret_from_asm()` for client secrets |
| Rate limiting | Implemented | `RateLimiterRegistry` with per-server token buckets |
| Splunk queries | Implemented | `log_worker.py` — `search_logs`, `get_change_data` |
| Sysdig metrics | Implemented | `metrics_worker.py` — `query_metrics`, `get_events` |
| SignalFx queries | Implemented | `apm_worker.py:46-55` — enrichment via `signalfx.query_signalfx_metrics` |
| Dynatrace APM | Implemented | `apm_worker.py:39-43` — primary golden signals source |
| Moogsoft metadata | Implemented | `ops_worker.py` — `get_incident_by_id` |
| ServiceNow ITSM | Implemented | `itsm_worker.py` — CI details, change records, known errors, incident search |
| GitHub DevOps | Implemented | `devops_worker.py` — deployments, PR details, commit diff, workflow runs |
| Stub fallback | Implemented | `mcp_client.py:965-1019` — stub responses for dev/testing |
| Legacy ARN mode | Implemented | `invoke_inline_agent` backward compatibility |

**Files:** `workers/mcp_client.py`, `workers/*.py`

---

### Capability 4: Evidence Collection Layer

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Logs | Implemented | Splunk log search via `log_worker` |
| Metrics | Implemented | Sysdig metrics via `metrics_worker` |
| Events | Implemented | Sysdig events via `metrics_worker` |
| Golden signals | Implemented | Dynatrace + SignalFx via `apm_worker` |
| Deployment/change metadata | Implemented | Splunk change data + ServiceNow change records + GitHub deployments |
| Topology data | Partial | ServiceNow CMDB CI details include dependencies but no topology graph |
| Evidence persistence | Implemented | `ReceiptCollector` tracks all evidence with timing, status, correlation ID |
| Evidence referencing | Implemented | `Hypothesis.evidence_refs` links hypotheses to evidence sources |
| Query validation | Implemented | `guardrails.py:155-171` — Splunk query policy allowlist |

**Files:** `supervisor/agent.py:560-1645`, `supervisor/receipt.py`, `workers/*.py`

---

### Capability 5: Hypothesis Generation Engine

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Multi-hypothesis generation | Implemented | `agent.py:1029-1059` — type-specific analyzers generate 2-3 hypotheses each |
| Resource saturation | Implemented | `_analyze_saturation` (CPU after change, generic) |
| Network failure | Implemented | `_analyze_network` (DNS after maintenance, generic) |
| Dependency outage | Implemented | `_analyze_timeout` (downstream slow queries), `_analyze_cascading` |
| Deployment failure | Implemented | `_analyze_error_spike` (deployment + error type correlation) |
| Configuration drift | Partial | Detected through `config_change` type in changes, not a dedicated analyzer |
| Memory leak | Implemented | `_analyze_oomkill` (gradual increase pattern detection) |
| Pipeline failure | Implemented | `_analyze_silent_failure` (pipeline + stale cache) |
| Connection pool leak | Implemented | `_analyze_flapping` (sawtooth pattern detection) |
| Incident types supported | 10 types | timeout, oomkill, error_spike, latency, saturation, network, cascading, missing_data, flapping, silent_failure |

**Files:** `supervisor/agent.py:1029-1554`

---

### Capability 6: Hypothesis Evaluation Engine

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Evidence-weighted scoring | Implemented | `compute_confidence()` at `agent.py:125-177` |
| LLM refinement | Implemented | `_llm_refine_hypotheses()` via `llm.refine_hypothesis()` |
| Hypothesis ranking | Implemented | `agent.py:836` — sorted by `(-score, name)` for deterministic tiebreak |
| Confidence calibration | Implemented | Cross-signal bonuses, missing-source penalties |
| Supporting evidence refs | Implemented | Each `Hypothesis` carries `evidence_refs` list |
| Knowledge retrieval boost | Implemented | `agent.py:860-878` — institutional knowledge boosts confidence |
| Proof-gate (confidence cap) | Implemented | Without evidence refs, confidence capped at 79 |

**Files:** `supervisor/agent.py:125-177, 790-896`

---

### Capability 7: Root Cause Decision Engine

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Winner selection | Implemented | Highest score wins; deterministic tiebreak by name |
| Evidence backing | Implemented | Winner hypothesis carries evidence_refs and reasoning |
| LLM reasoning generation | Implemented | `_llm_generate_reasoning()` via `llm.generate_reasoning()` |
| Determinism | Implemented | `temperature=0.0`, sorted hypotheses, keyword-first classification |

**Files:** `supervisor/agent.py:835-896`, `supervisor/llm.py:197-297`

---

### Capability 8: RCA Output Generator

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Incident summary | Implemented | Canonical `Incident` model (`incident_model.py`) provides structured incident data |
| Timeline | Implemented | `_build_timeline()` — chronologically ordered evidence timeline |
| Investigation steps | Implemented | Receipts capture every tool call with timing, status, params, policy_ref, trace_id |
| Evidence references | Implemented | `evidence_refs` on winning hypothesis; evidence dict in replay |
| Root cause | Implemented | `root_cause` string in result |
| Confidence score | Implemented | `confidence` integer 0-100 in result (with learned calibration) |
| Reasoning narrative | Implemented | `reasoning` string — deterministic or LLM-enhanced |
| Remediation guidance | Implemented | `generate_remediation()` called from `investigate()` at `agent.py:440`; produces actionable remediation steps from templates + optional LLM enrichment |
| Structured report format | Implemented | `supervisor/rca_report.py` — `generate_rca_report()` produces structured JSON + `render_markdown()` for human-readable output |
| Historical matches | Implemented | `historical_matches` included in result |

**Files:** `supervisor/agent.py:440-510`, `supervisor/remediation.py`, `supervisor/rca_report.py`, `supervisor/incident_model.py`

---

### Capability 9: Agent Observability

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Investigation start/end spans | Implemented | `trace_span("investigate")` wraps entire flow |
| Tool execution spans | Implemented | `trace_span(f"tool:{worker_name}.{action}")` per call |
| Evidence retrieval tracking | Implemented | `record_evidence_completeness()` per investigation |
| Hypothesis scoring metrics | Implemented | `record_investigation()` with hypothesis_count, winner |
| Final RCA result telemetry | Implemented | OTEL span attributes for confidence, root_cause, tool_calls |
| OpenTelemetry traces | Implemented | `observability.py` — OTLP/HTTP exporter with graceful fallback |
| OpenTelemetry metrics | Implemented | `eval_metrics.py` — 20+ counters and histograms |
| GenAI semantic conventions | Implemented | `gen_ai.*` attributes on LLM calls |
| LLM-as-judge eval | Implemented | `llm_judge.py` — multi-dimension scoring with OTEL emission |
| Cost estimation | Implemented | `eval_metrics.py:497-506` — per-model token cost estimation |
| Receipt audit trail | Implemented | `receipt.py` — full call receipts with timing, policy ref, trace ID |
| Structured logging | Implemented | JSON span logs as fallback when OTEL SDK absent |

**Files:** `supervisor/observability.py`, `supervisor/eval_metrics.py`, `supervisor/llm_judge.py`, `supervisor/receipt.py`

---

### Capability 10: Safety Guardrails

**Status: Implemented**

| Aspect | Status | Details |
|--------|--------|---------|
| Execution budgets | Implemented | `ExecutionBudget` — 20 calls max per investigation |
| Per-call timeout | Implemented | `CALL_TIMEOUT_SECONDS = 30s` via ThreadPoolExecutor |
| Wall-clock deadline | Implemented | `INVESTIGATION_DEADLINE_SECONDS = 120s` |
| Tool failure handling | Implemented | `_call_worker()` catches all exceptions, records in receipts |
| Retry logic | Implemented | `MAX_RETRIES_PER_CALL = 2` with exponential backoff |
| Circuit breakers | Implemented | Per-investigation `CircuitBreakerRegistry` (threshold=3, recovery=60s) |
| Query validation | Implemented | Splunk query policy allowlist in `guardrails.py:127-171` |
| Rate limiting | Implemented | Per-server token bucket (`mcp_client.py:443-513`) |
| Parameter redaction | Implemented | `receipt.py:149-155` — redacts passwords/tokens/secrets |
| Auth token validation | Implemented | Bearer token + agent ID whitelist on `/invocations` |
| Graceful shutdown | Implemented | SIGTERM handler disposes DB + memory clients |

**Files:** `supervisor/guardrails.py`, `supervisor/agent.py:432-546`, `workers/mcp_client.py:443-513`, `agentcore_runtime.py:319-332`

---

## 3. Architecture Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │              Incident Sources                │
                    │  Moogsoft │ ServiceNow │ Manual Trigger      │
                    └─────────────────┬────────────────────────────┘
                                      │ POST /invocations
                                      ▼
                    ┌──────────────────────────────────────────────┐
                    │         AgentCore Runtime Layer              │
                    │  FastAPI / BedrockAgentCoreApp               │
                    │  Auth: Bearer Token + Agent ID Whitelist     │
                    │  Input: incident_id validation               │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │        SentinalAI Supervisor Agent           │
                    │                                              │
                    │  ┌────────────────────────────────────┐      │
                    │  │ STEP 1: Incident Fetch             │      │
                    │  │   ops_worker → Moogsoft            │      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 2: Classification             │      │
                    │  │   Keyword matching + LLM fallback  │      │
                    │  │   10 incident types supported      │      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 3: ITSM Enrichment (Phase 1)  │      │
                    │  │   CI details, known errors, similar│      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 4: Playbook Execution         │      │
                    │  │   3-6 targeted tool calls          │      │
                    │  │   Budget + Circuit Breaker guarded  │      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 5: DevOps Enrichment          │      │
                    │  │   Proof-gated (only if change found)│     │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 6: Historical Context         │      │
                    │  │   Knowledge search (Memory LTM)    │      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 7: Evidence Analysis           │      │
                    │  │   Multi-hypothesis generation      │      │
                    │  │   Evidence-weighted scoring         │      │
                    │  │   LLM refinement (optional)         │      │
                    │  │   Knowledge retrieval boost         │      │
                    │  ├────────────────────────────────────┤      │
                    │  │ STEP 8: RCA Result                  │      │
                    │  │   root_cause, confidence, timeline  │      │
                    │  │   reasoning, historical_matches     │      │
                    │  │   Replay persist + Memory store     │      │
                    │  │   LLM-as-judge eval scoring         │      │
                    │  └────────────────────────────────────┘      │
                    │                                              │
                    │  Guardrails: Budget(20) │ Timeout(30s)       │
                    │    Deadline(120s) │ CircuitBreaker(3)        │
                    │    Query Policy │ Rate Limiting              │
                    └─────────────────┬────────────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
    ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
    │   McpGateway     │   │  Bedrock LLM     │   │  OTEL Collector  │
    │   (Unified)      │   │  (Converse API)  │   │  (OTLP/HTTP)     │
    │                  │   │                  │   │                  │
    │ OAuth2 + Tokens  │   │ Claude Sonnet    │   │ Traces + Metrics │
    │ Rate Limiting    │   │ Hypothesis Refine│   │ GenAI Sem Conv   │
    │ Stub Fallback    │   │ Reasoning Gen    │   │ → Splunk HEC     │
    └────────┬─────────┘   │ Judge Scoring    │   └──────────────────┘
             │             └──────────────────┘
    ┌────────┼────────┬────────┬────────┬────────┬────────┐
    ▼        ▼        ▼        ▼        ▼        ▼        ▼
 Moogsoft  Splunk   Sysdig  SignalFx Dynatrace  SNOW   GitHub
 (AIOPS)   (Logs)  (Infra) (APM)    (APM)     (ITSM)  (DevOps)
```

---

## 4. Gap Analysis

### Remediated Since Initial Audit (2026-03-07 → 2026-03-08)

The following gaps from the initial audit have been **fully resolved**:

| Original Gap | Resolution |
|-------------|-----------|
| ~~Remediation engine disconnected~~ | `generate_remediation()` is now called from `investigate()` at `agent.py:440` |
| ~~Severity scaling not wired~~ | `detect_severity()` and `get_budget_for_severity()` are now called at `agent.py:300-301` |
| ~~No incident normalization schema~~ | `supervisor/incident_model.py` provides canonical `Incident` dataclass with `from_moogsoft()`, `from_servicenow()`, `from_pagerduty()` factory methods; `supervisor/intake.py` provides multi-source intake |
| ~~No structured RCA report~~ | `supervisor/rca_report.py` — `generate_rca_report()` + `render_markdown()` produce structured JSON and human-readable output |
| ~~No PagerDuty integration~~ | `supervisor/incident_model.py:Incident.from_pagerduty()` normalizes PagerDuty payloads |
| ~~Sequential playbook execution~~ | `_execute_playbook()` now groups steps by worker and submits concurrently via `_parallel_executor` (`agent.py:898-916`) |
| ~~No confidence calibration feedback loop~~ | `supervisor/confidence_calibrator.py` implements learned confidence calibration |
| ~~LLM-as-judge self-evaluation~~ | `supervisor/ground_truth_eval.py` compares against ground truth data in `eval/ground_truth.json` |
| ~~Database layer underutilized~~ | `database/persistence.py` — `persist_investigation()`, `persist_tool_usage()`, `persist_knowledge_entry()` are called from `investigate()` at `agent.py:529-543` |
| ~~System prompt not used~~ | `SUPERVISOR_SYSTEM_PROMPT` is imported and prepended in `llm.py:refine_hypothesis()` (line 213) and `llm.py:generate_reasoning()` (line 276) |

### Remaining Gaps

### High Priority

| Gap | Description |
|-----|-------------|
| **No webhook/event-driven intake** | Only synchronous HTTP POST. No async event intake (SQS, SNS, EventBridge, webhook) for real-time alert-driven investigations. |
| **No investigation state persistence** | Investigation state is entirely in-memory. If the process crashes mid-investigation, all progress is lost. Only final results are persisted. |

### Medium Priority

| Gap | Description |
|-----|-------------|
| **No topology/dependency graph** | ServiceNow CMDB CI details include `dependencies` field but no topology traversal logic. Cascading failure analysis relies on log-based chain detection rather than topology. |
| **Knowledge graph underutilized** | `knowledge/` module exists with graph store, retrieval engine, and metadata filtering, but is gated behind `KNOWLEDGE_GRAPH_ENABLED` env var and has no automated population pipeline. |
| **No human-in-the-loop** | No approval gate for remediation actions. The `verify_before_acting` flag in remediation templates is informational only. |

### Low Priority

| Gap | Description |
|-----|-------------|
| **No multi-tenant support** | Single-tenant architecture. No tenant isolation for shared AgentCore deployments. |
| **No investigation queue/throttling** | No concurrency control for simultaneous investigations. Each request spawns a full investigation. |
| **No configuration hot-reload** | All config from env vars at startup. Changing playbooks/budgets requires restart. |
| **Hardcoded model IDs** | Model IDs use specific version strings that will become outdated. |
| **No CI/CD pipeline** | No GitHub Actions workflows for automated testing, linting, or builds on PRs. |

---

## 5. Recommended Next Implementation Steps

> **Note:** Phases 1-5 from the original audit (2026-03-07) have been **completed**. The items below represent remaining work.

### ~~Phase 1: Wire Existing Dead Code~~ — COMPLETED

All previously dead code (severity, remediation, system prompt) is now wired into the investigation pipeline.

### ~~Phase 2: Incident Model and Report Format~~ — COMPLETED

Canonical `Incident` dataclass (`incident_model.py`) and structured RCA report (`rca_report.py`) are implemented.

### ~~Phase 3: Pipeline Improvements~~ — PARTIALLY COMPLETED

Parallel evidence gathering is implemented. Event-driven intake remains.

### ~~Phase 4: Data Layer Activation~~ — COMPLETED

Database persistence is wired. Knowledge graph pipeline remains underutilized.

### ~~Phase 5: Observability and Eval Improvements~~ — COMPLETED

Ground truth eval framework (`ground_truth_eval.py`) and confidence calibration (`confidence_calibrator.py`) are implemented.

### Remaining Work

1. **Event-driven intake** — Add SQS/SNS consumer for async alert ingestion. This allows PagerDuty webhooks, Moogsoft event streams, and CloudWatch alarms to trigger investigations automatically.

2. **Knowledge graph pipeline** — Build an automated pipeline that populates the institutional knowledge graph from completed investigations, enabling progressive accuracy improvement.

3. **CI/CD pipeline** — Add GitHub Actions workflows for automated testing, linting (ruff), and coverage enforcement on PRs.

4. **Topology/dependency graph** — Implement CMDB dependency traversal for cascading failure analysis instead of relying solely on log-based chain detection.

5. **Human-in-the-loop** — Add an approval gate for remediation actions, especially for high-severity incidents where `verify_before_acting` is set in remediation templates.
