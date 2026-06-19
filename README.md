# SentinalAI

**Autonomous, self-correcting SRE agent for production incident root cause analysis.**

SentinalAI runs a full investigation loop — fetching alert metadata, gathering multi-source evidence, applying deterministic correlation rules, and generating evidence-weighted RCA — without a human in the loop. The system is designed for production SRE environments where reproducibility, auditability, and bounded execution cost are non-negotiable.

```
Incident In → [Fetch] → [Classify] → [Planner/Playbook] → [Evidence Gathering]
           → [Deterministic Scoring] → [Self-Correction Loop] → [RCA Out]
```

As of this release, SentinalAI implements the full **Think→Act→Observe** agentic loop with Working Memory, a Policy Gate pre-dispatch safety layer, network path intelligence via ThousandEyes MCP, and a multi-round self-correction harness — bridging the gap from deterministic playbook execution toward genuine closed-loop autonomous investigation.

---

## Table of Contents

- [Architecture](#architecture)
- [Investigation Pipeline](#investigation-pipeline)
- [Agentic Planner — Think→Act→Observe](#agentic-planner--thinkactobserve)
- [Self-Correction Harness](#self-correction-harness)
- [Working Memory](#working-memory)
- [Hypothesis Engine](#hypothesis-engine)
- [Guardrails & Policy Gate](#guardrails--policy-gate)
- [Workers & MCP Integration](#workers--mcp-integration)
- [ThousandEyes Network Intelligence](#thousandeyes-network-intelligence)
- [Intelligence Foundation](#intelligence-foundation)
- [Pattern Intelligence & Learning](#pattern-intelligence--learning)
- [Sentinel Wiki](#sentinel-wiki)
- [LLM Integration](#llm-integration)
- [Memory](#memory)
- [Observability](#observability)
- [AGUI — Agentic UI](#agui--agentic-ui)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Feature Flags](#feature-flags)
- [Testing](#testing)
- [Deployment](#deployment)
- [Project Structure](#project-structure)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Investigation Harness                             │
│  Layer 1: Pre-flight context (calibration, experiences, PIL predictions)  │
│  Layer 2: Multi-round self-correction loop (gap-fill + reanalyze)         │
│  Layer 3: Post-flight learning (strategy_evolver, experience_store)       │
│                                                                            │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    SentinalAI Supervisor                            │  │
│  │                                                                     │  │
│  │  FETCH → CLASSIFY → [PLANNER or PLAYBOOK] → ANALYZE → PERSIST      │  │
│  │              │              │                    │                  │  │
│  │         Keyword        Think→Act→Observe     Deterministic +        │  │
│  │         matching       Loop (optional)       LLM refinement +       │  │
│  │         10 types       or fixed 3-5 step     self-critique +        │  │
│  │                        playbook              evidence gates         │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                   │                                                        │
│         ┌─────────▼──────────┐                                            │
│         │   Working Memory   │  Tracks hypothesis, confirmed facts,        │
│         │  (per-round state) │  open questions, tool calls, confidence     │
│         └─────────┬──────────┘  trajectory across correction rounds       │
│                   │                                                        │
│         ┌─────────▼──────────┐                                            │
│         │   Policy Gate      │  Validate → Budget → Scope → Allow/Reject  │
│         │  (pre-dispatch)    │  before every MCP tool call                 │
│         └─────────┬──────────┘                                            │
└───────────────────┼────────────────────────────────────────────────────────┘
                    │
    ┌───────────────▼──────────────────────────────────────────────────┐
    │                  Worker Layer (14 workers)                         │
    │                                                                    │
    │  OpsWorker      LogWorker       MetricsWorker    ApmWorker         │
    │  (Moogsoft)     (Splunk)        (Sysdig)         (Dynatrace/SFx)   │
    │                                                                    │
    │  KnowledgeWorker  ItsmWorker    DevopsWorker     NetworkWorker     │
    │  (AgentCore Mem)  (ServiceNow)  (GitHub)         (ThousandEyes)    │
    │                                                                    │
    │  ConfluenceWorker  CodeWorker   GitWorker        VisualEvidenceWkr │
    │  (Confluence)      (AST/static) (git blame/log)  (screenshot/UI)  │
    └──────────────────────┬───────────────────────────────────────────┘
                           │
    ┌──────────────────────▼───────────────────────────────────────────┐
    │              AgentCore Gateway + Direct MCP Endpoints             │
    │  OAuth2 client_credentials · Rate limiting · 401 auto-retry       │
    │  Moogsoft · Splunk · Sysdig · Dynatrace · SignalFx · ServiceNow  │
    │  GitHub · ThousandEyes (port 8004, Bearer token)                  │
    └──────────────────────────────────────────────────────────────────┘
```

### Design Principles

| Principle | Implementation |
|---|---|
| **Deterministic first** | Incident classification is keyword-based. Hypothesis scoring is rule-based. Tiebreaks are alphabetical. Same incident + same input = same output. |
| **Proof-driven** | No causal artifact → confidence stays below 80. Missing sources apply score penalties. Multi-source corroboration is the only path to high confidence. |
| **Bounded** | 20 tool calls max. 30s timeout per call. Phase-level sub-budgets. Circuit breakers on 3 consecutive failures. Policy Gate rejects calls when budget is exhausted. |
| **Self-correcting** | The harness runs up to 2 additional analysis rounds when quality is below gate threshold, injecting Working Memory state into each round. |
| **Observable** | OTEL spans with GenAI semantic conventions. 20+ metrics. Every tool call produces an auditable receipt. Full replay artifact support. |
| **Graceful degradation** | LLM is optional. ThousandEyes is feature-flagged off. Planner falls back to fixed playbook when LLM is unavailable. All enrichment paths are non-blocking. |

---

## Investigation Pipeline

### Phase 0: Harness Pre-flight

Before the first investigation pass, the harness loads:
- Confidence calibration state from `neural_confidence_calibrator`
- Similar past investigation results from `experience_store`
- Strategy quality scores from `strategy_evolver`
- Predictive incubation (PIL) score from `adaptive_thresholds`

This context is injected into the agent before it starts, so it knows its own historical accuracy for this service and incident type.

### Phase 1: Fetch & ITSM Enrichment

`OpsWorker.get_incident_by_id()` fetches alert metadata from Moogsoft. If no incident is found, the investigation short-circuits.

**Phase 1b — ITSM Enrichment** (non-blocking): `ItsmWorker` fetches CI tier, owner, SLA, known errors, and similar ServiceNow incidents. Failure here does not block the investigation.

### Phase 2: Classify

`classify_incident(summary)` maps the incident summary to one of 10 types via keyword matching. No LLM involved. Default fallback: `error_spike`.

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

### Phase 3: Execute Planner or Playbook

Either the **AgenticPlanner** (Think→Act→Observe) or the **fixed playbook** executes, depending on the `AGENTIC_PLANNER` flag:

**Fixed playbooks** (deterministic, default):
```
timeout:        get_incident → search_logs(timeout) → golden_signals → query_metrics → get_change_data → get_network_alerts
latency:        get_incident → search_logs(latency) → golden_signals → query_metrics → get_change_data → get_network_evidence
network:        get_incident → search_logs(dns) → golden_signals → search_logs(connection) → get_change_data → get_network_evidence
oomkill:        get_incident → search_logs(OOMKilled) → query_metrics(memory) → get_events → search_logs(heap)
error_spike:    get_incident → search_logs(error) → golden_signals → get_change_data → get_events
saturation:     get_incident → golden_signals → query_metrics(cpu) → search_logs(cpu) → get_change_data
cascading:      get_incident → search_logs(cascade) → golden_signals → query_metrics → get_change_data
missing_data:   get_incident → search_logs(connection) → golden_signals → get_events → get_change_data
flapping:       get_incident → search_logs(flapping) → golden_signals → query_metrics → get_change_data
silent_failure: get_incident → search_logs(pipeline) → golden_signals → search_logs(cache) → query_metrics
```

**Phase 3b — DevOps Enrichment** (proof-gated): Only triggered when change evidence already exists in logs or ITSM. Calls `DevopsWorker` for GitHub deployment details, PR metadata, and CI/CD status.

**Phase 3c — Experience Retrieval**: Queries `KnowledgeWorker.search_similar()` for past investigations on the same service, if budget allows.

### Phase 4: Analyze Evidence

The core scoring engine. See [Hypothesis Engine](#hypothesis-engine). Network evidence from ThousandEyes is incorporated here as an additive confidence delta (up to +40 points, capped at 95 total).

### Phase 5: Persist & Learn

After a successful investigation:
- `experience_store` persists the result for future pre-flight context
- `strategy_evolver` records quality outcome for per-type strategy calibration
- `learning_loop` runs a step update
- `incident_dna` fingerprints the investigation for pattern matching
- If quality score ≥ threshold, `sentinel_wiki` writes a structured receipt

---

## Agentic Planner — Think→Act→Observe

**Feature flag**: `AGENTIC_PLANNER=false` (default off — zero behavior change until enabled)

When enabled, replaces the fixed playbook with a dynamic reasoning loop:

```
┌─────────────────────────────────────────────────────┐
│               AgenticPlanner Loop                    │
│                                                      │
│  ┌──────┐    ┌─────┐    ┌─────────┐                 │
│  │THINK │───▶│ ACT │───▶│ OBSERVE │──┐              │
│  │      │    │     │    │         │  │              │
│  │ LLM  │    │ Run │    │ Merge   │  │ Loop until   │
│  │selects    │ tool│    │ result  │  │ done=true or │
│  │next  │    │call │    │into ev. │  │ max_iters    │
│  │tool  │    │     │    │         │◀─┘              │
│  └──────┘    └─────┘    └─────────┘                 │
│                                                      │
│  Max iterations: PLANNER_MAX_ITERATIONS (default 10) │
│  Fallback: static playbook when LLM unavailable      │
│  Audit: _planner_trace attached to evidence dict     │
└─────────────────────────────────────────────────────┘
```

**Think**: LLM receives current evidence summary + available worker/action pairs and returns a `PlannerStep` (JSON): `{worker, action, params, reasoning, done}`.

**Act**: The supervisor executes the selected worker action with budget, circuit breaker, and Policy Gate checks.

**Observe**: Result is merged into the running evidence dict. The next Think step sees all prior observations.

**Done**: LLM sets `done=true` when it believes sufficient evidence has been gathered, or when the iteration cap is hit.

When the LLM returns invalid JSON or is unavailable, the planner transparently falls back to the fixed playbook for that iteration.

---

## Self-Correction Harness

`supervisor/agent_harness.py` wraps the supervisor in three layers of self-awareness:

### Layer 1 — Pre-flight Context

Loads calibration state, experience matches, and PIL predictions before the first pass. The agent enters the investigation knowing its baseline accuracy for this service/type combination.

### Layer 2 — Multi-round Self-correction Loop

After the initial investigation:

1. `online_evaluator` and `self_critique` score the result quality
2. If score < `HARNESS_QUALITY_GATE` (default 0.70), the harness extracts `gap_queries` from the critique
3. Executes targeted gap-fill: runs the specific missing worker calls directly
4. Enriches evidence with gap results + `_working_memory` state dict
5. Calls `supervisor.reanalyze(enriched_evidence)` — skips the playbook re-run (~90–110s saved)
6. Repeats up to `HARNESS_MAX_ROUNDS` (default 2) or until quality is satisfactory or score improvement plateaus

Stuck detection: if consecutive rounds produce < `HARNESS_MIN_IMPROVEMENT` (default 0.04), the loop exits early.

### Layer 3 — Post-flight Learning

- Records outcome to `strategy_evolver` (per-type quality calibration)
- Persists quality score to `adaptive_thresholds`
- Stores high-quality investigations in `experience_store`
- Runs `learning_loop` step
- Emits a `HARNESS_REFLECTION` event for UI display

---

## Working Memory

`supervisor/working_memory.py` provides structured investigation state across harness correction rounds, replacing fragile cross-method thread-local state.

```python
@dataclass
class WorkingMemory:
    incident_id: str
    current_hypothesis: str       # Best hypothesis from last analysis round
    confirmed_facts: list[str]    # Deduplicated evidence-backed facts
    open_questions: list[str]     # Gaps from self_critique output
    tools_called: list[str]       # Deduplicated tool call record
    confidence_trajectory: list[float]  # Per-round confidence scores
    round_num: int                # Current correction round number
```

**`update_from_result(result)`**: Extracts `root_cause` → `current_hypothesis`, normalizes confidence (0–100 or 0.0–1.0) into `confidence_trajectory`, mines `reasoning`/`evidence_timeline` → `confirmed_facts`, extracts `_critique.gaps` → `open_questions`.

**`is_improving()`**: Returns `True` if the last confidence trajectory entry exceeds the previous, enabling stuck-detection in the harness loop.

**`to_context_dict()`**: Returns a 6-key dict injected as `_working_memory` into enriched evidence, so each reanalysis pass sees the accumulated investigation state.

---

## Hypothesis Engine

SentinalAI does not ask an LLM "what went wrong." It generates hypotheses deterministically, scores them against evidence, and optionally refines with an LLM.

### Multi-Hypothesis Generation

Each incident type has a dedicated analyzer that produces 1–3 `Hypothesis` objects:

```python
@dataclass
class Hypothesis:
    name: str              # "downstream_slow_queries"
    root_cause: str        # Deterministic RCA string
    base_score: float      # Initial confidence (0–100)
    evidence_refs: list    # ["logs:timeout", "signals:latency"]
    reasoning: str         # Causal explanation
```

10 type-specific analyzers inspect raw evidence (logs, signals, metrics, events, changes, ITSM context, DevOps context, network evidence) for patterns.

### Evidence-Weighted Confidence Scoring

Every hypothesis score is refined by `compute_confidence()`:

```
score = base
  + min(log_count, 5)                   # +1 per log entry, max +5
  + 2 if anomaly_detected                # golden signals anomaly
  + 1 if metrics have pattern            # structured metric match
  + (source_count × 2)                  # cross-signal corroboration
  + (corroborating_sources × 2)         # explicit evidence refs
  - 5 if signals missing                 # penalty
  - 3 if metrics missing                 # penalty
  + network_confidence_delta (0–40)     # ThousandEyes correlation rules
```

Result clamped to `[0, 95]`. Confidence above 80 requires a causal artifact — this is an invariant, not a soft limit.

### Winner Selection

Hypotheses sort by `(-score, name)`. Alphabetical tiebreak ensures determinism across all tie scenarios.

### LLM Refinement (Optional)

If `LLM_ENABLED=true`, two non-blocking passes run after deterministic scoring:

1. **Hypothesis refinement**: Re-ranks hypotheses with full evidence context via Bedrock Converse. Deterministic result is authoritative if LLM fails.
2. **Reasoning generation**: Produces human-readable causal chain referencing specific evidence artifacts (capped at 512 tokens).

---

## Guardrails & Policy Gate

### Execution Guardrails (W1–W5)

| Layer | Mechanism | Limits |
|---|---|---|
| **W1 Circuit Breaker** | Per-investigation `CircuitBreakerRegistry`. Trips on N consecutive failures. Recovery probe after interval. | threshold=3, recovery=60s |
| **W2 Multi-Hypothesis** | 1–3 hypotheses per incident type. Never a single guess. Deterministic tiebreak. | 10 type-specific analyzers |
| **W3 Evidence-Weighted** | Confidence penalizes missing sources, rewards corroboration. | Bounded [0, 95] |
| **W4 Timeout** | Every worker call wrapped in `ThreadPoolExecutor` with hard timeout. | 30s default |
| **W5 Retry** | Exponential backoff: 10ms × 2^attempt. Only retries if budget remaining. | max_retries=2 |

**Execution Budget**: 20 tool calls per investigation (configurable). Phase-level sub-budgets:
```
initial_context:    2 calls (fetch + classify)
itsm_enrichment:    3 calls
evidence_gathering: 8 calls (playbook/planner steps)
change_correlation: 3 calls
devops_enrichment:  2 calls (proof-gated)
historical_context: 2 calls
```

### Policy Gate

**Feature flag**: `POLICY_GATE_ENABLED=false` (default off)

`supervisor/policy_gate.py` implements a pre-dispatch safety layer that intercepts every MCP tool call in `McpGateway.invoke()` before it reaches the wire:

```
Every tool call → Validate → Budget → Scope → Allow / Reject
```

| Check | Condition | Decision |
|---|---|---|
| `_validate` | `params` is not a dict | REJECT |
| `_check_budget` | `budget_remaining <= 0` | REJECT |
| `_check_scope` | Configurable tool allowlist | ALLOW (extensible) |

Returns a `PolicyResult` with `decision` (ALLOW/REJECT/WARN), `reason`, and optional `suggested_action`. Uses a lazy import inside `try/except ImportError` in `mcp_client.py` to guarantee zero circular-import risk.

**Query Validation**: Splunk queries are validated against an allowlist and checked for injection patterns (`|`, `eval`, `lookup`, `delete`).

---

## Workers & MCP Integration

All workers extend `BaseWorker`, which provides action registration, execution dispatch, timing, and structured error handling.

| Worker | Transport | Key Actions |
|---|---|---|
| `OpsWorker` | AgentCore → Moogsoft | `get_incident_by_id` |
| `LogWorker` | AgentCore → Splunk | `search_logs`, `get_change_data` |
| `MetricsWorker` | AgentCore → Sysdig | `query_metrics`, `get_resource_metrics`, `get_events` |
| `ApmWorker` | AgentCore → Dynatrace/SignalFx | `get_golden_signals`, `check_latency` |
| `KnowledgeWorker` | AgentCore Memory | `search_similar`, `store_result` |
| `ItsmWorker` | AgentCore → ServiceNow | `get_ci_details`, `search_incidents`, `get_change_records`, `get_known_errors` |
| `DevopsWorker` | AgentCore → GitHub | `get_recent_deployments`, `get_pr_details`, `get_commit_diff`, `get_workflow_runs` |
| `NetworkWorker` | Direct HTTP → ThousandEyes MCP (port 8004) | `get_network_evidence`, `get_network_alerts`, `check_network_health` |
| `ConfluenceWorker` | AgentCore → Confluence | Runbook search, doc retrieval |
| `CodeWorker` | Local/static | AST analysis, static evidence extraction |
| `GitWorker` | AgentCore → git | `git_blame`, `git_log`, commit correlation |
| `VisualEvidenceWorker` | Local | Screenshot capture, UI-state evidence |

### MCP Gateway Client

`workers/mcp_client.py` manages the connection to all MCP tool servers:

- **AgentCore Gateway** (preferred): Single HTTPS endpoint. Tool names map to `{TargetName}___{operation}` (e.g., `SplunkTarget___search_oneshot`)
- **OAuth2**: Client credentials grant with automatic token refresh. Supports Cognito, Secrets Manager, and static Bearer token fallback
- **Per-server rate limiting**: Token bucket algorithm with configurable RPM per target
- **Legacy fallback**: `invoke_inline_agent` via boto3 when gateway is not configured
- **Stub mode**: Returns empty/minimal responses for local development when neither gateway nor ARNs are configured
- **401 auto-retry**: Token invalidation and retry on authentication failures

---

## ThousandEyes Network Intelligence

Network path intelligence is provided via a dedicated ThousandEyes MCP server (separate from the AgentCore gateway, running on port 8004 with Bearer token auth).

**Feature flag**: `ENABLE_THOUSANDEYES_RCA=false` (default off)

### Evidence Model

`integrations/thousandeyes/normalizer.py` normalizes raw TE responses into `NetworkEvidence` — a typed dataclass covering availability, packet loss, latency, jitter, DNS time, connect time, SSL time, response code, error type, path hops, changed hops, BGP route changes, affected scope, confidence, and recommended owner.

**Deterministic confidence scoring** (0.0–1.0, no LLM):
```
availability == 0%      → +0.40   |  packet_loss > 5%   → +0.10
availability < 50%      → +0.20   |  packet_loss > 20%  → +0.20
high-confidence error   → +0.15   |  connect_time > 500ms → +0.10
changed_hops > 0        → +0.10   |  bgp_route_changed  → +0.15
dns_time > 500ms        → +0.10
Scope multiplier: global ×1.2, regional ×1.1, capped at 1.0
```

### Correlation Rules

`integrations/thousandeyes/correlation.py` implements 6 deterministic rules:

| Rule | ID | Condition | Confidence Delta | Owner |
|---|---|---|---|---|
| Network-induced latency | TE-CORR-001 | packet_loss>5% + connect_time>200ms | +0.30 | network |
| External network degradation | TE-CORR-002 | changed_hops + cloud agents <80% avail | +0.35 | network |
| Infra healthy / path degraded | TE-CORR-003 | 2+ cloud agents <80% + internal APM healthy | +0.40 | network |
| DNS root cause | TE-CORR-004 | dns-server test + DNS error type | +0.40 | dns |
| Regional ISP issue | TE-CORR-005 | degraded+healthy agent split + common ASN | +0.35 | isp |
| SaaS provider outage | TE-CORR-006 | all cloud agents 0% availability (2+) | +0.40 | saas |

Rules run in parallel and are sorted by confidence delta. The top matched rule's delta is added to the investigation confidence score (capped at +40 total, confidence ceiling 95).

### Fixture Mode

`TE_USE_FIXTURES=true` (CI default) loads 16 sanitized JSON fixture files instead of calling the live TE API. IP addresses use RFC 5737 ranges (192.0.2.x, 198.51.100.x, 203.0.113.x). ASNs use RFC 5398 documentation range (AS64496–AS64511). Token is never logged, never in code, never in Git.

---

## Intelligence Foundation

`intelligence/` provides a graph-backed investigation intelligence layer:

| Module | Purpose |
|---|---|
| `incident_graph.py` | Graph of incidents, services, causal artifacts, and their relationships |
| `dependency_graph.py` | Service dependency topology for blast radius inference |
| `evidence_graph.py` | Links evidence nodes to hypotheses and services |
| `pattern_intelligence.py` | Cross-incident pattern detection and signature matching |
| `pattern_signature.py` | Fingerprint schema for recurring failure patterns |
| `change_tracker.py` | Correlates change events (deploys, config changes) with incident onset |
| `resolution_memory.py` | Stores and retrieves successful resolution paths |
| `investigation_store.py` | Persistent store for completed investigation artifacts |
| `bridge.py` | Connects intelligence outputs back to the supervisor analysis phase |

---

## Pattern Intelligence & Learning

SentinalAI is designed to improve with each investigation:

### Self-Improvement Loop

`supervisor/self_improvement_loop.py` runs after each investigation and:
- Updates per-type strategy weights in `strategy_evolver`
- Adjusts per-service confidence calibration in `adaptive_thresholds`
- Promotes strong patterns to `pattern_registry`
- Records quality metrics to `neural_confidence_calibrator`

### Neural Confidence Calibrator

`supervisor/neural_confidence_calibrator.py` applies learned Platt-scaling corrections to raw confidence scores based on historical accuracy for the specific `(incident_type, service)` pair. Falls back to raw score when insufficient history exists.

### Neural Quality Net

`supervisor/neural_quality_net.py` predicts investigation quality before the harness correction loop runs, enabling early exit when quality is predicted to be high — saving correction round cost for straightforward incidents.

### Pattern Registry

`supervisor/pattern_registry.py` stores `(incident_type, root_cause_fingerprint) → PatternRecord` mappings. Matched patterns surface `match_count` and `top_hypothesis` in the harness reflection, giving the UI context on how often this failure mode has been seen before.

### Blast Radius & Cascade Detection

`supervisor/blast_radius.py` and `supervisor/cascade_tracker.py` analyze whether a given incident is expanding (more services, more alerts) or cascading from a known upstream failure. Results influence urgency scoring and investigation prioritization.

---

## Sentinel Wiki

`sentinel_wiki/` is SentinalAI's institutional memory layer — a structured, searchable knowledge base built from completed investigations.

After each high-quality investigation:
- `receipt_writer.py` writes a structured receipt (incident ID, RCA, confidence, evidence timeline, resolution actions)
- `ingester.py` indexes the receipt into full-text and entity indexes
- `vector_index.py` creates a semantic embedding for similarity search
- `pattern_promoter.py` promotes high-confidence recurring patterns to `sentinel_wiki/patterns/`

The wiki is queryable via `searcher.py` and surfaces:
- Past incidents on the same service
- Known failure patterns matching the current signature
- Approved runbook references via Confluence integration
- Topology context from `sentinel_wiki/topology/`

---

## LLM Integration

### Bedrock Converse

`supervisor/llm.py` wraps the Bedrock Converse API for optional enhancement passes:

1. **Hypothesis refinement**: Re-ranks hypotheses with full evidence context. Deterministic path remains authoritative if LLM fails.
2. **Reasoning generation**: Human-readable causal chain, capped at 512 tokens.
3. **Planner think step**: In AgenticPlanner mode, the LLM selects the next tool and arguments per iteration.

All three are non-blocking. Graceful degradation is built in at every call site.

### LLM-as-Judge

`supervisor/llm_judge.py` evaluates investigation quality across 6 dimensions:

| Dimension | What It Measures |
|---|---|
| `root_cause_accuracy` | Does RCA match expected? |
| `causal_reasoning` | Is the causal chain clear and non-circular? |
| `evidence_usage` | Are claims backed by specific evidence references? |
| `timeline_quality` | Is the evidence timeline ordered and progressive? |
| `actionability` | Could an on-call SRE act on this immediately? |
| `overall` | Weighted composite score |

Falls back to rule-based scoring when LLM is unavailable. Scores are emitted as OTEL metrics for quality tracking over time.

---

## Memory

### AgentCore Memory (STM + LTM)

`supervisor/memory.py` integrates with Bedrock AgentCore Memory:

- **Short-Term Memory (STM)**: Recent investigation turns within a session
- **Long-Term Memory (LTM)**: Semantic similarity search across all completed investigations

Namespaced: `/incidents/`, `/services/{service}/`, `/patterns/{type}/`

Opt-in via `BEDROCK_AGENTCORE_MEMORY_ID`. All calls are no-ops when unconfigured.

### Experience Store

`supervisor/experience_store.py` stores high-quality investigation results locally for pre-flight context loading. Enables the harness to surface similar past incidents before the first analysis pass — no external service dependency.

---

## Observability

### OTEL Tracing

Every investigation creates a root span following [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

```
gen_ai.system = "sentinalai"
gen_ai.operation.name = "investigate"
gen_ai.request.model = "anthropic.claude-sonnet-..."
gen_ai.usage.input_tokens / output_tokens
sentinalai.incident_type   sentinalai.confidence
sentinalai.service         sentinalai.root_cause
sentinalai.tool_calls      sentinalai.budget_remaining
sentinalai.hypothesis_count sentinalai.winner_hypothesis
sentinalai.harness_rounds  sentinalai.quality_score
sentinalai.planner_mode    sentinalai.network_evidence_matched
```

### Metrics Pipeline

20+ OTEL metrics exported to Splunk via OTEL Collector sidecar:

| Metric | Type | Purpose |
|---|---|---|
| `sentinalai.investigations.total` | Counter | Completions by type/service/status |
| `sentinalai.confidence.distribution` | Histogram | Score distribution |
| `sentinalai.investigation.duration_ms` | Histogram | End-to-end latency |
| `sentinalai.worker_calls.total` | Counter | Per-worker call counts |
| `sentinalai.circuit_breaker.trips` | Counter | Circuit breaker state transitions |
| `sentinalai.budget.exhausted` | Counter | Budget exhaustion events |
| `sentinalai.harness.rounds` | Histogram | Self-correction rounds taken |
| `sentinalai.harness.quality_improvement` | Histogram | Score delta per correction round |
| `sentinalai.network.evidence_confidence` | Histogram | ThousandEyes confidence per investigation |
| `sentinalai.judge.score` | Histogram | LLM-as-judge quality scores |
| `gen_ai.client.token.usage` | Histogram | LLM token usage |

### Receipt Tracking

Every worker call produces a `Receipt` with correlation ID, worker/action, redacted params, start/end timestamps, elapsed ms, status, and result count. Sensitive fields (`password`, `token`, `secret`, `api_key`, `authorization`) are auto-redacted.

### Replay

Set `SENTINALAI_REPLAY_DIR` to persist full investigation artifacts. Load with `investigate(incident_id, replay=True)` for deterministic replay and regression testing.

---

## AGUI — Agentic UI

`agui/` is the real-time investigation operations UI:

```
agui/
  main.py            # FastAPI app + WebSocket server
  api/               # REST endpoints: intake, intelligence, investigations
  ws_manager.py      # WebSocket connection management
  event_bus.py       # Investigation event streaming
  state_store.py     # In-memory investigation state
  replay_engine.py   # Replay investigation playback
  graph_builder.py   # Evidence graph construction for UI
  receipt_store.py   # Receipt aggregation for UI display
  synthetic_generator.py  # Demo mode with realistic synthetic incidents
```

`ui/` is the React/TypeScript frontend:
- **Mission Control**: Live investigation feed with confidence trajectory charts
- **Evidence Timeline**: Ordered evidence viewer with source attribution
- **Architecture MiniMap**: Real-time service dependency graph
- **Pattern Intelligence Panel**: Matched patterns and historical context
- **Risk/Confidence Layer**: Visual confidence scoring with evidence gate status
- **Harness Reflection**: Post-investigation learning output display

**Start the UI dev server:**
```bash
cd ui && npm install && npx vite --host 0.0.0.0 --port 5173
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- AWS credentials (Bedrock + AgentCore)
- Node.js 18+ (UI only)
- Docker (optional)

### Install

```bash
git clone <repo-url> && cd Sentinalai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Run Locally (Stub Mode)

No MCP servers required. Workers return empty evidence — useful for testing the pipeline, guardrails, and hypothesis engine:

```bash
python scripts/run_investigation.py --incident-id INC12345
```

### Run with AgentCore Gateway (Production)

```bash
export AGENTCORE_GATEWAY_URL="https://gateway.agentcore.us-east-1.amazonaws.com"
export GATEWAY_OAUTH2_CLIENT_ID="your-client-id"
export GATEWAY_OAUTH2_CLIENT_SECRET="your-client-secret"
export GATEWAY_OAUTH2_TOKEN_URL="https://your-domain.auth.us-east-1.amazoncognito.com/oauth2/token"
python scripts/run_investigation.py --incident-id INC12345
```

### Run with ThousandEyes

```bash
export ENABLE_THOUSANDEYES_RCA=true
export TE_TOKEN="your-bearer-token"           # never commit this
export TE_MCP_URL="http://localhost:8004"     # ThousandEyes MCP server
# For CI/offline testing:
export TE_USE_FIXTURES=true
python scripts/run_investigation.py --incident-id INC12345
```

### Run with Self-Correction Harness

The harness is enabled by default. To tune:

```bash
export HARNESS_ENABLED=true
export HARNESS_MAX_ROUNDS=2
export HARNESS_QUALITY_GATE=0.70
export HARNESS_MIN_IMPROVEMENT=0.04
```

### Enable Agentic Planner

```bash
export AGENTIC_PLANNER=true
export PLANNER_MAX_ITERATIONS=10
export LLM_ENABLED=true
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

---

## Configuration

All configuration via environment variables. See `.env.template` for the full reference.

### Core

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-6` | LLM for refinement and planning |
| `EVAL_JUDGE_MODEL_ID` | `anthropic.claude-haiku-4-5-20251001` | LLM for judge scoring |
| `LLM_ENABLED` | `true` | Enable/disable all LLM passes |
| `LLM_TEMPERATURE` | `0.0` | Temperature (0 = deterministic) |
| `LLM_MAX_TOKENS` | `2048` | Max output tokens per LLM call |

### AgentCore Gateway

| Variable | Default | Description |
|---|---|---|
| `AGENTCORE_GATEWAY_URL` | — | Gateway HTTPS endpoint (enables gateway mode) |
| `AGENTCORE_TARGET_MOOGSOFT` | `MoogsoftTarget` | Gateway target for Moogsoft |
| `AGENTCORE_TARGET_SPLUNK` | `SplunkTarget` | Gateway target for Splunk |
| `AGENTCORE_TARGET_SYSDIG` | `SysdigTarget` | Gateway target for Sysdig |
| `AGENTCORE_TARGET_SIGNALFX` | `SignalFxTarget` | Gateway target for SignalFx |
| `AGENTCORE_TARGET_DYNATRACE` | `DynatraceTarget` | Gateway target for Dynatrace |
| `AGENTCORE_TARGET_SERVICENOW` | `ServiceNowTarget` | Gateway target for ServiceNow |
| `AGENTCORE_TARGET_GITHUB` | `GitHubTarget` | Gateway target for GitHub |
| `GATEWAY_OAUTH2_CLIENT_ID` | — | OAuth2 client ID |
| `GATEWAY_OAUTH2_CLIENT_SECRET` | — | OAuth2 client secret |
| `GATEWAY_OAUTH2_TOKEN_URL` | — | Token endpoint URL |
| `GATEWAY_ACCESS_TOKEN` | — | Static Bearer token (fallback) |

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
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP HTTP endpoint |
| `OTEL_SERVICE_NAME` | `sentinalai` | Service name for traces/metrics |
| `OTEL_DEPLOYMENT_ENV` | `dev` | Deployment environment tag |
| `SENTINALAI_REPLAY_DIR` | `/tmp/sentinalai_replays` | Replay artifact directory |

### Knowledge & Memory

| Variable | Default | Description |
|---|---|---|
| `KNOWLEDGE_GRAPH_ENABLED` | `false` | Enable knowledge graph |
| `KNOWLEDGE_STORAGE_DIR` | `knowledge/.knowledge_store` | JSONL storage directory |
| `BEDROCK_AGENTCORE_MEMORY_ID` | — | AgentCore Memory ID |
| `MEMORY_STM_LAST_K_TURNS` | `5` | Short-term memory window |
| `MEMORY_LTM_TOP_K` | `3` | Long-term memory results |

### Database (Optional)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL connection string |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size |

---

## Feature Flags

All new capabilities are off by default. Existing behavior is unchanged unless a flag is explicitly set.

| Flag | Default | Description |
|---|---|---|
| `AGENTIC_PLANNER` | `false` | Enable Think→Act→Observe loop instead of fixed playbook |
| `PLANNER_MAX_ITERATIONS` | `10` | Maximum planner iterations per investigation |
| `HARNESS_ENABLED` | `true` | Enable self-correction harness |
| `HARNESS_MAX_ROUNDS` | `2` | Max correction rounds beyond initial pass |
| `HARNESS_QUALITY_GATE` | `0.70` | Minimum quality score before harness exits |
| `HARNESS_MIN_IMPROVEMENT` | `0.04` | Minimum score delta to continue iterating |
| `HARNESS_REFLECTION_LLM` | follows `LLM_ENABLED` | Use LLM for reflection narrative |
| `ENABLE_THOUSANDEYES_RCA` | `false` | Enable ThousandEyes network evidence |
| `TE_MCP_URL` | `http://localhost:8004` | ThousandEyes MCP server URL |
| `TE_TOKEN` | — | Bearer token (env var only — never in code or Git) |
| `TE_USE_FIXTURES` | `false` | Use sanitized JSON fixtures instead of live TE API |
| `POLICY_GATE_ENABLED` | `false` | Enable pre-dispatch PolicyGate safety layer |
| `KNOWLEDGE_GRAPH_ENABLED` | `false` | Enable knowledge graph layer |
| `LLM_ENABLED` | `true` | Enable all LLM enhancement passes |

---

## Testing

```bash
# Full suite (3,604 tests across 114 test files)
cd /path/to/Sentinalai && python -m pytest

# Specific area
python -m pytest tests/test_tool_selector.py -v
python -m pytest tests/test_thousandeyes_worker.py tests/test_network_evidence_analysis.py -v
python -m pytest tests/test_working_memory.py tests/test_policy_gate.py tests/test_planner.py -v

# With coverage
python -m pytest --cov=supervisor --cov=workers --cov=knowledge --cov=integrations \
  --cov-report=term-missing

# Eval pipeline (10 scenarios, 6 quality dimensions each)
python scripts/run_evals.py
```

### Test Coverage by Area

| Area | Key Test Files | What They Validate |
|---|---|---|
| **Core Pipeline** | `test_supervisor.py`, `test_integration.py`, `test_supervisor_contracts.py` | All 10 incident types, end-to-end flow, schema |
| **Hypothesis & Voting** | `test_analyzer_branches.py`, `test_vote_logic_validation.py` | Multi-hypothesis generation, evidence scoring, tiebreaks |
| **Harness** | `test_harness_gaps.py`, `test_harness_phases_345.py` | Self-correction rounds, quality gating, plateau detection |
| **Agentic Planner** | `test_planner.py` | Think→Act→Observe loop, LLM fallback, max-iter cap, trace |
| **Working Memory** | `test_working_memory.py` | State tracking, confidence trajectory, open questions |
| **Policy Gate** | `test_policy_gate.py` | Validate/Budget/Scope checks, ALLOW/REJECT decisions |
| **ThousandEyes** | `test_thousandeyes_worker.py`, `test_network_evidence_analysis.py` | Normalizer, correlation rules, fixture mode, confidence scoring |
| **Tool Selection** | `test_tool_selector.py` | Classification, playbook retrieval, worker validity |
| **Workers** | `test_workers.py`, `test_itsm_worker.py`, `test_devops_worker.py` | All 14 workers, dispatch, error handling |
| **MCP Client** | `test_mcp_client.py`, `test_mcp_gateway_coverage.py` | Gateway routing, OAuth2, rate limiting, 401 retry |
| **LLM** | `test_llm.py`, `test_llm_judge.py` | Bedrock Converse, judge scoring, rule-based fallback |
| **Observability** | `test_observability.py`, `test_observability_otel.py` | OTEL spans, GenAI semconv, metric emission |
| **Intelligence** | `test_intelligence_foundation.py`, `test_incident_graph.py` | Graph operations, pattern detection |
| **Pattern Registry** | `test_pattern_registry.py`, `test_operational_patterns.py` | Pattern storage, matching, promotion |
| **Blast Radius** | `test_audit_gap_remediations.py`, `test_co_failure_index.py` | Blast radius inference, cascade detection |
| **Wiki** | `test_sentinel_wiki.py` | Receipt writing, indexing, search |
| **Memory** | `test_memory.py`, `test_resolution_memory.py` | STM/LTM roundtrip, graceful degradation |
| **Knowledge** | `test_knowledge_layer.py`, `test_retrieval_quality.py` | Graph store, metadata filter, retrieval, confidence boost |
| **Neural Models** | `test_neural_models.py` | Quality net, confidence calibrator |
| **Eval Pipeline** | `test_eval_pipeline.py` | Judge scores, GenAI token metrics, full scoring pipeline |
| **Infrastructure** | `test_database_connection.py`, `test_replay_store.py`, `test_guardrails.py` | Connection pooling, replay artifacts, budget/circuit breaker |

Coverage target: **80%** (enforced in `pyproject.toml`).

---

## Deployment

### AgentCore Runtime

`agentcore_runtime.py` exposes the standard AgentCore HTTP contract:

```
POST /invocations   → Run investigation (incident_id or prompt field)
GET  /ping          → Health check
```

Input validation: Incident IDs must match `^[A-Za-z0-9_\-]{1,100}$`.

### Docker

```bash
docker build -t sentinalai .
docker run -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e AGENTCORE_GATEWAY_URL=https://... \
  -e GATEWAY_OAUTH2_CLIENT_ID=... \
  -e GATEWAY_OAUTH2_CLIENT_SECRET=... \
  -e GATEWAY_OAUTH2_TOKEN_URL=... \
  sentinalai
```

Non-root container (`bedrock_agentcore`, UID 1000). Read-only rootfs. 30s health check interval.

### Docker Compose (with OTEL Collector + AGUI)

```bash
cp .env.template .env
docker compose up --build
```

Includes the OTEL Collector sidecar (`otel/opentelemetry-collector-contrib`) for Splunk HEC export, and the AGUI FastAPI server. Resource limits: 2GB/2CPU (agent), 512MB/1CPU (collector).

### AgentCore Managed Deployment

```bash
agentcore configure --config agentcore.yaml
agentcore launch
```

---

## Project Structure

```
Sentinalai/
├── supervisor/
│   ├── agent.py                    # Core investigation pipeline + hypothesis engine
│   ├── agent_harness.py            # Self-correction harness (3 layers)
│   ├── planner.py                  # AgenticPlanner — Think→Act→Observe loop
│   ├── working_memory.py           # Investigation state across correction rounds
│   ├── policy_gate.py              # Pre-dispatch safety layer (Validate→Budget→Scope)
│   ├── tool_selector.py            # Incident classification + playbooks (10 types)
│   ├── guardrails.py               # Budget, circuit breaker, query validation
│   ├── llm.py                      # Bedrock Converse client
│   ├── llm_judge.py                # LLM-as-judge quality scoring
│   ├── self_critique.py            # Self-critique gap detection
│   ├── online_evaluator.py         # Online quality scoring
│   ├── evidence_gates.py           # Evidence completeness gates
│   ├── grounding_confidence.py     # Evidence grounding checks
│   ├── neural_quality_net.py       # Quality prediction model
│   ├── neural_confidence_calibrator.py  # Platt-scaling confidence correction
│   ├── pattern_registry.py         # Recurring failure pattern store
│   ├── experience_store.py         # High-quality investigation cache
│   ├── strategy_evolver.py         # Per-type strategy weight evolution
│   ├── adaptive_thresholds.py      # Per-service threshold calibration
│   ├── learning_loop.py            # Post-flight self-improvement step
│   ├── blast_radius.py             # Blast radius inference
│   ├── cascade_tracker.py          # Cascade failure detection
│   ├── co_failure_index.py         # Co-failure correlation
│   ├── incident_dna.py             # Investigation fingerprinting
│   ├── memory.py                   # AgentCore Memory (STM + LTM)
│   ├── observability.py            # OTEL tracing
│   ├── eval_metrics.py             # 20+ OTEL metric instruments
│   ├── receipt.py                  # Worker call receipts
│   └── replay.py                   # Investigation replay store
├── workers/
│   ├── base_worker.py              # Action dispatch + timing
│   ├── ops_worker.py               # Moogsoft
│   ├── log_worker.py               # Splunk
│   ├── metrics_worker.py           # Sysdig
│   ├── apm_worker.py               # Dynatrace + SignalFx
│   ├── knowledge_worker.py         # AgentCore Memory search
│   ├── itsm_worker.py              # ServiceNow
│   ├── devops_worker.py            # GitHub
│   ├── network_worker.py           # ThousandEyes (ENABLE_THOUSANDEYES_RCA)
│   ├── confluence_worker.py        # Confluence runbooks
│   ├── code_worker.py              # Static/AST code evidence
│   ├── git_worker.py               # git blame, log, commit correlation
│   ├── visual_evidence_worker.py   # Screenshot/UI-state evidence
│   └── mcp_client.py               # AgentCore gateway + OAuth2
├── integrations/
│   └── thousandeyes/
│       ├── adapter.py              # HTTP client (Bearer auth, fixture fallback)
│       ├── normalizer.py           # NetworkEvidence + deterministic scoring
│       ├── correlation.py          # TE-CORR-001 through TE-CORR-006
│       └── fixture_loader.py       # Sanitized JSON fixture loader
├── intelligence/
│   ├── incident_graph.py           # Incident/service/artifact graph
│   ├── dependency_graph.py         # Service dependency topology
│   ├── pattern_intelligence.py     # Cross-incident pattern detection
│   ├── change_tracker.py           # Change-to-incident correlation
│   └── resolution_memory.py        # Successful resolution paths
├── sentinel_wiki/
│   ├── ingester.py                 # Receipt indexing
│   ├── searcher.py                 # Full-text + entity search
│   ├── vector_index.py             # Semantic similarity index
│   ├── pattern_promoter.py         # Pattern promotion pipeline
│   ├── receipt_writer.py           # Structured receipt generation
│   └── patterns/                   # Promoted failure pattern library
├── knowledge/
│   ├── graph_backend_json.py       # JSONL node/edge storage
│   ├── graph_store.py              # Domain graph API
│   ├── metadata_filter.py          # Hard metadata filter
│   └── retrieval_engine.py         # Similarity retrieval + confidence boost
├── agui/
│   ├── main.py                     # FastAPI + WebSocket server
│   ├── api/                        # REST API endpoints
│   ├── event_bus.py                # Investigation event streaming
│   └── ws_manager.py               # WebSocket connection management
├── ui/
│   └── src/                        # React/TypeScript frontend
│       └── components/             # Mission Control, Timeline, MiniMap, etc.
├── database/
│   └── connection.py               # PostgreSQL + pgvector
├── tests/                          # 3,604 tests across 114 test files
├── tools/
│   └── thousandeyes_discovery/     # TE fixture library (16 sanitized scenarios)
├── scripts/
│   ├── run_investigation.py        # CLI runner
│   └── run_evals.py                # Eval pipeline (10 scenarios)
├── agentcore_runtime.py            # HTTP adapter (AgentCore / FastAPI)
├── Dockerfile
├── Dockerfile.ui
├── docker-compose.yaml
├── otel-collector-config.yaml
├── agentcore.yaml
├── pyproject.toml
└── .env.template
```
