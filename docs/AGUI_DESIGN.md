# ObserveAI — AG UI Design Document
# Principal Engineer Review Level | AWS-Aligned | Production-Grade

---

## PHASE 0 — AWS CAPABILITY BASELINE

| AWS Service | Capability | Limitation for AG UI | Decision |
|---|---|---|---|
| Bedrock AgentCore | Agent lifecycle, tool routing, OAuth2, Memory (STM/LTM) | No built-in execution graph export; no streaming event bus to UI | USE — integrate Memory + gateway; EXTEND with event bridge |
| CloudWatch Logs | Full log capture, filter/search, dashboards | Log-centric, not execution-graph-centric; no DAG reconstruction; 15s metric delay | USE for ops monitoring; INSUFFICIENT for UI execution trace |
| X-Ray | Distributed traces, service map, segment analysis | No agent-level semantic context (hypothesis, confidence); UI cannot query segments in real-time | USE trace_id as correlation key; EXTEND with OTEL semantic attrs |
| ServiceLens | Service topology from traces | Reactive (post-execution), not interactive; no control plane | USE for post-mortem; SUPPLEMENT with live event streaming |
| EventBridge | Serverless event routing, filtering, replay | 256KB limit per event; 10k events/s soft limit; 1-5s delivery latency | USE for async routing between services; NOT for UI streaming |
| Kinesis Data Streams | High-throughput streaming, 1ms latency, replay | Cost at $0.015/shard-hr; operational overhead; overkill for single-agent UI | USE if multi-tenant/high-volume; SKIP for MVP (asyncio bus) |
| API Gateway (WebSocket) | Managed WebSocket connections, connection routing | $1/million messages; no stateful pub/sub; cold start on Lambda | USE for production scaling; LOCAL asyncio for MVP |
| DynamoDB | Sub-ms reads, TTL, streams, global tables | No full-text search; limited query patterns without GSI design | USE as primary live state store with TTL |
| OpenSearch | Full-text + semantic search, aggregations | Cost ($0.10/GB-hour); replication lag; complex ops | USE for incident search + memory trace queries |
| S3 | Immutable objects, versioning, lifecycle, cheap | No real-time access; eventual consistency on list | USE for receipt snapshots + replay artifacts |
| Cognito / Identity Center | OIDC/OAuth2, MFA, groups, JWT claims | UI complexity; federation setup time | USE Cognito for JWT issuance; enforce RBAC via JWT claims |
| Secrets Manager | Encrypted secrets, rotation, IAM policy | Cost ($0.40/secret/month) | USE for all credentials; already in existing system |

### Why CloudWatch Alone Is Insufficient
1. **No execution semantics** — CW logs don't know what is a hypothesis vs tool call
2. **No streaming to browser** — CW cannot push events to WebSocket
3. **No graph reconstruction** — No parent/child relationship data
4. **No control plane** — Cannot pause/approve/reject from CW
5. **No confidence/risk** — CW is metric-agnostic
6. **15-60s metric delay** — Stale for real-time incident response

### X-Ray Trace Alignment Strategy
```
OTEL Span (sentinalai) ←──── trace_id ────→ X-Ray Segment
                                              ↓
                                        X-Ray Service Map
                                              ↓
                                  AG UI uses trace_id to:
                                  - Link receipts to X-Ray spans
                                  - Provide "View in X-Ray" deeplinks
                                  - Correlate wall-clock timestamps
```
Decision: OTEL is primary; X-Ray is secondary/deeplink target.

---

## PHASE 0.5 — AWS HARDENING LAYER

### 1. TRACE CORRELATION MODEL
```
Investigation Start → generate trace_id (UUID v4, 32-char hex)
                     ↓
  Every event emitted includes:
    - trace_id (top-level)
    - span_id (per-operation, 16-char hex)
    - parent_span_id (for nesting)
    - investigation_id (business key)
    - sequence_num (monotonic, 0-indexed)

  X-Ray alignment:
    - OTEL exporter configured with xray ID generator
    - trace_id format: {version}-{epoch_hi}-{random_lo}
    - Deeplink: https://console.aws.amazon.com/xray/home#/traces/{trace_id}
```

### 2. IAM / RBAC MODEL

| Role | Capabilities | Enforcement |
|---|---|---|
| Viewer | Read incidents, investigations, receipts, graph, memory, replays | API-level: read-only methods only |
| Operator | Viewer + start investigations, trigger replay | API-level: POST /investigations, POST /replay |
| Approver | Operator + approve/reject control actions | API-level: POST /control; DynamoDB condition check |
| Admin | All + manage config, purge data, rotate tokens | API-level: DELETE + admin routes; Cognito group |

Enforcement layers:
- L1: Cognito JWT claims (`custom:agui_role`)
- L2: API middleware validates role per route
- L3: DynamoDB PutItem conditions (approver_id must match JWT sub)
- L4: UI renders controls conditionally based on role (defense-in-depth)

### 3. STORAGE STRATEGY

| Store | Type | Use Case | TTL | Cost Strategy |
|---|---|---|---|---|
| DynamoDB `agui-events` | Hot | Live event stream, last 7 days | 7 days | On-demand billing; TTL auto-purge |
| DynamoDB `agui-state` | Hot | Investigation state, control actions | 30 days | On-demand billing |
| DynamoDB `agui-control` | Hot | HITL approvals, audit log | 90 days | On-demand billing |
| S3 `agui-receipts` | Warm | Immutable receipt snapshots | 1 year → Glacier | S3 Intelligent-Tiering |
| S3 `agui-replay` | Cold | Full investigation replay snapshots | 2 years → Deep Archive | S3 Glacier Deep Archive |
| OpenSearch (future) | Warm | Incident search, memory trace | 90 days | t3.small.search, reserved |

### 4. COST MODEL (per 100 incidents/day)

| Component | Volume | Cost/Month |
|---|---|---|
| DynamoDB (events) | ~100 events/investigation × 100/day = 10k writes/day | ~$3 |
| DynamoDB (state) | 100 reads/day × 30 days | ~$1 |
| S3 (receipts) | 500KB/investigation × 100/day × 30 days = 1.5GB | ~$0.03 |
| S3 (replay) | 2MB/investigation × 100 × 30 = 6GB | ~$0.14 |
| EventBridge (future) | 10k events/day × 30 = 300k | ~$0.30 |
| **Total** | | **~$4.50/month** |

### 5. FAILURE MODES & DEGRADED UI

| Failure | Detection | UI Behavior | Recovery |
|---|---|---|---|
| Event stream lag >5s | Heartbeat timeout | Show "Delayed" banner; disable live controls | Auto-reconnect with backoff |
| Missing events (gap in sequence_num) | Sequence check in graph builder | Highlight gap in DAG as "Unknown" node | Reconcile from DynamoDB on reconnect |
| DynamoDB unavailable | Health check | Fall back to in-memory; show "Offline" badge | Auto-retry with exponential backoff |
| S3 unavailable | Receipt fetch failure | Show receipt stub with "Loading..." | Retry with local cache fallback |
| Replay inconsistency | Hash mismatch on replay | Show warning; disable replay approval | Flag for manual audit |
| Partial execution capture | Receipt count < expected | Show "Incomplete Evidence" warning | Reconcile from OTEL/CloudWatch |

---

## PHASE 1 — CURRENT SYSTEM DISCOVERY (VERIFIED)

### Architecture Map

```
[SQS / Webhook / CLI]
         ↓
  [intake.py — event-driven dispatcher]
         ↓
  [agent.py — investigation pipeline]
    ├── tool_selector.py (classify → playbook)
    ├── guardrails.py (budget, circuit breaker)
    ├── [ThreadPoolExecutor] — parallel worker calls
    │    ├── ops_worker → Moogsoft (incident)
    │    ├── log_worker → Splunk (logs, changes)
    │    ├── metrics_worker → Sysdig (metrics, events)
    │    ├── apm_worker → Dynatrace/SignalFx (golden signals)
    │    ├── itsm_worker → ServiceNow (CI, changes)
    │    ├── devops_worker → GitHub (deployments)
    │    └── confluence_worker → Confluence (runbooks)
    ├── hypothesis engine (evidence-weighted scoring)
    ├── llm.py (optional Bedrock refinement)
    ├── llm_judge.py (quality scoring, 6 dimensions)
    ├── memory.py (AgentCore STM + LTM)
    ├── receipt.py (per-call receipts with trace_id)
    ├── observability.py (OTEL spans + 20+ metrics)
    └── replay.py (artifact storage)
         ↓
  [agentcore_runtime.py — FastAPI HTTP adapter]
    ├── POST /invocations
    └── GET /ping
```

### EXISTS vs MISSING

**EXISTS:**
- ✅ Receipt system with trace_id linkage (supervisor/receipt.py)
- ✅ OTEL instrumentation with GenAI semconv (supervisor/observability.py)
- ✅ Replay artifact storage (supervisor/replay.py)
- ✅ Deterministic hypothesis engine with confidence scores
- ✅ AgentCore Memory (STM + LTM) via knowledge_worker
- ✅ Circuit breakers + budget enforcement (supervisor/guardrails.py)
- ✅ LLM judge scoring (6 dimensions)
- ✅ PostgreSQL + pgvector persistence (optional)
- ✅ SQS event-driven intake
- ✅ Docker + compose deployment

**MISSING:**
- ❌ No frontend (zero HTML/JS/React)
- ❌ No WebSocket / SSE streaming
- ❌ No BFF API layer
- ❌ No event bus (events are OTEL spans only, not queryable by UI)
- ❌ No execution graph reconstruction
- ❌ No real-time event emission to UI
- ❌ No HITL (human-in-the-loop) control system
- ❌ No Kinesis/EventBridge/DynamoDB/S3 for AG UI
- ❌ No memory trace UI
- ❌ No replay UI
- ❌ No risk/confidence visualization

---

## PHASE 2 — GAP ANALYSIS TABLE

| Capability | AWS Coverage | Current System | Gap | Action |
|---|---|---|---|---|
| Real-time execution stream | EventBridge (async) | None | CRITICAL | Build asyncio event bus + WebSocket BFF |
| Execution DAG reconstruction | X-Ray (partial) | Receipt list (flat) | HIGH | Build graph_builder.py from events |
| Receipt system | X-Ray segments | receipts with trace_id | PARTIAL | Extend with immutable storage (S3) + BFF API |
| Replay system | None built-in | replay.py (artifacts) | HIGH | Build replay_engine.py with step-by-step |
| Memory trace UI | None | AgentCore Memory | HIGH | Build memory API + MemoryTracePanel |
| Human-in-the-loop controls | None | None | CRITICAL | Build control system + ControlPanel |
| Risk/confidence scoring | None | confidence_calibrator.py | PARTIAL | Expose via API + RiskConfidenceLayer |
| Temporal freshness | CloudWatch (indirect) | wall_clock timestamps in receipts | PARTIAL | Build staleness detection + UI warnings |
| Incident browser | None | Single-incident scope | HIGH | Build incident list/search API |
| Auth/RBAC | Cognito (not wired) | Bearer token only | HIGH | Wire Cognito JWT + role-based middleware |
| Event persistence | DynamoDB (not provisioned) | OTEL only | CRITICAL | Provision DynamoDB + event store |
| Receipt storage | S3 (not provisioned) | OTEL only | HIGH | Provision S3 + receipt store |
| WebSocket streaming | API GW WebSocket (not built) | None | CRITICAL | Build WS manager + BFF |
| Execution state persistence | DynamoDB (not provisioned) | In-memory only | HIGH | Build state_store.py |
| Memory scoring dashboard | None | LLM judge scores (OTEL) | HIGH | Build memory scoring panel |

---

## PHASE 3 — TARGET ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────┐
│                     AG UI — ObserveAI                           │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                 React Frontend (Vite + TS)               │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │   │
│  │  │Incident  │ │Execution │ │Evidence  │ │Memory    │   │   │
│  │  │Command   │ │Graph     │ │Drawer    │ │Trace     │   │   │
│  │  │Center    │ │(ReactFlow│ │(Receipts)│ │Panel     │   │   │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐                │   │
│  │  │Replay    │ │Control   │ │Risk +    │                │   │
│  │  │Mode      │ │Panel     │ │Confidence│                │   │
│  │  └──────────┘ └──────────┘ └──────────┘                │   │
│  │                                                         │   │
│  │  WebSocket Client ←──── /ws/investigations/{id}        │   │
│  │  REST Client ←────────── /api/v1/*                     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ↑ HTTP / WS                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              BFF Layer (FastAPI + uvicorn)               │   │
│  │                                                         │   │
│  │  ws_manager.py    ← pub/sub WebSocket connections       │   │
│  │  event_bus.py     ← asyncio pub/sub (EventBridge ready) │   │
│  │  graph_builder.py ← DAG reconstruction from events      │   │
│  │  replay_engine.py ← deterministic step-by-step replay   │   │
│  │  state_store.py   ← DynamoDB (+ memory fallback)        │   │
│  │  receipt_store.py ← S3 (+ local fallback)               │   │
│  │  middleware/auth  ← JWT + RBAC                          │   │
│  │  middleware/trace ← trace_id propagation                │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ↑ events                               │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │          Agent Layer (existing supervisor/)              │   │
│  │                                                         │   │
│  │  agui_bridge.py  ← emits events to event_bus            │   │
│  │  agent.py        ← investigation pipeline               │   │
│  │  receipt.py      ← per-call receipt tracking            │   │
│  │  observability.py ← OTEL spans                          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ↕ AWS services                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │DynamoDB  │ │S3 Receipt│ │AgentCore │ │CloudWatch/X-Ray  │  │
│  │(state +  │ │Store     │ │Memory    │ │(ops observability│  │
│  │ events)  │ │          │ │          │ │ + X-Ray deeplink)│  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Consistency + Latency Targets
- UI update < 2s: WebSocket push from event_bus; DynamoDB on-demand
- Replay load < 5s: S3 snapshot pre-fetched; step replay at controlled pace
- Receipt fetch < 500ms: DynamoDB GSI on investigation_id
- Graph reconstruction < 1s: In-memory DAG built incrementally per event

### Idempotency Design
- Events: idempotency_key = SHA256(investigation_id + sequence_num)
- Receipts: receipt_id = UUID stored in DynamoDB; duplicate publish = no-op
- Control actions: DynamoDB conditional write (action_id = UUID, status = pending → approved/rejected)
- Replay: event_hash = SHA256(event payload); mismatch triggers warning

---

## PHASE 4 — DATA CONTRACTS

### 4.1 EVENT SCHEMA (v1.0)
See: agui/schemas/events.py

### 4.2 RECEIPT SCHEMA (v1.0)
See: agui/schemas/receipts.py

### 4.3 EXECUTION GRAPH NODE SCHEMA (v1.0)
See: agui/schemas/graph.py

### 4.4 INCIDENT STATE SCHEMA (v1.0)
See: agui/schemas/incidents.py

### Versioning Strategy
- schema_version field on all top-level objects (semver string)
- Breaking changes = major version bump
- Additive changes = minor version bump
- All readers must handle unknown fields gracefully (Pydantic extra="allow")
- Schema registry: agui/schemas/ directory is single source of truth

---

## PHASE 5 — AG UI DESIGN (COMPONENT HIERARCHY)

```
App
└── AppShell
    ├── Sidebar (incident list + navigation)
    ├── TopBar (trace_id, status, role indicator)
    └── MainView
        ├── IncidentCommandCenter (default view)
        │   ├── EventTimeline (WebSocket-driven, chronological)
        │   └── AgentDecisionOverlay (hypothesis + reasoning)
        ├── ExecutionGraph (panel)
        │   ├── GraphCanvas (ReactFlow DAG)
        │   └── NodeDetail (drawer on node click)
        ├── EvidenceDrawer (panel)
        │   ├── ReceiptCard (per tool call)
        │   └── EvidenceList (filterable)
        ├── MemoryTracePanel (panel)
        │   ├── SimilarIncidentCard (with similarity score)
        │   └── MemoryFilters (service, time window, type)
        ├── ReplayMode (mode overlay)
        │   ├── ReplayControls (play/pause/step/speed)
        │   └── ReplayTimeline (scrubber)
        ├── ControlPanel (overlay, approver+ only)
        │   ├── ActionButton (approve/reject/pause/resume)
        │   └── ControlLog (audit trail)
        └── RiskConfidenceLayer (always-visible bar)
            ├── ConfidenceGauge (0-100%)
            ├── RiskIndicator (low/medium/high/critical)
            └── StaleDataWarning (data freshness)
```

### State Management (Zustand)

```typescript
investigationStore:
  - current_investigation: Investigation | null
  - events: AGUIEvent[]
  - graph: ExecutionGraph
  - receipts: Receipt[]
  - control_actions: ControlAction[]
  - replay_state: ReplayState
  - ws_status: 'connecting' | 'connected' | 'disconnected'

incidentStore:
  - incidents: Incident[]
  - selected_incident_id: string | null
  - filters: IncidentFilters
  - pagination: PaginationState
```

---

## PHASE 6 — IMPLEMENTATION PLAN

### Phase 1 (Core): Event streaming + Timeline UI
- Dependencies: schemas, event_bus, ws_manager, state_store
- Validation: WebSocket connects, events flow, timeline renders
- Risk: asyncio thread safety (agent runs sync → bridge uses threadsafe call)

### Phase 2 (Graph + Evidence): DAG + Receipt integration
- Dependencies: Phase 1 + graph_builder, receipt_store
- Validation: Graph nodes match receipt count; no orphan nodes
- Risk: Event ordering; parallel worker calls create concurrent branches

### Phase 3 (Replay + Memory): Replay system + memory trace
- Dependencies: Phase 2 + replay_engine, memory API
- Validation: Deterministic replay produces same graph; memory results match
- Risk: S3 availability; replay hash mismatch detection

### Phase 4 (Control + Risk): HITL + confidence/risk layer
- Dependencies: Phase 3 + auth middleware, control API
- Validation: Approver can pause/approve; viewer cannot; confidence updates live
- Risk: Race conditions on control actions (resolved by DynamoDB conditional writes)
