# SentinelAI — Next-Generation Intelligence Foundation

**Version:** 1.0  
**Status:** Approved for Phase 1 Implementation

---

## 1. Architecture Review

### Current State Assessment

SentinelAI is a well-engineered autonomous RCA system with strong foundational components:
deterministic quality gates (G1–G6), multi-round correction harness, OTEL observability,
hybrid retrieval (BM25 + TF-IDF + staleness decay), and a closed learning loop. The platform
produces correct, evidence-backed root cause analysis.

What it is NOT yet: a continuously learning operational intelligence platform.

The gap is not capability — it is architecture. Evidence, outcomes, patterns, and decisions are
scattered across 12+ independent JSON files with no shared schema, no cross-queryability, and
no unified entity model. Adding long-term learning, pattern intelligence, or CMDB integration
at this state would require touching every file.

### Architectural Debt (Most Expensive to Retrofit Later)

| Debt | Retrofit Cost | Eliminated By |
|------|--------------|---------------|
| No `investigation_id` primary key (uses `incident_id`, causing collisions) | HIGH — touches every store | `InvestigationRecord.investigation_id` |
| No `service_id`/`application_id` in any schema | HIGH — touches 8 JSON files | All entities carry these fields now |
| Evidence is untyped `dict`; no relationship model | HIGH — requires reprocessing all history | `EvidenceGraph` |
| No outcome linkage (can't answer "did the fix work?") | MEDIUM — adds new store | `ResolutionOutcome` |
| Pattern matching uses hand-engineered 16-dim vector only | MEDIUM — can't add graph structure later | `PatternSignature` with graph fingerprint |
| No decision provenance (why was this recommendation made?) | MEDIUM | `DecisionTrace` |
| Replay artifacts in /tmp (not durable) | LOW | `ReplaySeed` with explicit store |
| Multiple storage backends (8 JSON, 2 SQLite, 1 NDJSON, 1 PG) | LOW now / HIGH at scale | `InvestigationStore` unified coordinator |

---

## 2. Gap Analysis

### Evidence Layer
- Evidence is `dict[str, Any]` — no schema, no validation, no relationship model
- No way to express "Log A preceded Alert B by 4 minutes and was on the same host"
- Cross-source relationships (Splunk log + Dynatrace metric = correlated) not persisted
- Source provenance not linked to specific claims in the RCA

### Operational Memory Layer
- 8 separate JSON files with overlapping concerns
- `co_failure_index.json` + `cascade_tracker.json` + `blast_radius_history.json` each need the same service data with no single source of truth
- Service learning cannot answer: "how has this service's failure profile evolved?"
- No application-level grouping (service ≠ application)

### Pattern Intelligence Layer
- `PatternRegistry` uses cosine similarity on 16-dim vector → finds numerically similar incidents
- Cannot find structurally similar incidents (same failure propagation path, different services)
- Pattern matching is one-way: find past patterns from current incident. Cannot ask "which services show this pattern now?"
- No outcome linkage: don't know which patterns lead to successful resolutions

### Decision Intelligence Layer
- Every recommendation has `why: str` set to empty or post-hoc
- No immutable record of what evidence drove a decision
- Cannot audit: "why did the agent recommend restart instead of rollback?"

### Replay Intelligence Layer
- `ReplayStore` writes to `/tmp/sentinalai_replays` (ephemeral, max 24h, max 500 files)
- No structured seed for reconstruction — just raw snapshots
- No forward-compatibility versioning

### Knowledge Graph Readiness
- `knowledge/graph_store.py` stores incident→service→artifact nodes, but no IDs that would match CMDB
- No `service_id`, `application_id`, `entity_id` in any current schema
- Adding Dynatrace Smartscape or Kubernetes topology later requires schema redesign of every JSON file

---

## 3. Requirements

### Functional Requirements

**FR-1:** Every investigation must be addressable by a stable `investigation_id` (not `incident_id`)  
**FR-2:** Evidence must be represented as a typed graph with named relationships  
**FR-3:** Resolution outcomes must be stored and linked to investigations  
**FR-4:** Service profiles must aggregate learning across all investigations for a service  
**FR-5:** Patterns must capture graph structure, not just feature vectors  
**FR-6:** Every decision must record supporting evidence, pattern match, and historical success rate  
**FR-7:** Investigations must be replayable from a stored seed  
**FR-8:** All schemas must carry `service_id`, `application_id`, `entity_id` for future CMDB integration  
**FR-9:** All new components must be additive — zero changes to existing RCA flows  
**FR-10:** Intelligence layer failures must not block investigations  

### Non-Functional Requirements

**NFR-1:** O(1) node/edge lookup by ID  
**NFR-2:** 100k+ investigations storable without redesign (monthly directory partitioning)  
**NFR-3:** All writes atomic (tmp-swap) and thread-safe  
**NFR-4:** Schema versioned for forward compatibility  
**NFR-5:** Full serialization round-trip fidelity (to_dict → from_dict → to_dict is stable)  

---

## 4. Technical Design

### Design Principles

**1. Investigation-Centric:** Everything orbits `investigation_id`. One investigation = one graph + one
   replay seed + N decision traces + 0-1 outcome.

**2. Deterministic IDs:** All entity IDs are content-addressed (sha256 of canonical input).
   Re-ingesting the same evidence produces the same node IDs. Enables safe retries and deduplication.

**3. Graph-Native Evidence:** Evidence is not a flat dict. It is a directed property graph where nodes are
   evidence artifacts and edges encode operational relationships.

**4. Outcome-Linked Patterns:** A `PatternSignature` is only useful if it carries a `success_rate`.
   Success rate is derived from `ResolutionOutcome` records. This closes the learning loop.

**5. Immutable Append:** Investigation records are never mutated in place. New information (outcome,
   operator feedback, additional traces) is appended. The record at investigation_id t=0 is preserved.

**6. Additive by Construction:** New components live in `intelligence/`. Existing code imports nothing
   from `intelligence/`. A bridge (`intelligence/bridge.py`) converts existing dicts to graphs.

### Component Architecture

```
intelligence/
  schema.py              — NodeType, EdgeRelationship, ResolutionStatus, EntityType, deterministic ID
  evidence_graph.py      — EvidenceNode, EvidenceEdge, EvidenceGraph
  resolution_outcome.py  — ResolutionOutcome, OutcomeStore
  service_profile.py     — ServiceProfile, ServiceProfileIndex
  pattern_signature.py   — PatternSignature, PatternSignatureIndex
  decision_trace.py      — DecisionTrace, DecisionTraceLog
  replay_seed.py         — ReplaySeed, ReplaySeedStore
  bridge.py              — evidence_dict → EvidenceGraph (backward compat)
  investigation_store.py — Unified coordinator (all sub-stores)
  __init__.py            — Public API surface
```

### ID Scheme

All IDs are deterministic 16-character hex strings derived from sha256:

```
node_id    = sha256("{source_type}:{entity_id}:{ts_bucket_10s}")[:16]
edge_id    = sha256("{src_id}:{relationship}:{dst_id}")[:16]
pattern_id = sha256(sorted(node_types) + sorted(edge_rels) + incident_type)[:16]
outcome_id = sha256("{investigation_id}:{executed_action}:{resolution_ts}")[:16]
seed_id    = sha256("replay:{investigation_id}:{created_at}")[:16]
```

`ts_bucket_10s` = floor(unix_ts / 10) * 10 — collapses near-simultaneous evidence to same node.

---

## 5. Data Model

### EvidenceNode

```python
EvidenceNode(
    node_id:          str,          # deterministic sha256[:16]
    node_type:        NodeType,     # METRIC|LOG|EVENT|CHANGE|TRACE|ALERT|CMDB|RUNBOOK|OUTCOME
    source_type:      str,          # splunk|dynatrace|sysdig|servicenow|cmdb|moogsoft
    entity_id:        str,          # logical entity: service name, host, pod
    entity_type:      EntityType,   # SERVICE|HOST|POD|CONTAINER|DATABASE|QUEUE|ENDPOINT
    content:          dict,         # raw evidence payload (preserved exactly)
    timestamp:        str,          # ISO-8601: when evidence occurred
    collected_at:     str,          # ISO-8601: when collected by agent
    confidence:       float,        # 0–1, from source_confidence tier
    investigation_id: str,
    # Knowledge Graph readiness (populated when CMDB integrated)
    service_id:       str,          # CMDB service GUID (empty until KG integration)
    application_id:   str,          # CMDB application GUID
    topology_id:      str,          # Dynatrace/K8s topology node ID
    agent_id:         str,          # which agent instance collected this
)
```

### EvidenceEdge

```python
EvidenceEdge(
    edge_id:          str,          # deterministic sha256[:16]
    src_id:           str,          # EvidenceNode.node_id
    dst_id:           str,          # EvidenceNode.node_id
    relationship:     EdgeRelationship, # see Relationships section
    weight:           float,        # edge confidence/strength 0–1
    timestamp:        str,          # ISO-8601: when relationship established
    evidence:         dict,         # supporting evidence for this relationship
    investigation_id: str,
)
```

### Edge Relationships

| Relationship | Semantics | Example |
|--------------|-----------|---------|
| `CAUSED_BY` | A was caused by B | OOMKill CAUSED_BY memory leak |
| `PRECEDED` | A occurred before B (temporal) | CPU spike PRECEDED OOMKill by 4m |
| `CORRELATED` | A and B co-occurred (no causality determined) | Error spike CORRELATED with deployment |
| `AFFECTS` | A impacts B's health | connection pool AFFECTS payment-service |
| `RUNS_ON` | A runs on infrastructure B | payment-service RUNS_ON node-42 |
| `HOSTED_ON` | A is hosted on platform B | pod HOSTED_ON k8s-cluster-prod |
| `DEPENDS_ON` | A depends on B | checkout DEPENDS_ON payment |
| `GENERATED_BY` | Evidence A was generated by action B | heap dump GENERATED_BY jmap call |

### EvidenceGraph

```python
EvidenceGraph(
    graph_id:         str,          # = investigation_id
    investigation_id: str,
    incident_id:      str,
    service:          str,
    incident_type:    str,
    phase:            InvestigationPhase,
    schema_version:   str,          # "1.0"
    created_at:       str,
    _nodes:           dict[str, EvidenceNode],    # O(1) lookup
    _edges:           dict[str, EvidenceEdge],    # O(1) lookup
    _out_edges:       dict[str, list[str]],       # src_id → [edge_ids]
    _in_edges:        dict[str, list[str]],       # dst_id → [edge_ids]
)
```

### ResolutionOutcome (Requirement 28)

```python
ResolutionOutcome(
    outcome_id:            str,
    investigation_id:      str,
    incident_id:           str,
    service_id:            str,
    application_id:        str,
    pattern_signature_id:  str,
    recommendation_id:     str,
    recommended_action:    str,
    executed_action:       str,
    resolution_status:     ResolutionStatus,  # SUCCESS|PARTIAL_SUCCESS|FAILED
    mttr_minutes:          float,
    resolution_timestamp:  str,
    operator_feedback:     str,
    operator_notes:        str,
    created_at:            str,
)
```

### ServiceProfile

```python
ServiceProfile(
    profile_id:              str,   # = service_name (stable key)
    service_name:            str,
    service_id:              str,   # future CMDB GUID
    application_id:          str,   # future CMDB GUID
    recurring_incident_types: dict[str, int],
    recurring_entities:       dict[str, int],
    recurring_dependencies:   dict[str, int],
    recurring_symptoms:       dict[str, int],
    recurring_resolutions:    dict[str, int],
    investigation_ids:        list[str],
    failure_history:          list[dict],   # [{ts, incident_type, mttr, status}]
    resolution_history:       list[dict],   # [{ts, action, outcome}]
    total_investigations:     int,
    avg_mttr_minutes:         float,
    last_updated:             str,
    first_seen:               str,
)
```

### PatternSignature

```python
PatternSignature(
    pattern_id:             str,    # sha256(node_types+edge_rels+incident_type)[:16]
    node_type_sequence:     list[str],
    edge_relationship_sequence: list[str],
    service_scope:          str | None,   # None = universal pattern
    incident_types:         list[str],
    frequency:              int,
    success_rate:           float,
    failure_rate:           float,
    confidence:             float,
    last_seen:              str,
    first_seen:             str,
    matching_resolutions:   list[str],
    features:               list[float],  # 16-dim, compatible with PatternRegistry
)
```

### DecisionTrace

```python
DecisionTrace(
    trace_id:               str,
    investigation_id:       str,
    decision_type:          str,    # hypothesis|recommendation|gate_verdict|step_selection
    decision:               str,
    supporting_evidence:    list[dict],   # [{node_id, content, confidence}]
    contradicting_evidence: list[dict],
    pattern_id:             str | None,
    pattern_frequency:      int,
    pattern_success_rate:   float,
    prior_occurrence_count: int,
    historical_success_rate: float,
    confidence:             float,
    reasoning_path:         list[str],
    why:                    str,    # human-readable causal explanation
    created_at:             str,
)
```

### ReplaySeed

```python
ReplaySeed(
    seed_id:                str,
    replay_seed_id:         str,    # globally unique alias
    investigation_id:       str,
    incident_id:            str,
    incident_snapshot:      dict,   # incident at investigation time
    evidence_graph_snapshot: dict,  # EvidenceGraph.to_dict()
    tool_call_sequence:     list[dict],  # ordered receipts
    rca_report_snapshot:    dict,
    decision_traces:        list[dict],
    resolution_outcome:     dict | None,
    schema_version:         str,
    created_at:             str,
    aarc_compatible:        bool,
)
```

---

## 6. Entity Relationship Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          INVESTIGATION                                    │
│  investigation_id (PK)  incident_id  service  incident_type  phase        │
└──────────┬──────────────────┬─────────────────┬──────────────────┬───────┘
           │ 1:1              │ 1:N             │ 1:1             │ 1:1
           ▼                  ▼                  ▼                  ▼
  ┌────────────────┐  ┌───────────────┐  ┌──────────────┐  ┌──────────────┐
  │ EvidenceGraph  │  │ DecisionTrace │  │  ReplaySeed  │  │ Resolution   │
  │ graph_id       │  │ trace_id      │  │  seed_id     │  │ Outcome      │
  │ phase          │  │ decision_type │  │  schema_ver  │  │ outcome_id   │
  │ schema_version │  │ decision      │  │  aarc_compat │  │ status       │
  └───────┬────────┘  │ why           │  └──────────────┘  │ mttr_minutes │
          │           │ confidence    │                      └──────┬───────┘
          │           │ pattern_id ───┼──────────────────────┐     │
          │           │ prior_count   │                       │     │
          │           └───────────────┘                       │     │
          │ 1:N                                               │     │ N:1
          ├────────────────────┐                              ▼     ▼
          ▼                    ▼                    ┌──────────────────────┐
  ┌──────────────┐  ┌──────────────────┐           │  PatternSignature    │
  │ EvidenceNode │  │  EvidenceEdge    │           │  pattern_id (PK)     │
  │ node_id (PK) │  │  edge_id (PK)    │           │  node_type_sequence  │
  │ node_type    │◄─┤  src_id (FK)     │           │  edge_rel_sequence   │
  │ source_type  │◄─┤  dst_id (FK)     │           │  frequency           │
  │ entity_id    │  │  relationship    │           │  success_rate        │
  │ entity_type  │  │  weight          │           │  failure_rate        │
  │ content      │  │  evidence        │           │  confidence          │
  │ confidence   │  └──────────────────┘           └──────────────────────┘
  │ service_id──►│─────────────────────────────────────────► (future CMDB)
  │ application_id
  │ topology_id ►│─────────────────────────────────► (future Smartscape)
  └──────────────┘
                    ┌──────────────────────────────────────────────────────┐
                    │                   ServiceProfile                      │
                    │  profile_id (= service_name)  service_id  app_id     │
                    │  recurring_incident_types  recurring_resolutions      │
                    │  investigation_ids[]  failure_history[]               │
                    │  avg_mttr_minutes  total_investigations               │
                    └──────────────────────────────────────────────────────┘
                                  (derived from ResolutionOutcome + Graphs)
```

---

## 7. Folder Structure

```
intelligence/
  __init__.py              Public API: EvidenceGraph, ResolutionOutcome, etc.
  schema.py                NodeType, EdgeRelationship, ResolutionStatus, EntityType,
                           InvestigationPhase, new_id()
  evidence_graph.py        EvidenceNode, EvidenceEdge, EvidenceGraph
  resolution_outcome.py    ResolutionOutcome, OutcomeStore
  service_profile.py       ServiceProfile, ServiceProfileIndex
  pattern_signature.py     PatternSignature, PatternSignatureIndex
  decision_trace.py        DecisionTrace, DecisionTraceLog
  replay_seed.py           ReplaySeed, ReplaySeedStore
  bridge.py                evidence_dict_to_graph() — backward compat
  investigation_store.py   InvestigationStore — unified coordinator

eval/
  investigations/          Per-investigation artifacts
    {YYYYMM}/              Monthly partition (archival-friendly)
      {investigation_id}.json
    _index.jsonl           Append-only investigation index (fast service/type lookup)
  resolution_outcomes.jsonl Append-only outcome log
  service_profiles.json    Aggregated service profiles
  pattern_signatures.json  Pattern library

tests/
  test_intelligence_foundation.py  All Phase 1 tests
```

---

## 8. Migration Strategy

### Principle: Zero-Break Migration

All new components are additive. No existing file is changed. Existing tests continue to pass.

### Phase 0 (NOW): Foundation
- Create `intelligence/` package
- All tests pass, no integration with agent yet

### Phase 1 (Next 2 sessions): Passive Capture
- Add `EvidenceGraph` construction to `agent_harness.py` post-flight
- Add `ReplaySeed` write to `replay.py`
- Add `DecisionTrace` write after gate evaluations in `evidence_gates.py`
- Write to `eval/investigations/` alongside existing outputs
- Existing code path unchanged

### Phase 2 (Future): Active Learning
- Wire `ServiceProfile` into `tool_selector.py` (skip historically-useless steps)
- Wire `PatternSignature` into hypothesis ranking
- Wire `ResolutionOutcome` into blast radius risk weighting

### Phase 3 (Future): CMDB Integration
- Populate `service_id`, `application_id`, `topology_id` from CMDB lookup
- Enable Dynatrace Smartscape edge import (`RUNS_ON`, `HOSTED_ON` edges)
- Enable cross-investigation graph queries via InvestigationStore

### Migration Compatibility
- All Phase 0 schemas include `schema_version: "1.0"`
- Forward compatibility: extra fields in JSON are preserved via `extras: dict`
- Old `eval/*.json` files remain authoritative until Phase 2 cutover
- `bridge.py` provides bidirectional conversion

---

## 9. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| EvidenceGraph construction fails mid-investigation | LOW | LOW | bridge.py wraps in try/except; investigation continues |
| Monthly partition directory grows too large | LOW | MEDIUM | Max 5000 files/dir before sub-partitioning; monitor via _index.jsonl count |
| Deterministic IDs collide | VERY LOW | HIGH | sha256[:16] = 1-in-18-trillion collision rate at 100k files |
| Schema version mismatch on load | LOW | MEDIUM | Unknown version → log warning, skip component, don't crash |
| Outcome store not populated (operator never closes loop) | HIGH | LOW | All outcome fields optional; profiles still build from graph-only data |
| PatternSignature false positives (wrong pattern match) | MEDIUM | MEDIUM | Pattern only boosts confidence (+10 max); doesn't override gate logic |

---

## 10. Phased Implementation Plan

### Phase 1 — Intelligence Foundation (THIS SESSION)
**Goal:** Core data model, all stores, bridge, tests  
**Files:** 10 Python modules + 1 test file  
**Integration:** Zero — purely additive  
**Success criteria:** All tests pass, serialization round-trips are stable

### Phase 2 — Passive Capture (Next Session)
**Goal:** Silently write intelligence artifacts during every investigation  
**Changes:** agent_harness.py (+15 lines), replay.py (+10 lines), evidence_gates.py (+8 lines)  
**Success criteria:** After 10 investigations, `eval/investigations/` has 10 populated artifacts

### Phase 3 — Pattern Feedback (Future)
**Goal:** PatternSignature influences hypothesis ranking  
**Changes:** tool_selector.py, grounding_confidence.py  
**Success criteria:** Investigations for repeat patterns converge 20% faster

### Phase 4 — CMDB Graph (Future)
**Goal:** Populate service_id/application_id/topology_id from live CMDB  
**Changes:** New `intelligence/cmdb_adapter.py` only  
**Success criteria:** RUNS_ON and HOSTED_ON edges present in graphs for known services

### Phase 5 — Autonomous Outcomes (Future)
**Goal:** Auto-populate ResolutionOutcome from fix_engine verification  
**Changes:** fix_engine.py calls `OutcomeStore.record()` after verification  
**Success criteria:** Success rate tracked for all applied remediations
