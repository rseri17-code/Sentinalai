# SentinalAI (ObserveAI) — Security & Determinism Audit Report

**Date:** 2026-03-03
**Auditor Role:** Senior Enterprise Agent Architect
**Scope:** AgentCore-hosted observability agent (ObserveAI) with MCP tool servers (Splunk, Sysdig, SignalFx, Moogsoft, Dynatrace, ServiceNow, GitHub)
**Repository:** SentinalAI

---

## Executive Summary

The SentinalAI agent demonstrates a **strong deterministic architecture** with hardcoded playbooks, rule-based classification, evidence-weighted confidence scoring, and comprehensive OTEL observability. However, several gaps exist around **user-identity-scoped authorization**, **policy decision receipts**, **tool enumeration controls**, and **Entra-based authentication** (which is entirely absent — the system uses OAuth2 client_credentials via AWS Cognito instead).

**Overall Assessment: 5 of 7 categories PASS, 2 FAIL**

---

## Category Assessments

---

### 1. Policy-First Execution

**Verdict: PASS**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Intent classification before tool invocation | PASS | `supervisor/tool_selector.py:183-211` — `classify_incident()` runs keyword matching (then optional LLM fallback) **before** any playbook tool calls |
| Policy validation before execution | PASS | `supervisor/guardrails.py:155-171` — `validate_query()` enforces Splunk query allowlist and blocks dangerous patterns (`\|`, `eval`, `lookup`, `delete`) |
| Reasoning before tool invocation | PASS | `supervisor/agent.py:247` — Classification at Step 2 precedes playbook execution at Step 3 (`_execute_playbook` at line 256) |
| User prompts cannot dictate execution order | PASS | `supervisor/agent.py:667-705` — Playbook steps are iterated from `INCIDENT_PLAYBOOKS` dict (line 29-104 of `tool_selector.py`); user input only provides `incident_id` which is validated against `^[A-Za-z0-9_\-]{1,100}$` at `agentcore_runtime.py:44` |

#### Analysis

The investigation pipeline follows a strict 4-phase protocol:
1. **Fetch incident** (`_fetch_incident`, line 520)
2. **Classify** (`classify_incident`, line 247) — deterministic keyword match, LLM fallback only when `LLM_ENABLED=true` and keywords default
3. **Execute playbook** (`_execute_playbook`, line 256) — hardcoded step sequence from `INCIDENT_PLAYBOOKS`
4. **Analyze** (`_analyze_evidence`, line 279) — rule-based hypothesis scoring

User input (`incident_id`) passes through regex validation (`agentcore_runtime.py:44-58`) and never influences tool selection or execution order.

#### Gaps

- **G1.1:** `validate_query()` is defined in `guardrails.py:155-171` but **never called** from the main investigation pipeline. The `LogWorker` builds queries from `query_hint` templates in the playbook (`agent.py:714-716`), but `validate_query()` is not invoked before Splunk query dispatch. This is a **dead code** policy check.

#### Remediation

- **R1.1:** Wire `validate_query()` into `LogWorker.search_logs()` or `_build_params()` so every Splunk query is validated against the allowlist before MCP invocation.

#### Risk: **Medium** (G1.1 — policy bypass via crafted `query_hint` templates is low-probability since templates are hardcoded, but the defense-in-depth layer is non-functional)

---

### 2. Deterministic Tool Selection

**Verdict: PASS**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Tools selected via structured playbooks | PASS | `supervisor/tool_selector.py:29-104` — `INCIDENT_PLAYBOOKS` dict maps each of 10 incident types to ordered `(worker, action)` tuples |
| Deterministic routing | PASS | `supervisor/tool_selector.py:183-211` — `classify_incident()` uses keyword-first matching with ordered dict iteration; LLM fallback uses `temperature=0.0` (line 246) |
| LLM does not arbitrarily explore tools | PASS | LLM is used **only** for classification fallback (`classify_incident_llm`, line 214-283) and optional hypothesis refinement (`_llm_refine_hypotheses`, line 870-918). LLM **never** selects or invokes tools |
| Tool invocation bounded | PASS | `supervisor/guardrails.py:26-27` — `MAX_TOOL_CALLS_PER_CASE=20`, enforced via `ExecutionBudget.can_call()` checked at every `_call_worker()` and `_execute_playbook()` call |
| Deterministic tiebreak | PASS | `supervisor/agent.py:804` — `hypotheses.sort(key=lambda h: (-h.base_score, h.name))` ensures stable ordering |

#### Analysis

Tool selection is **fully deterministic** in the primary path:
- Incident classification: keyword matching against `CLASSIFICATION_KEYWORDS` (line 107-158)
- Playbook selection: direct dict lookup in `INCIDENT_PLAYBOOKS` (line 288)
- Each playbook has 3-6 ordered steps with explicit `(worker, action)` pairs
- Budget enforcement prevents runaway execution

The YAML `ToolSelector` class (line 359-571) provides catalog-aware selection but falls back to hardcoded playbooks, preserving determinism.

#### Gaps

- **G2.1:** When `LLM_ENABLED=true` and keyword classification defaults to `error_spike`, the LLM fallback (`classify_incident_llm`, line 214-283) introduces a non-deterministic classification path. While `temperature=0.0` reduces variance, LLM outputs are not guaranteed identical across model versions or API calls.

#### Remediation

- **R2.1:** Log and persist the LLM classification decision with model version for audit. Consider pinning model version in `classify_incident_llm` to a specific point-in-time snapshot.

#### Risk: **Low** (LLM fallback is gated behind env var and only triggers when keyword matching fails)

---

### 3. Authorization Model

**Verdict: FAIL**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Entra validation at agent boundary | **FAIL** | **Not implemented.** No Microsoft Entra (Azure AD) integration exists anywhere in the codebase |
| OAuth2 authentication present | PASS | `workers/mcp_client.py:155-317` — `OAuth2CredentialProvider` implements `client_credentials` grant via AWS Cognito |
| MCPs trust agent identity (not user) | **PARTIAL** | `workers/mcp_client.py:36-41` — Gateway mints per-resource TOKEN-B, but the agent's TOKEN-A is an M2M (machine-to-machine) credential, not a user-scoped token |
| MCPs restricted to whitelisted agents | **INDETERMINATE** | No agent identity whitelist or gateway target ACL is defined in the codebase. This is delegated to the AgentCore gateway configuration (external) |
| User identity propagated to MCPs | **FAIL** | No user identity, session token, or delegated credential is passed through the call chain. The agent authenticates as itself, not on behalf of a user |

#### Analysis

The audit criteria specified **Entra-based authentication validated at the agent boundary**. The actual implementation uses:

1. **OAuth2 client_credentials** via AWS Cognito (`mcp_client.py:155-317`)
2. **Static Bearer token** fallback (`mcp_client.py:106,720-721`)
3. **No-auth mode** for local dev (`mcp_client.py:724`)

The authentication architecture documented at `mcp_client.py:36-41`:
```
Agent (TOKEN-A) -> AgentCore Gateway
    Gateway validates TOKEN-A, maps audience
    Gateway mints TOKEN-B (per-resource) via credential provider
    Gateway -> Resource MCP Server (TOKEN-B) -> Backend API
```

This is a **service-to-service** (M2M) pattern. The agent authenticates as a service principal, not as a delegated user. There is:
- No Entra ID token validation
- No user identity extraction from incoming requests
- No user-scoped authorization claims
- No RBAC or permission checks based on caller identity
- No `Authorization` header extraction from the `/invocations` endpoint (`agentcore_runtime.py:189-209`)

The `/invocations` endpoint accepts any JSON body with a valid `incident_id` — there is no authentication or authorization check at the HTTP boundary.

#### Gaps

- **G3.1:** **No Entra (Azure AD) integration** — The task specification requires Entra-based authentication validated at the agent boundary. This is entirely absent.
- **G3.2:** **No user identity propagation** — The agent operates with its own M2M credentials. MCPs cannot enforce user-level access control because no user context is passed.
- **G3.3:** **No authentication on /invocations endpoint** — `agentcore_runtime.py:189-209` accepts unauthenticated requests. Any caller can trigger investigations.
- **G3.4:** **No agent identity whitelist** — The codebase does not define which agents are allowed to call which MCP targets. This is assumed to be handled by external AgentCore gateway configuration, but no verification exists in code.
- **G3.5:** **OAuth2 client_secret in environment variable** — `mcp_client.py:110` reads `GATEWAY_OAUTH2_CLIENT_SECRET` from env, which may be visible in process listings. The AWS Secrets Manager path (`mcp_client.py:114,320-343`) is optional.

#### Remediation

- **R3.1:** Implement Entra ID token validation middleware at the `/invocations` endpoint. Extract `sub`, `oid`, and `roles` claims from the Bearer token and propagate them through the investigation pipeline.
- **R3.2:** Add user identity (extracted from Entra token) to every MCP gateway call as a custom header (e.g., `X-User-Identity`) so downstream MCPs can enforce user-level authorization.
- **R3.3:** Add FastAPI middleware or AgentCore SDK hook to validate incoming authentication tokens before processing investigation requests.
- **R3.4:** Define an agent allowlist in configuration and validate the agent identity claim against it before processing requests.
- **R3.5:** Mandate AWS Secrets Manager for production by failing startup if `GATEWAY_OAUTH2_SECRET_ARN` is not set in production environments.

#### Risk: **HIGH**

---

### 4. Tool Enumeration Controls

**Verdict: FAIL**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Prompt cannot force tool listing | **PARTIAL** | User prompts only provide `incident_id`; no natural-language interface exposes tool discovery. However, the YAML catalog (`supervisor/sentinalai_mcp_tool_catalog.yaml`) with all 89 tools is shipped in the container image |
| Prompt cannot force pre-analysis execution | PASS | Investigation pipeline is hardcoded; `incident_id` input cannot alter execution flow |
| Tool metadata exposure restricted | **FAIL** | Full tool catalog (89 tools) is readable at `supervisor/sentinalai_mcp_tool_catalog.yaml` and loaded into memory via `ToolSelector._load_catalog()` (`tool_selector.py:376-397`) |
| Tool-to-server mapping exposed | **FAIL** | `workers/mcp_client.py:350-390` contains the full `_TOOL_TO_SERVER` mapping, `_SERVER_TO_TARGET` mapping (line 396-404), and gateway target naming convention (triple-underscore, line 407-424) in plaintext |

#### Analysis

The system's deterministic architecture naturally limits prompt-based tool enumeration attacks because:
- The input surface is a single `incident_id` string (not free-form natural language)
- Tool selection is playbook-driven, not LLM-driven
- The LLM (when enabled) only classifies incident types, never enumerates or selects tools

However, the **metadata exposure** surface is significant:
- `sentinalai_mcp_tool_catalog.yaml` (89 tools) is bundled in the Docker image
- `_TOOL_TO_SERVER` and `_SERVER_TO_TARGET` mappings reveal the entire backend topology
- Gateway target naming convention (`{Target}___{operation}`) is documented in comments

If an attacker gains container read access or the image is publicly available, the complete tool surface area is exposed.

#### Gaps

- **G4.1:** **Tool catalog bundled in container image** — The YAML catalog exposes all 89 tool names, server names, and capabilities. This reveals the attack surface to anyone with image access.
- **G4.2:** **Backend topology in source code** — `_TOOL_TO_SERVER` and `_SERVER_TO_TARGET` dicts reveal all 7 backend servers, their AgentCore target names, and routing conventions.
- **G4.3:** **No runtime tool enumeration restriction** — `ToolSelector.select_tools_for_incident()` can return tool lists based on any `incident_type` string, including types not in the hardcoded set (falls back to `error_spike` playbook via `get_playbook`, line 288).
- **G4.4:** **System prompt contains tool names** — `supervisor/system_prompt.py:12-13` lists all tool categories (Splunk, Sysdig, Dynatrace/SignalFx, ServiceNow, GitHub) in the system prompt.

#### Remediation

- **R4.1:** Move `sentinalai_mcp_tool_catalog.yaml` to a runtime-fetched configuration (e.g., AWS Parameter Store or AgentCore config) instead of bundling in the image.
- **R4.2:** Minimize metadata in deployed artifacts. Remove inline comments documenting gateway naming conventions from production builds.
- **R4.3:** Add runtime validation in `ToolSelector` to reject unknown `incident_type` values rather than defaulting to `error_spike` silently.
- **R4.4:** Strip backend infrastructure names from the system prompt. Use abstract labels instead of vendor names.

#### Risk: **Medium** (The agent is not prompt-driven, so prompt-based enumeration is infeasible. The risk is from image/source exposure revealing backend topology.)

---

### 5. Evidence & Receipts

**Verdict: PASS (with gaps)**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Trace ID per tool call | PASS | `supervisor/receipt.py:25` — `correlation_id` generated via `uuid.uuid4().hex[:12]` per receipt |
| Tool span | PASS | `supervisor/observability.py:157-205` — `trace_span()` creates OTEL spans (real or lightweight) for each investigation phase |
| Inputs recorded | PASS | `supervisor/receipt.py:70-78` — `ReceiptCollector.start()` records `tool`, `action`, `params` (with redaction) |
| Outputs recorded | **PARTIAL** | `supervisor/receipt.py:82-91` — `ReceiptCollector.finish()` records `result_count` and `status`, but not the full output payload |
| Timestamp | PASS | `supervisor/receipt.py:77` — `start_ts` via `time.monotonic()`; `end_ts` at line 84 |
| Policy decision reference | **FAIL** | No receipt or span attribute records **which policy rule** allowed or denied the tool call |
| Confidence evidence-weighted | PASS | `supervisor/agent.py:125-177` — `compute_confidence()` applies source bonuses, penalties, and corroboration scoring |

#### Analysis

The receipt system (`supervisor/receipt.py`) is well-designed:
- Every `_call_worker()` invocation creates a receipt via `receipts.start()` (line 465) and finalizes it via `receipts.finish()` (line 477)
- Sensitive parameters are redacted (`_redact_params`, line 112-118): `password`, `token`, `secret`, `api_key`, `authorization`
- Receipts are persisted to replay store (`agent.py:376-381`)
- Receipt summaries are emitted as OTEL metrics (`eval_metrics.py:340-371`)

#### Gaps

- **G5.1:** **No full output capture** — `ReceiptCollector.finish()` records `result_count` (heuristic count of items) but not the actual output data. For audit purposes, the raw output should be persisted (with redaction).
- **G5.2:** **No policy decision reference** — Neither receipts nor OTEL spans record which policy rule (playbook step, budget check, circuit breaker state, query validation result) authorized or blocked the tool call.
- **G5.3:** **Monotonic timestamps, not wall-clock** — `time.monotonic()` is used for timing (correct for elapsed calculations) but receipts lack a wall-clock `datetime` timestamp for cross-system correlation.
- **G5.4:** **Receipt correlation_id is not linked to OTEL trace ID** — The receipt's `correlation_id` (line 25) is a separate UUID from the OTEL trace. There is no field linking a receipt to its parent OTEL span/trace.

#### Remediation

- **R5.1:** Add optional full output capture to receipts (with configurable redaction), gated behind a `RECEIPT_CAPTURE_OUTPUT=true` flag.
- **R5.2:** Add a `policy_ref` field to Receipt recording the policy decision (e.g., `"playbook:timeout:step3"`, `"budget:remaining=15"`, `"circuit:ops_worker:closed"`).
- **R5.3:** Add `wall_clock_ts: str` (ISO 8601) field to Receipt alongside the monotonic timestamps.
- **R5.4:** Accept and store `trace_id` from the parent OTEL span context in the Receipt for cross-correlation.

#### Risk: **Medium** (Receipts exist and function but lack full audit-grade fidelity for enterprise compliance)

---

### 6. Bounded Execution Safety

**Verdict: PASS**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| Timeouts enforced per tool call | PASS | `supervisor/agent.py:470-473` — `ThreadPoolExecutor` with `future.result(timeout=self._call_timeout)` where `_call_timeout` defaults to 30s (`guardrails.py:28`) |
| Retries controlled | PASS | `supervisor/agent.py:448-453` — Max `1 + MAX_RETRIES_PER_CALL` (default 3 total attempts) with exponential backoff starting at 10ms |
| Circuit breakers scoped per investigation | PASS | `supervisor/agent.py:233` — `CircuitBreakerRegistry()` instantiated per-investigation; `guardrails.py:62-93` — threshold=3 failures, 60s recovery |
| Maximum tool-call count enforced | PASS | `supervisor/guardrails.py:40-54` — `ExecutionBudget` with `MAX_TOOL_CALLS_PER_CASE=20`; checked at every budget point in `_call_worker`, `_fetch_incident`, `_execute_playbook`, etc. |
| Rate limiting per MCP server | PASS | `workers/mcp_client.py:443-513` — `_TokenBucket` rate limiter with per-server RPM limits; checked in `McpGateway.invoke()` (line 604-610) |
| Budget scaling by severity | PASS | `supervisor/severity.py:54-59` — Critical=35 calls, high=30, medium=25, low=20, info=15 |
| Phase-level budgets | PASS | `supervisor/tool_selector.py:334-341` — `PHASE_BUDGETS` with per-phase `max_calls` and `max_seconds` limits |

#### Analysis

The bounded execution model is comprehensive with 5 layers of defense:

1. **W1: Circuit Breaker** (`guardrails.py:62-93`) — Per-worker, per-investigation isolation; threshold=3 failures; 60s recovery; OTEL metrics on state transitions
2. **W2: Budget** (`guardrails.py:40-54`) — Hard cap of 20 calls per investigation (configurable by severity)
3. **W4: Timeout** (`agent.py:470-473`) — 30s per-call timeout via `ThreadPoolExecutor`
4. **W5: Retry** (`agent.py:448-453`) — Max 2 retries with exponential backoff (10ms, 20ms)
5. **Rate Limiter** (`mcp_client.py:443-513`) — Per-server token-bucket with configurable RPM limits

#### Gaps

- **G6.1:** **No per-investigation wall-clock timeout** — While individual tool calls have 30s timeouts, there is no total investigation elapsed-time circuit breaker. A pathological case with 20 calls x 30s timeout = 10 minutes, exceeding the documented 60s target. The `agentcore.yaml` specifies `timeout_seconds: 120` at the runtime level, but this is not enforced in the agent code.
- **G6.2:** **ThreadPoolExecutor per call** — A new `ThreadPoolExecutor(max_workers=1)` is created for every `_call_worker()` invocation (`agent.py:471`). This creates and destroys threads per call rather than using a shared pool, which is inefficient and could leak resources under high load.

#### Remediation

- **R6.1:** Add a `time.monotonic()` check at the start of each `_call_worker()` invocation against a per-investigation deadline (e.g., `investigation_start + 120s`). Abort remaining playbook steps if exceeded.
- **R6.2:** Create a single `ThreadPoolExecutor` per `SentinalAISupervisor` instance and reuse it across calls, with proper shutdown in a context manager.

#### Risk: **Low** (All critical safety mechanisms are present; gaps are optimization and edge-case hardening)

---

### 7. Observability

**Verdict: PASS**

#### Evidence

| Check | Status | Code Reference |
|---|---|---|
| OTEL spans for reasoning | PASS | `supervisor/observability.py:157-205` — `trace_span("investigate")` wraps the full investigation; hypothesis analysis and LLM calls are nested within |
| OTEL spans for tool calls | PASS | `supervisor/eval_metrics.py:216-252` — `record_worker_call()` emits counter + histogram per tool call with worker_name, action, status, latency |
| OTEL spans for errors | PASS | `supervisor/observability.py:190-198` — Exception handling sets `StatusCode.ERROR` on OTEL span; worker errors recorded via `record_worker_call(status="error")` |
| OTEL cost metrics | PASS | `supervisor/eval_metrics.py:402-465` — `record_llm_usage()` emits `gen_ai.client.token.usage` and cumulative `sentinalai.llm.tokens.total` counters |
| OTEL token usage | PASS | `supervisor/eval_metrics.py:424-432` — Input and output token histograms with `gen_ai.token.type` attribute |
| GenAI semantic conventions applied | PASS | `supervisor/observability.py:87-92` — `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.operation.name` |
| OTEL Collector configured | PASS | `otel-collector-config.yaml` — OTLP/HTTP receiver on 4318, Splunk HEC exporters for traces/metrics/logs with retry and queue |

#### Analysis

The observability stack is production-grade:

**20+ OTEL metric instruments** (`eval_metrics.py`):
- `sentinalai.investigations.total` (counter)
- `sentinalai.confidence.distribution` (histogram)
- `sentinalai.investigation.duration_ms` (histogram)
- `sentinalai.worker_calls.total` (counter)
- `sentinalai.worker_call.duration_ms` (histogram)
- `sentinalai.circuit_breaker.trips` (counter)
- `sentinalai.budget.exhausted` (counter)
- `sentinalai.evidence.completeness` (counter)
- `sentinalai.receipts.total_calls` (histogram)
- `sentinalai.eval.score` (histogram)
- `sentinalai.judge.score` (histogram)
- `gen_ai.client.token.usage` (histogram)
- `gen_ai.client.operation.duration` (histogram)
- `sentinalai.llm.calls.total` (counter)
- `sentinalai.llm.tokens.total` (counter)

**GenAI semantic conventions** applied to investigation span attributes:
- `gen_ai.system = "sentinalai"`
- `gen_ai.operation.name = "investigate"`
- `gen_ai.request.model` (from LLM calls)
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`

**Custom eval attributes** for Splunk dashboards:
- `sentinalai.incident_type`, `sentinalai.service`, `sentinalai.confidence`
- `sentinalai.root_cause`, `sentinalai.tool_calls`, `sentinalai.budget_remaining`
- `sentinalai.hypothesis_count`, `sentinalai.winner_hypothesis`

**Graceful degradation**: When OTEL SDK is absent, lightweight `Span` objects (`observability.py:115-150`) provide the same interface with structured JSON logging.

#### Gaps

- **G7.1:** **No per-tool-call OTEL span** — Individual tool calls emit metrics (`record_worker_call`) but do not create child OTEL spans. The investigation trace has a single root span with attributes, not a span tree showing the call sequence.
- **G7.2:** **No cost estimation metric** — Token counts are recorded, but there is no `sentinalai.investigation.estimated_cost` metric that maps tokens to dollars for FinOps dashboards.

#### Remediation

- **R7.1:** Wrap each `_call_worker()` invocation in a child `trace_span(f"tool:{worker_name}.{action}")` to produce a proper span tree for distributed tracing.
- **R7.2:** Add a cost estimation metric based on token counts and model pricing (e.g., `estimated_cost = input_tokens * input_price + output_tokens * output_price`).

#### Risk: **Low** (Observability is comprehensive; gaps are refinements, not missing capabilities)

---

## Summary Matrix

| # | Category | Verdict | Risk | Key Gap |
|---|---|---|---|---|
| 1 | Policy-First Execution | **PASS** | Medium | `validate_query()` is dead code — never called from pipeline |
| 2 | Deterministic Tool Selection | **PASS** | Low | LLM classification fallback introduces non-determinism |
| 3 | Authorization Model | **FAIL** | **HIGH** | No Entra auth; no user identity; unauthenticated `/invocations` endpoint |
| 4 | Tool Enumeration Controls | **FAIL** | Medium | Full tool catalog (89 tools) and backend topology bundled in image |
| 5 | Evidence & Receipts | **PASS** | Medium | No policy decision reference; no full output capture; no OTEL trace linkage |
| 6 | Bounded Execution Safety | **PASS** | Low | No per-investigation wall-clock deadline |
| 7 | Observability | **PASS** | Low | No per-tool-call child spans; no cost estimation metric |

---

## Critical Findings (Priority Order)

### P0 — No Authentication on `/invocations` Endpoint (HIGH)

- **File:** `agentcore_runtime.py:189-209`
- **Finding:** The POST `/invocations` endpoint accepts any JSON body without authentication. Any network-reachable caller can trigger investigations.
- **Impact:** Unauthorized access to all backend observability systems (Splunk, Sysdig, Moogsoft, ServiceNow, GitHub) via the agent's M2M credentials.
- **Remediation:** Add authentication middleware (Entra ID JWT validation or Cognito token validation) to the `/invocations` endpoint before any processing.

### P0 — No Entra-Based Authentication (HIGH)

- **File:** Entire codebase
- **Finding:** The task specification requires Entra-based authentication validated at the agent boundary. No Entra/Azure AD integration exists. The system uses AWS Cognito OAuth2 client_credentials for **gateway-to-MCP** auth only.
- **Impact:** The agent cannot participate in Entra-governed enterprise identity federations. User-level access control is impossible.
- **Remediation:** Implement Entra ID token validation at the agent HTTP boundary. Extract user claims and propagate through the call chain.

### P1 — `validate_query()` Never Called (MEDIUM)

- **File:** `supervisor/guardrails.py:155-171`
- **Finding:** The Splunk query validation function exists but is never invoked from the investigation pipeline. Playbook-templated queries bypass validation.
- **Impact:** If playbook templates are modified to include dangerous patterns, the validation safety net does not catch them.
- **Remediation:** Call `validate_query()` in `LogWorker.search_logs()` before dispatching to the MCP gateway.

### P1 — No Policy Decision Receipts (MEDIUM)

- **File:** `supervisor/receipt.py:19-37`
- **Finding:** Receipts record what was called and when, but not **why** (which policy rule authorized the call, which budget was consumed, which circuit breaker state was checked).
- **Impact:** Audit trail lacks the policy decision chain required for enterprise compliance.
- **Remediation:** Add `policy_ref: str` field to Receipt and populate it from the calling context.

---

## Gap Registry

| ID | Category | Severity | Description | File | Line |
|---|---|---|---|---|---|
| G1.1 | Policy-First | Medium | `validate_query()` is dead code | `guardrails.py` | 155-171 |
| G2.1 | Deterministic | Low | LLM classification non-deterministic | `tool_selector.py` | 214-283 |
| G3.1 | Authorization | High | No Entra ID integration | N/A | N/A |
| G3.2 | Authorization | High | No user identity propagation | `mcp_client.py` | 584-623 |
| G3.3 | Authorization | High | Unauthenticated `/invocations` | `agentcore_runtime.py` | 189-209 |
| G3.4 | Authorization | Medium | No agent identity whitelist | N/A | N/A |
| G3.5 | Authorization | Medium | Client secret in env var | `mcp_client.py` | 110 |
| G4.1 | Enumeration | Medium | Tool catalog in container image | `sentinalai_mcp_tool_catalog.yaml` | - |
| G4.2 | Enumeration | Medium | Backend topology in source | `mcp_client.py` | 350-404 |
| G4.3 | Enumeration | Low | No unknown incident_type rejection | `tool_selector.py` | 288 |
| G4.4 | Enumeration | Low | Vendor names in system prompt | `system_prompt.py` | 12-13 |
| G5.1 | Receipts | Medium | No full output capture | `receipt.py` | 82-91 |
| G5.2 | Receipts | Medium | No policy decision reference | `receipt.py` | 19-37 |
| G5.3 | Receipts | Low | Monotonic timestamps only | `receipt.py` | 77 |
| G5.4 | Receipts | Low | No OTEL trace ID linkage | `receipt.py` | 25 |
| G6.1 | Bounded Exec | Low | No investigation-level deadline | `agent.py` | 212-416 |
| G6.2 | Bounded Exec | Low | ThreadPoolExecutor per call | `agent.py` | 471 |
| G7.1 | Observability | Low | No per-tool child spans | `agent.py` | 422-514 |
| G7.2 | Observability | Low | No cost estimation metric | `eval_metrics.py` | - |

---

## Conclusion

SentinalAI demonstrates strong engineering in deterministic orchestration, bounded execution, and OTEL observability. The architecture correctly separates LLM reasoning from tool invocation, uses hardcoded playbooks for deterministic tool selection, and provides comprehensive metrics and receipts.

The **critical gap** is the authorization model: the system lacks user-identity-scoped authentication at the agent boundary, relies entirely on M2M credentials, and exposes the `/invocations` endpoint without authentication. This must be remediated before production deployment in an Entra-governed enterprise environment.

Secondary gaps around dead-code policy validation, tool catalog exposure, and receipt audit fidelity should be addressed in the next development sprint.
